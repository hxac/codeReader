# u2-l4 · ops/attention.rs 与 ops/paged_plan.rs：分页注意力的调度面

> 依赖：u2-l3（ops.rs 总览）。本讲是 ops.rs 中「再出口之外的自研部分」的精读：三个注意力封装入口、两套分页计划类型，以及它们与 `kv_pool.rs` 中 `KvDesc` 的契约。

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 paged attention 中「计划（plan）」与「KV 数据」分离的原因——为什么 kernel 需要的是一张"页号索引表"而不是 KV 张量本身。
2. 逐参数读懂 `prefill_attention_paged_into`、`paged_attention_batch_decode_into`、`paged_attention_batch_decode_split_kv_into` 三个入口，并把参数按「KV 数据 / 几何 / 计划 / 输出」归类。
3. 讲清 `PrefillPagedPlan`（含预分配 + 原地重填的图稳定用法）、`SplitKvCsr` 与 `build_split_kv_csr` 各自解决什么问题，手算一个 split-KV CSR 实例。
4. 对照 `KvDesc<'a>`（含 `desc()` 与 `desc_for_len()` 的差异），复述「core 描述状态、kernels 消费状态」这条契约，并能回答"两个请求合并成一个批次"这类设计题。

## 2. 前置知识

本讲默认你已读过 u2-l1（tensor 门面）与 u2-l3（ops.rs 总览），另外补充三个概念：

- **Paged KV cache（分页 KV 缓存）**：KV 缓存不像连续数组那样整块存放，而是切成固定大小的「页」（page，如 16 个 token 一页），每页在池里有自己的页号。一个请求的 KV 散落在若干页里，注意力 kernel 需要一张「页号表」才能找到它们。u3-l2 会精读池本身；本讲只把它当作数据侧的既成事实。
- **CSR（Compressed Sparse Row，压缩稀疏行）**：一种用「值数组 + 行偏移数组」表达变长分组的数据结构。本讲的 `SplitKvCsr` 与 plan 里的 `page_indptr`、`o_indptr`、`q_indptr` 都是 CSR：例如 `o_indptr = [0, 2, 4]` 表示第 0 个请求占输出槽 `[0,2)`、第 1 个占 `[2,4)`。
- **Split-KV（KV 切分）**：解码时若某请求的上下文很长，一个线程块算不完整个 KV，就把它的 KV 切成若干 chunk，多个块并行各算一段，最后归并。这要求调度面额外提供"哪个请求的第几个 chunk"的索引——这正是 `SplitKvCsr` 的由来。
- **padding 行（填充行）**：CUDA Graph 回放要求每一步的形状固定，所以批次数组都按最坏情况（`padded_bs × cap` 个槽位）预分配，空槽填 0 并用 `block_valid_mask = 0` 标记无效。这与 u1-l1 提过的「padding 行是协议面」一脉相承。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-core/src/ops.rs` | 算子门面：声明 `mod attention` / `mod paged_plan` 两个私有子模块，并 `pub use` 本讲的全部 6 个名字 |
| `pegainfer-core/src/ops/attention.rs` | 三个注意力封装入口：薄包装 kernels 同名函数，负责把 core 的 `KvLayout` 转成 kernels 的 `PagedKvLayout` |
| `pegainfer-core/src/ops/paged_plan.rs` | 分页计划：`PrefillPagedPlan`（包装 kernels 同名类型）、`SplitKvCsr` 与纯 CPU 构造器 `build_split_kv_csr` |
| `pegainfer-core/src/kv_pool.rs` | KV 池与请求状态：`KvState::desc()` / `desc_for_len()` 产出 `KvDesc<'a>`，是 plan 的上游数据源 |
| `pegainfer-kernels/src/ops/attention.rs` | （真身，本讲只作交叉引用）kernels 侧的 `PrefillPagedPlan` 定义与三个注意力 kernel 封装的实现 |
| `pegainfer-qwen3/src/batch_decode_buffers.rs` | （消费方示例）调用 `build_split_kv_csr` 装配解码元数据，并带三个纯 CPU 的 CSR 单测，是本讲实践的锚点 |

## 4. 核心概念与源码讲解

### 4.1 计划与数据分离：为什么需要一个「调度面」

#### 4.1.1 概念说明

KV 数据（`CudaSlice<bf16>` 大缓冲）与「哪些页属于哪个请求」这两件事在物理上是分离的：池里只有无主的页，请求持有的只是页号列表。注意力 kernel 做一次计算，需要两类输入：

- **数据侧**：整块 KV 缓冲的指针 + 几何（每页多大、多少层、多少头、头维度多少）——所有请求共享同一份。
- **调度侧**：本批次每个请求的页号表、序列长度、输出行位置——每个请求都不同、每一步都变。

「调度面」就是后者：把请求级的页表元数据整理成 kernel 可直接消费的设备端数组。pegainfer-core 里 `kv_pool.rs` 的 `KvDesc` 是最小粒度的描述（单请求、宿主侧），`ops/paged_plan.rs` 是批次粒度的计划（多请求、含设备端上传与图稳定复用），`ops/attention.rs` 则是把两者与数据侧拼装后转交给 kernels 的入口。

这个分离在源码里有明文注释：`KvPool::buffer()` 的文档写明"池中所有状态都是同一缓冲的视图，executor 可以持有池、从 plan（而非请求状态）取页元数据"（[kv_pool.rs:254-259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L254-L259)）。

为什么值得分离？三个直接收益：

1. **零拷贝调度**：请求来了又走，页在池里换手，KV 数据从不在请求间搬运；改变批次只需重写一张小小的索引表。
2. **CUDA Graph 兼容**：索引表可以预分配到最坏大小后原地重填，设备指针不变（见 4.3.3）。
3. **职责单一**：core 描述"状态长什么样"，kernels 决定"怎么消费"，两边只隔着几个 `i32` 数组的契约。

#### 4.1.2 核心流程

一次注意力计算的拼装过程（伪代码）：

```text
# 宿主侧（每个调度步）
for req in batch:
    desc = req.kv_state.desc()            # 页号切片 + last_page_len（宿主侧，零 GPU）
plan = PrefillPagedPlan::from_raw_batch(...)   # 或 update_batch 原地重填
csr  = build_split_kv_csr(chunk_size, cap, kv_lens, padded_bs)  # split-KV 索引，纯 CPU

# 设备侧（kernel 启动）
prefill_attention_paged_into(
    kv_buffer,            # 数据侧：整块池缓冲（所有请求共享）
    layout,               # 数据侧：几何（stride 等）
    plan,                 # 调度侧：page_indices_d / q_indptr_d / ...
    ...
)
```

分层的形状：

```text
KvState（每请求，RAII 持页）
   └─ desc() / desc_for_len() → KvDesc<'a>（单请求页表，宿主侧切片）
                                   │
                                   ▼
        PrefillPagedPlan（批次计划，设备端 i32 数组，可预分配/原地重填）
        SplitKvCsr（split-KV 的批次计划，宿主 Vec，由消费方上传）
                                   │
                                   ▼
        ops/attention.rs 三入口（把 数据+几何+计划 打包交给 kernels）
                                   │
                                   ▼
        pegainfer_kernels::ops::attention（真身：真正的 kernel 启动）
```

#### 4.1.3 源码精读

ops.rs 的模块声明与再出口——本讲的 6 个公开名字全部出自这里，两个子模块本身是私有的：

[ops.rs:L3-L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L3-L16)：声明 `mod attention`（私有）、`mod paged_plan`（私有），再出口 3 个注意力函数、`PrefillPagedPlan`、`SplitKvCsr`、`build_split_kv_csr`。注意 attention 与 paged_plan 没有走 u2-l3 里那 69 项 kernels 再出口的路子——它们是 core **自研**的封装层。

kernels 侧真身的位置（交叉验证用，细节留待读 kernels 时展开）：[pegainfer-kernels/src/ops/attention.rs:L31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L31) 定义 `pub struct PrefillPagedPlan`，同文件 [L604](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L604)、[L1226](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1226)、[L1355](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1355) 分别是三个注意力入口的实现。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「计划与数据分离」在源码中的落点。
2. **操作步骤**：
   - 打开 [kv_pool.rs:L254-L259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L254-L259)，抄下 `buffer()` 的文档注释。
   - 在 `pegainfer-core/src/` 内检索 `kv_buffer` 与 `page_indices_d` 两个词，数一数它们各自出现在哪些文件。
3. **需要观察的现象**：`kv_buffer` 只以 `&CudaSlice<bf16>` 引用的形式出现在 attention.rs 的签名里——core 从不读写 KV 数据内容；`page_indices_d` 只出现在 plan 的 getter 里——core 只搬运索引。
4. **预期结果**：你会得到一个结论：core 对 KV 数据是"纯转发"，对页表是"全程经手"。这正是调度面的含义。

#### 4.1.5 小练习与答案

**练习 1**：如果不做计划/数据分离，每个请求各自持有连续的 KV 张量，注意力批次会发生什么？

**答案**：批内每个请求的 KV 长度不同、地址不连续，kernel 无法用一个统一的 GEMM/attention 形状覆盖整批，只能逐请求启动或先做一次数据搬运合并；同时请求的进出让 KV 缓冲频繁重排，CUDA Graph 也无法捕获固定形状的批内核。分页 + 索引表让"数据不动、索引动"。

**练习 2**：`KvDesc` 与 `PrefillPagedPlan` 分别住在宿主侧还是设备侧？

**答案**：`KvDesc<'a>` 是宿主侧的借用切片（`&[PageId]` + `usize`），不涉及 GPU；`PrefillPagedPlan` 内部持有 kernels 侧类型，暴露的 `page_indices_d()` 等是已上传的 `CudaSlice<i32>` 设备缓冲。前者是"描述"，后者是"已装填的调度数据"。

### 4.2 attention.rs：paged attention 三入口

#### 4.2.1 概念说明

[ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs) 提供三个函数，分别对应推理的两条主路径：

| 入口 | 路径 | 调度输入形态 |
|------|------|--------------|
| `prefill_attention_paged_into` | 预填充（一次吃整段 prompt，含 causal mask） | 一个 `&PrefillPagedPlan` 对象 |
| `paged_attention_batch_decode_into` | 批解码（每请求每步 1 个新 token） | 逐个传入 plan 提供的 `&CudaSlice<i32>` 数组 |
| `paged_attention_batch_decode_split_kv_into` | 长 KV 的批解码（KV 切 chunk 并行，随后归并） | decode 数组 + split-KV 专属数组 + 两块临时归并缓冲 |

三个函数都是**薄包装**：参数几乎原样转发给 `pegainfer_kernels::ops` 的同名函数，core 唯一主动做的变换是把 `&KvLayout` 换成 `&layout.kernel_layout()`——即「core 的几何描述 → kernels 的几何描述」。此外 prefill 入口把 core 的 plan 类型经 Deref 自动转成 kernels 的 plan 类型（见 4.3.3）。

注意 prefill 入口还接收 `q_norm` / `k_norm` / `cos_cache` / `sin_cache` / `rms_eps`——QK 的 RMSNorm 与 RoPE 旋转被融进这一次调用里，这是 kernels 侧融合设计在 core 签名上的直接投影（对照 u2-l3 提过的 `dflash_qk_norm_rope_into` 命名习惯：减少访存优先）。

#### 4.2.2 核心流程

prefill 路径（单入口吃掉一个 plan 对象）：

```text
调用方（模型 executor）
  ├─ 持有 KvPool → kv_buffer（数据侧）
  ├─ 持有 KvLayout → layout（几何）
  ├─ 构造/重填 PrefillPagedPlan（调度侧）
  └─ prefill_attention_paged_into(ctx, q, k, v, norms, caches,
         kv_buffer, layout, layer, plan, output, 头数, head_dim, eps)
        └─ layout.kernel_layout()   # core→kernels 的几何转换（唯一主动变换）
        └─ &plan ──Deref──▶ &kernels::ops::PrefillPagedPlan
        └─ pegainfer_kernels::ops::prefill_attention_paged_into(...)
```

batch decode 路径（plan 被"拆散"成数组传入）：

```text
plan / decode buffers 提供：
  page_indices_d    # 展平的页号表（所有请求拼接）
  page_indptr_d     # 每请求在页号表中的起始偏移（CSR）
  last_page_len_d   # 每请求最后一页的有效 token 数
  positions_d       # 每请求当前位置（RoPE 用）
  request_indices_d / kv_tile_indices_d / kv_chunk_size_d  # tile 调度
paged_attention_batch_decode_into(ctx, q/k/v 新 token, kv_buffer, layout,
    layer, 上述数组, output, num_qo_heads, batch_size)
```

split-KV 在此之上追加：`split_request_indices_d`（某槽位属于哪个请求）、`split_kv_tile_indices_d`（第几个 chunk）、`split_o_indptr_d`（部分结果落哪一行）、`split_block_valid_mask_d`（padding 槽位标 0），以及归并用的两块临时缓冲 `split_tmp_v`（bf16）与 `split_tmp_s`（f32）。kernel 分两阶段：先各 chunk 独立算出局部的 max/sum 归一化中间量，再做一次在线 softmax 归并：

\[ o = \sum_c \frac{e^{m_c - m}\, s_c}{\sum_j e^{m_j - m}} \cdot v_c,\qquad m = \max_c m_c \]

其中 \(m_c\)、\(s_c\)（softmax 分母）、\(v_c\)（加权值和）是各 chunk 的局部结果——这就是需要 `split_tmp_s`/`split_tmp_v` 两块临时缓冲的原因。

#### 4.2.3 源码精读

[attention.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L1-L9)：导入清单本身就是一张依赖表——`super::PrefillPagedPlan`（本模块的 plan）、`crate::kv_pool::KvLayout`（几何来自 KV 池）、`crate::tensor::{DeviceContext, DeviceVec, HiddenStates}`（tensor 门面）。attention 模块直接依赖 kv_pool，是「调度面横跨 ops 与 kv_pool 两章」的实证。

[attention.rs:L12-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L12-L50)：`prefill_attention_paged_into` 的签名与转发。参数可分四类：

- **本步激活**：`q_batch` / `k_batch` / `v_batch`（`&mut HiddenStates`，k 需可变因为 norm/RoPE 原地写）；
- **数据侧**：`kv_buffer: &CudaSlice<bf16>` + `layout: &KvLayout` + `layer: usize`；
- **调度侧**：`plan: &PrefillPagedPlan`（第 24 行）+ `start_pos`/`seq_len` 语义都封装在 plan 内部；
- **几何与输出**：头数三元组、`rms_eps`、`output: &mut HiddenStates`。

第 41 行 `&layout.kernel_layout()` 是整个文件里 core 最实质的一步操作。

[attention.rs:L53-L91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L53-L91)：`paged_attention_batch_decode_into`——注意签名里没有 plan 对象，而是 7 个独立的 `&CudaSlice<i32>` 调度数组（L61-L67），它们的名字与 4.3.3 里 plan 的 getter 一一对应；executor 可以从 plan 取，也可以从自己的 decode buffers 取，core 不关心。

[attention.rs:L94-L146](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L94-L146)：`paged_attention_batch_decode_split_kv_into`——最长的签名。三个值得注意的点：`row_offset: usize`（输出行偏移，多流/多段输出共用一个 output 时定位本段）；`split_tmp_v: &mut CudaSlice<bf16>` 与 `split_tmp_s: &mut CudaSlice<f32>`（可变借用：归并阶段原地写）；`split_padded_slots: usize`（把 padded 大小显式传给 kernel，配合 mask 用）。依旧逐参转发给 kernels 同名函数（L120-L145）。

#### 4.2.4 代码实践

1. **实践目标**：不借助 IDE 的类型提示，靠签名本身把三个入口的参数归类。
2. **操作步骤**：
   - 打开 [attention.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs)，为 `paged_attention_batch_decode_split_kv_into` 的全部参数做四分法标注：**激活 / KV 数据与几何 / 调度索引 / 输出与临时缓冲**。
   - 数一下调度索引类参数有几个 `CudaSlice<i32>`、几个 `CudaSlice<u8>`，并写下每个数组"每个元素属于谁"（每请求一个？每槽位一个？）。
3. **需要观察的现象**：你会发现在三个入口里，「KV 数据」始终只有 `kv_buffer` 一个参数；批次的一切差异都体现在 `i32` 索引数组的长度与内容上。
4. **预期结果**：得到一张 4 列表格；split-KV 入口的调度索引数量是 prefill 入口的数倍，直观印证"KV 越长、调度面越厚"。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prefill 入口把 plan 作为对象传入，而两个 decode 入口把调度数据拆成独立数组？

**答案**：prefill 的调度数据天然聚在一个 plan 对象里（q_indptr、tile 索引等由 plan 一次性算好），传对象最省事；decode 每步只前进一个 token，调度数据由 executor 的常驻 decode buffers 持有并逐步推进（对照 ops.rs 再出口的 `advance_decode_metadata`，以及 plan 的 `decode_metadata_d_mut()` 返回可变二元组），executor 需要灵活拼装（split-KV 的 CSR 数组来自另一处上传），拆散传数组让 kernel 封装不绑定单一持有者。

**练习 2**：三个入口的函数名都以 `_into` 结尾，这与 u2-l3 归纳的命名约定一致吗？

**答案**：一致。`_into` 表示结果写入调用方提供的缓冲（这里是 `output: &mut HiddenStates`）而不是新建返回值；这让输出缓冲可以被 executor 预分配并纳入 CUDA Graph 的指针稳定集。

### 4.3 paged_plan.rs：PrefillPagedPlan 与 SplitKvCsr

#### 4.3.1 概念说明

[paged_plan.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs) 承载两套调度数据：

- **`PrefillPagedPlan`**：预填充计划。core 侧是一个包装器（`inner: pegainfer_kernels::ops::PrefillPagedPlan`），提供四条构造路径与一组设备端数组 getter。它的杀手锏是「预分配 + 原地重填」：先按最坏情况分配（`new_preallocated`），之后每步用 `update_batch_with_cta_tile_q` 原地重填——文档注释明说"no allocation, pointers unchanged"（图稳定缓冲复用），这是 decode/prefill 混排路径能进 CUDA Graph 的前提。
- **`SplitKvCsr` + `build_split_kv_csr`**：split-KV 解码的宿主侧 CSR，纯 CPU 构造、零 GPU 依赖。它解决"批内各请求的 KV 长短不一，且都可能被切成多个 chunk"的索引问题，并统一 padding 到 `padded_bs × cap` 个槽位。

#### 4.3.2 核心流程

`build_split_kv_csr` 的算法（宿主侧，纯 CPU）：

1. 校验：`chunk_size > 0`、`cap > 0`、`kv_lens.len() <= padded_bs`。
2. 对每个请求 \(r\)，计算 chunk 数：

\[ \text{chunks}(r) = \max\left(1,\ \left\lceil \frac{\text{kv\_len}_r}{\text{chunk\_size}} \right\rceil\right) \]

（`.max(1)`：哪怕 KV 长度为 0 也保留一个 chunk，避免空请求在 CSR 里消失。）
3. 若 \(\text{chunks}(r) > \text{cap}\) 则报错——`cap` 是预分配的每请求 chunk 上限，超限说明"上下文上限配错了"（错误信息原文：context limit misconfigured）。
4. 逐 chunk 追加 `(request_idx, chunk_idx, mask=1)`，并把当前累计长度压入 `o_indptr`——标准 CSR 前缀和。
5. 把不足 `padded_bs` 的空请求的 `o_indptr` 补齐为同一累计值（空区间 `[len, len)`）。
6. 把三个定长数组 padding 到 `padded_bs * cap` 个槽位，`mask=0`、索引填 0（确定性填充）。

一个完整实例（取自 qwen3 的真实单测，见 4.3.4）：`chunk_size=64, cap=64, kv_lens=[100, 100], padded_bs=2`。每请求 \( \lceil 100/64 \rceil = 2 \) 个 chunk：

```text
request_indices = [0, 0, 1, 1, 0×124…]   # 长度 2×64=128
kv_tile_indices = [0, 1, 0, 1, 0×124…]
block_valid_mask = [1, 1, 1, 1, 0×124…]
o_indptr         = [0, 2, 4]              # 请求 0 占槽 [0,2)，请求 1 占 [2,4)
```

`PrefillPagedPlan` 的四条构造路径与用途：

| 构造入口 | 输入 | 场景 |
|----------|------|------|
| `new` | 单个 `&KvDesc` + start_pos/seq_len + 头几何 | 单请求，`cta_tile_q` 用默认（override=0） |
| `from_raw_batch_with_cta_tile_q` | 多请求的原始页号 `Vec<i32>` 列表 | 批量 prefill，executor 已有扁平化页表 |
| `new_preallocated` | 只给最坏尺寸（tokens/pages/batch/tiles 四上限） | 图稳定路径的第一步：只分配一次 |
| `update_batch_with_cta_tile_q` | 同 `from_raw_batch` 的数据 | 图稳定路径的每步：原地重填，指针不动 |

#### 4.3.3 源码精读

[paged_plan.rs:L9-L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L9-L16)：`SplitKvCsr` 的四个 `Vec` 字段与文档注释——"padded request/chunk indices, the per-slot validity mask, and the per-request chunk offsets"，一行说清三个数组的语义分工。

[paged_plan.rs:L20-L69](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L20-L69)：`build_split_kv_csr` 全函数。L41 的 `kv_len.div_ceil(chunk_size).max(1)` 是 chunk 数公式；L42-L46 的 `anyhow::ensure!` 是 cap 越界检查（错误信息带 kv_len 与 chunk_size，可诊断）；L47-L51 逐 chunk 填三个数组；L52 压入 `o_indptr` 前缀和；L54-L61 空请求补 `o_indptr`、padding 槽位填 `(0,0,0)` 且 `mask=0`。注意 L28-L32 的前置校验顺序：先验参数合法性再算 `padded_slots`。

[paged_plan.rs:L71-L95](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L71-L95)：`PrefillPagedPlan` 包装结构 + `new`。`new` 只是 `new_with_cta_tile_q(..., 0)` 的默认化（override=0 表示"用内核默认 tile"）。`new_with_cta_tile_q` 是私有的（L98，`fn` 无 `pub`）——外部想自定义 tile 只能走 batch 路径。

[paged_plan.rs:L98-L126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L98-L126)：**本文件最关键的一跳**——`KvDesc` 到 kernels plan 的桥。L108-L112 把 `desc.page_indices()`（`&[PageId]`）逐个 `p.index() as i32` 收集成 `Vec<i32>`；L114-L124 把「页号表 + `desc.last_page_len()` + start_pos/seq_len + 头几何」整体交给 kernels 的 `PrefillPagedPlan::new_with_cta_tile_q`。core 在这里完成了「宿主侧 PageId 切片 → 设备侧 i32 数组」的形态转换。

[paged_plan.rs:L128-L153](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L128-L153)：`from_raw_batch_with_cta_tile_q`——批量入口，注意它**不接收 `KvDesc`**，直接吃调用方准备好的 `&[Vec<i32>]` 页号表（kernels 侧对应 `new_batch_with_cta_tile_q`）。executor 侧的真实用法见 [pegainfer-qwen3/src/unified_forward.rs:L336](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L336)。

[paged_plan.rs:L155-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L155-L200)：`new_preallocated`（L157-L173，按 `max_total_tokens/max_total_pages/max_batch/max_tiles` 四个上限一次性分配）与 `update_batch_with_cta_tile_q`（L177-L200，原地重填）。L155-L156 与 L175 的文档注释直说动机："graph-stable buffer reuse"、"no allocation, pointers unchanged"。qwen3 的 [unified_forward.rs:L203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/unified_forward.rs#L203) 正是 `new_preallocated` 的消费点。

[paged_plan.rs:L202-L246](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L202-L246)：getter 群 + `Deref`。getter 名与 4.2 里 decode 入口的参数名严格一一对应（`page_indices_d()` / `page_indptr_d()` / `last_page_len_d()` / `request_indices_d()` / `kv_tile_indices_d()` / `kv_chunk_size_d()`，另有 prefill 专属的 `q_indptr_d()` / `qo_tile_indices_d()` / `total_num_rows_d()`）；`decode_metadata_d_mut()` 返回 `(request_indices, kv_chunk_size)` 的可变借用，供每步推进 decode 元数据。L240-L246 的 `Deref` 让 `&PrefillPagedPlan`（core 型）能自动当作 `&kernels::ops::PrefillPagedPlan` 用——4.2 的 prefill 入口能直接把 `plan` 传给 kernels 函数，靠的就是这次 deref 强制转换。

#### 4.3.4 代码实践

1. **实践目标**：验证你手算 CSR 的能力，并跑通仓库里现成的纯 CPU 测试。
2. **操作步骤**：
   - 先手算：`build_split_kv_csr(chunk_size=64, cap=4, kv_lens=[40, 100], padded_bs=3)` 的 `o_indptr` 与前几个槽位。
   - 再对照真实测试 [pegainfer-qwen3/src/batch_decode_buffers.rs:L536-L544](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L536-L544)（`padding_requests_contribute_zero_chunks`）核对：`o_indptr = [0, 1, 3, 3]`（请求 0 只有 1 个 chunk，请求 1 有 2 个，第 3 槽是 padding、贡献空区间）。
   - 有 CUDA 工具链的环境可运行（qwen3 编译会触发 kernels 的 nvcc 构建，耗时较长）：
     ```bash
     cargo test --release -p pegainfer-qwen3 --lib uniform_batch_csr -- --nocapture
     ```
     该过滤只会命中 `uniform_batch_csr` 这类纯 CPU 测试，不会触发同文件里需要 GPU 的 `shared_prefix_views_are_counted_by_reference`。
3. **需要观察的现象**：手算值与断言一致；三个 CSR 测试（[L525-L549](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L525-L549)）分别覆盖均匀批、padding 批、cap 越界三种形态。
4. **预期结果**：测试通过（或无编译环境时，以手算对照断言值完成验证；本条命令**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`new_preallocated` 之后如果实际批次比四个 max 都小，会发生什么？比 max 大呢？

**答案**：比 max 小没问题——`update_batch_with_cta_tile_q` 重填实际内容，多余部分是 padding（这正是预分配的意义）；比 max 大则重填时必然越界，qwen3 侧在更上层（如 `sync_paged_meta` 的 page-index overflow 检查，见 [batch_decode_buffers.rs:L515-L522](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L515-L522) 的同类语义）显式报错而不是让设备拷贝悄悄越界。

**练习 2**：为什么 `build_split_kv_csr` 里 `.max(1)` 是必要的？去掉它会怎样？

**答案**：若某请求 kv_len=0，`div_ceil` 得 0 个 chunk，该请求在 `request_indices` 里没有任何槽位、`o_indptr` 出现两个相等的前缀——CSR 结构上仍合法，但 kernel 侧按"每请求至少一个输出行"的假设做归并时会读到未初始化槽位；`.max(1)` 保证空请求也占一个显式的、被 mask 标记的槽位，行为确定。

**练习 3**：`PrefillPagedPlan` 为什么不直接 `pub use pegainfer_kernels::ops::PrefillPagedPlan`（像 u2-l2 的 ffi 那样一行再出口），而要包一层？

**答案**：因为 core 需要在边界上做**有内容的适配**：`new` 系列构造入口接收 core 自己的 `KvDesc`（含 `PageId → i32` 的转换，L108-L112），这是一行再出口给不了的；同时 `Deref` 又保留了"零成本当 kernels 类型用"的便利。这是「纯转发门面」与「适配器门面」的分界：attention 三入口是前者，`PrefillPagedPlan` 是后者。

### 4.4 KvDesc 对拍：core 描述状态、kernels 消费状态

#### 4.4.1 概念说明

`KvDesc<'a>` 是 KV 池交给前向代码的「单请求身份卡」，定义在 [kv_pool.rs:L427-L430](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L427-L430)：只有两个字段——`pages: &'a [PageId]`（借用的页号切片）与 `last_page_len: usize`（最后一页的有效 token 数）。它不拥有任何东西（纯借用）、不知道池缓冲在哪（页号只在签发它的池里有意义，见 `belongs_to`，[kv_pool.rs:L349-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L349-L353)）、也不含几何（stride 在 `KvLayout` 里，由入口单独传）。

「对拍」的意思是：把 `KvDesc`（最小描述）与 plan（批次调度数据）并排放，看信息是如何逐级补充的——这正是「core 描述状态、kernels 消费状态」契约的具体形状。

#### 4.4.2 核心流程

`KvDesc` 的两个生产出口（都在 `KvState` 上）：

- `desc()`（[kv_pool.rs:L390-L395](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L390-L395)）：按**当前** `seq_len` 产出描述，信任状态本身。
- `desc_for_len(seq_len)`（[kv_pool.rs:L397-L408](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L397-L408)）：按**调用方声称的长度**产出描述，但先 `ensure!` 校验持有页数恰好等于该长度所需页数（多了少了都报错），再按该长度算 `last_page_len`。用途是"本步即将写入/读取到某个长度"的预演——例如写 KV 前先拿到目标长度的描述。

信息从 desc 到 kernel 的完整对拍表（本讲的综合结论）：

| 信息 | KvDesc（单请求） | PrefillPagedPlan（批次） | SplitKvCsr（split-KV 批次） | 作用 |
|------|------------------|--------------------------|------------------------------|------|
| 页号表 | `pages: &[PageId]`（宿主借用） | `page_indices_d`（设备，展平拼接） | ——（split 阶段直接复用 decode 的页表） | 定位 KV 数据 |
| 页表行偏移 | 隐含（单个请求无需偏移） | `page_indptr_d`（CSR） | `o_indptr`（输出行 CSR） | 批次内切分归属 |
| 末页长度 | `last_page_len: usize` | `last_page_len_d`（每请求一个） | ——（由 kv_len 与 chunk_size 隐含） | 末页只算有效 token |
| 序列位置/长度 | 不含（在 `KvState::seq_len`） | start_pos/seq_len → `q_indptr_d` 等派生 | `kv_lens` 输入 | causal mask 与 RoPE |
| chunk 调度 | —— | `qo_tile_indices_d` / `kv_tile_indices_d` / `kv_chunk_size_d` | `request_indices` / `kv_tile_indices` | kernel 网格到数据的映射 |
| 有效性 | —— | 由各数组长度隐含 | `block_valid_mask`（padding 槽位=0） | padding 行确定性 |
| 几何（stride/头数） | 不含（`KvLayout` 单独传） | 构造时消费，kernel 侧另有 | —— | 数据解释方式 |

一句话总结这条链：**`KvDesc` 说"我有哪些页"，plan 补上"批次里谁在哪行、哪块归谁算"，`KvLayout` + `kv_buffer` 补上"数据在哪、怎么读"。**

#### 4.4.3 源码精读

[kv_pool.rs:L423-L446](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L423-L446)：`KvDesc` 定义与实现。文档注释点明它是 "Kernel-facing metadata"，但注意结构本身**不携带几何**（doc 里"bundles pool geometry"指的是它与 `KvLayout` 配对使用的整体观感）；`page_indices()` 是 `pub(crate)`（L443）——只有 core 内部（正是 paged_plan.rs L109）能把页号切片取出来转成 i32，模型 crate 拿到的 `KvDesc` 是不透明的，只能整体传给前向代码。

[kv_pool.rs:L310-L322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L310-L322)：两个纯函数 `pages_needed`（`div_ceil`）与 `last_page_len_for`（整除时返回 `page_size` 而非 0——恰满一页与空页的区分），是 desc 一致性的算术基础。

[kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L495-L538)：`kv_state_lifecycle` 测试，展示了 desc 随生命周期的演化：空状态 `num_pages=0, last_page_len=0`；prefill 10 token 后 `1 页, last_page_len=10`；decode 到 16 恰满一页 `last_page_len=16`（注意不是 0）；第 17 个 token 跨页后 `2 页, last_page_len=1`。这是「desc 与页分配同步推进」的行为基准。

[paged_plan.rs:L98-L126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/paged_plan.rs#L98-L126)：4.3.3 已逐行读过，从对拍视角再看一遍：`KvDesc` 的全部两个字段在这里被消费（`page_indices()` 与 `last_page_len()`），plan 的其余信息（start_pos、头几何、tile 参数）全部由调用方另行提供——即 **plan = Σ(desc) + 批次拓扑 + 几何**。

#### 4.4.4 代码实践

源码阅读型实践（对照笔记）：

1. **实践目标**：产出一张「desc ↔ plan」对照表，作为你理解调度面的长期笔记。
2. **操作步骤**：
   - 左栏：抄录 [kv_pool.rs:L427-L446](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L427-L446) 的 `KvDesc` 字段、每个字段的生产者（`desc()` 还是 `desc_for_len()`）、以及两个生产出口的差异（一个信任当前 seq_len，一个校验"持有页数 == 目标长度所需页数"后按目标长度计算）。
   - 右栏：抄录 `PrefillPagedPlan` 与 `SplitKvCsr` 的字段/getter 及其构造入口（`new` / `from_raw_batch_with_cta_tile_q` / `new_preallocated`+`update_batch_with_cta_tile_q` / `build_split_kv_csr`）。
   - 中间列：写下信息在哪一跳被补齐（如"页号 → 设备 i32 数组：L108-L112"；"末页长度 → 每请求数组：L114-L124"）。
3. **需要观察的现象**：`KvDesc` 的信息量在 plan 构造入口处被一次性"榨干"，此后 kernel 只认 plan 不认 desc。
4. **预期结果**：一张三列对照表，外加一句话结论（可用 4.4.2 的对拍表自查）。

#### 4.4.5 小练习与答案

**练习 1**：`desc()` 和 `desc_for_len(n)` 会在什么场景下给出不同的 `last_page_len`？

**答案**：当状态当前长度与目标长度落在"同一页的不同位置"时。例如 page_size=16、当前 seq_len=10：`desc()` 给 `last_page_len=10`；若本步要写 5 个 token，`desc_for_len(15)` 校验仍只需 1 页后给 `last_page_len=15`。若 `desc_for_len` 的目标长度需要的页数与持有页数不符（如持有 1 页却要 17 token），它会 `ensure!` 报错——这就是它比 `desc()` 多出来的防御。

**练习 2**（设计题，综合实践的前菜）：不迁移任何 KV 数据，把两个请求 A、B 合并成一个批次，desc 侧和 plan 侧各需要补充什么？

**答案**：desc 侧——`KvDesc` 是"单请求、连续页切片"的形状，两个请求就是两个 desc；合并意味着需要**批次的 desc 视图**：页号表拼接后的展平数组 + 每请求的行偏移（CSR 的 `indptr`）+ 每请求的 `last_page_len` 数组 + 每请求的位置（RoPE 用）——即从"结构体"升级为"数组化的同款信息"。plan 侧——`PrefillPagedPlan::from_raw_batch_with_cta_tile_q` 表明 plan 本来就是这个形状（`&[Vec<i32>]` + 各长度数组），所以"合并"只是把两个请求的页表 append 进同一批输入并重编 request 序号；真正要补的是**输出路由**（`q_indptr`/`o_indptr` 保证 A、B 的结果落回各自的行）与**池一致性**：页号只在签发它的池里有意义（`belongs_to`，[kv_pool.rs:L349-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L349-L353)），A、B 必须同池（或 batch 入口显式携带池身份）。

## 5. 综合实践

把 4.4.4 的对照笔记扩展成一份完整的「合并批次设计稿」：

1. **任务**：假设你接到一个需求——"两个已在线的请求 A（2 页，last_page_len=5）与 B（1 页，last_page_len=16，恰满页），要在下一步把它们放进同一个 split-KV 解码批次，且不许搬任何 KV 数据"。
2. **步骤**：
   - 用 4.4.5 练习 2 的结论写出：`KvDesc` 侧需要升级的字段清单（页表展平 + indptr + last_page_len 数组 + positions）。
   - 手算 `build_split_kv_csr(chunk_size=32, cap=4, kv_lens=[37, 16], padded_bs=2)` 的四个数组（A 的 kv_len=2×16+5=37；B=16），写出 `o_indptr` 与全部非 padding 槽位。
   - 指出这一步里 `KvLayout` 与 `kv_buffer` 是否需要任何变化，并说明理由（提示：数据侧共享、几何不变，这正是分离的红利）。
   - 在 [paged_attention_batch_decode_split_kv_into](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops/attention.rs#L94-L146) 的签名上勾出你会传入的每一项及其来源（desc 升级 / CSR 上传 / 常驻 buffers）。
3. **验证**：`o_indptr = [0, 2, 3]`（A 占 2 个 chunk：\(\lceil 37/32 \rceil = 2\)；B 占 1 个），`request_indices` 非零槽为 `[0, 0, 1]`，`kv_tile_indices` 为 `[0, 1, 0]`，`block_valid_mask` 为 `[1, 1, 1]` 后接 5 个 0 槽（`padded_bs × cap = 8` 槽）。若你的手算与此一致即通过。

## 6. 本讲小结

- **计划与数据分离**：KV 数据（`kv_buffer`）全批次共享一份，请求差异全部编码在 `i32` 索引数组里——数据不动、索引动，这是零拷贝调度与 CUDA Graph 兼容的共同前提。
- **三入口**：`prefill_attention_paged_into`（吃 plan 对象，norm/RoPE 融合在调用内）、`paged_attention_batch_decode_into`（吃拆散的 7 个调度数组）、`paged_attention_batch_decode_split_kv_into`（再叠加 split-KV CSR 数组与在线 softmax 归并的两块临时缓冲）；三者都是薄包装，core 的实质动作只有 `layout.kernel_layout()` 这一次几何转换。
- **两套计划**：`PrefillPagedPlan` 是适配器门面（`KvDesc → 设备 i32 数组` 的桥，Deref 直通 kernels 型），`new_preallocated` + `update_batch_with_cta_tile_q` 提供"分配一次、原地重填、指针不变"的图稳定用法；`SplitKvCsr` 是纯 CPU 的 CSR 构造，`.max(1)`、cap 校验与确定性 padding 各有讲究。
- **KvDesc 契约**：`desc()` 信任当前状态、`desc_for_len()` 校验页数后按目标长度预演；`KvDesc` 只说"我有哪些页"，几何与池缓冲由入口另行传入——core 描述状态，kernels 消费状态。
- **`belongs_to` 的提醒**：页号只在签发它的池里有意义，任何跨请求、跨池的批次拼装都必须先过池一致性这一关。

## 7. 下一步学习建议

本讲讲完了「调度面」，下一讲 u3-l1 进入它的地基：`page_pool.rs` 的固定页分配器与 `OwnedPagePermit` 的 RAII 归还语义——`KvDesc.pages` 里的每个 `PageId` 为什么一定不会泄漏，答案在那里。若你想先看消费侧全貌，可以读 `pegainfer-qwen3/src/unified_forward.rs`（`new_preallocated` 与两条 decode 入口的实际编排）与 `pegainfer-kernels/src/ops/attention.rs`（`PrefillPagedPlan` 真身的字段与 tile 划分逻辑）；u4-l1 讲 CUDA Graph 时还会回到 `new_preallocated` 的"指针不变"约束。
