# 注意力：FlashInfer paged prefill 与 batch decode

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PrefillPagedPlan` 里每一类元数据数组（页表、per-token、tile 计划）是给哪个 kernel 消费的，以及为什么它可以「一次构建、所有层共享」。
2. 独立阅读 `prefill_attention_paged_into` 的三段式 kernel 链（QK norm + RoPE → paged KV scatter → paged prefill attention），并理解 Rust wrapper 与 CUDA 入口各自的职责。
3. 区分 batch decode 的两条路径：非分区 `paged_attention_batch_decode_into` 与 split-KV 变体各自适合什么场景、参数差在哪里。
4. 解释 `SUPPORTED_GQA_GROUP_SIZES = [1, 2, 3, 4, 6, 8]` 为什么只门控「优化」（CUDA Graph decode + 专用 decode kernel）而不门控正确性——组大小不在列表里时 decode 走 eager 的 prefill 路径。

## 2. 前置知识

本讲默认你已读过 u3-l1（wrapper 标准五步）与 u3-l3（`KvStorage`/`KvFormat` 布局元数据）。在此基础上补充几个概念：

- **paged KV cache（页式 KV 缓存）**：如果不分页，每个请求要按最大序列长度预留一整块连续显存，且不同请求的公共前缀无法共享。分页后，KV 缓存被切成固定 `page_size`（本 crate 用 16）的页，请求持有一张「页表」——第 i 个 token 落在 `page_indices[i / page_size]` 页内的第 `i % page_size` 行。最后一页可能没填满，所以还要一个 `last_page_len` 记录有效行数。
- **CSR 格式（Compressed Sparse Row）**：多个请求的页表拼接存放在一个大数组里，用 `page_indptr`（长度 = 请求数 + 1）记录边界。请求 r 的页是 `page_indices[page_indptr[r]..page_indptr[r+1]]`。本讲的 `q_indptr` 同理，只是拼接的对象从页变成了 query token。
- **GQA（Grouped Query Attention）**：query 头数 `num_q_heads` 是 KV 头数 `num_kv_heads` 的整数倍，组大小 \( g = \text{num\_q\_heads} / \text{num\_kv\_heads} \)。Qwen3-4B 是 32/8 = 4 组。
- **prefill 与 decode**：prefill 一次处理整段 prompt（每个 token 都是 query，causal mask 只看自己及之前）；decode 每步每请求只有一个新 query，但要看全部历史 KV。
- **HiddenStates 列主序约定**（承接 u3-l1）：token i 连续存于偏移 \( i \times \text{hidden\_dim} \)，即 `[hidden_dim, seq_len]` 的列主序矩阵。本讲的 Q/K/V batch 和输出全部沿用这一约定。
- **CUDA Graph 与指针稳定性**：graph 回放要求 kernel 参数里的设备指针在捕获与回放之间不变。这是 `PrefillPagedPlan` 提供「预分配 + 原地重填」模式的根本动机（详见 u4-l3）。
- **FlashInfer**：vendored 的 header-only C++ 注意力库（见 u1-l3 的构建接入）。本讲的 prefill/decode attention kernel 不是手写 CUDA，而是 FlashInfer 模板的薄封装；手写的部分是 RoPE 前置和 KV scatter。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/ops/attention.rs` | Rust wrapper 层：`PrefillPagedPlan` 的构建/复用、prefill 与 decode 的完整调用链、`checked_paged_geometry` 布局校验 |
| `csrc/shared/prefill_attention.cu` | 手写 CUDA：warp 级 QK RMSNorm + RoPE kernel 及其 C 入口 |
| `csrc/shared/paged_attention.cu` | FlashInfer 封装的 C 入口：paged decode、split-KV decode、KV scatter、batch prefill、tile 尺寸查询 |
| `csrc/shared/paged_launch.cuh` | 上述 C 入口共用的启动模板：`make_paged_kv`、`decode_launch`、`decode_split_kv_launch`、`prefill_paged_launch` |
| `src/ffi/shared.rs` | Rust 侧 `extern "C"` 声明（本讲只引用注意力相关的几条） |
| `tests/prefill_plan_refill.rs` | plan「预分配 + 原地重填」的对拍门禁测试 |
| `KERNELS.md` | 算子索引：`qwen3_4b.attn.*` 与 `qwen3_4b.kv.scatter` 的登记行 |

模型侧的两个引用文件（用于讲 GQA 门控的消费端）：

| 文件 | 作用 |
|------|------|
| `../pegainfer-qwen3/src/config.rs` | 启动期探测 GQA 组是否编译，未编译则打警告 |
| `../pegainfer-qwen3/src/executor.rs` | decode 执行入口，组未编译时改道 eager 统一路径 |

## 4. 核心概念与源码讲解

### 4.1 PrefillPagedPlan：注意力元数据的「一次构建、多层共享」

#### 4.1.1 概念说明

FlashInfer 的 `BatchPrefillWithPagedKVCache` 不是只吃 Q/KV 指针——它还需要两组**设备侧元数据**：

1. **页表**：`page_indices` / `page_indptr` / `last_page_len`（CSR），描述每个请求的 KV 在页池里的位置；
2. **tile 调度计划**：`request_indices` / `qo_tile_indices` / `kv_tile_indices` 等，把「哪个 CTA 处理哪个请求的哪一段 query」预先排好。

如果每层注意力都现场计算并上传这些数组，一个 36 层的模型每步要做 36 次同样的宿主计算和 H2D 拷贝。但页表和 tile 计划**与层无关**——同一步内所有层面对的是同一批请求、同一张页表。所以 `PrefillPagedPlan` 把它们算一次、上传一次，所有层共享。这就是文档注释里「Built once per prefill call, shared across all layers」的含义。

此外它还解决第二个问题：CUDA Graph 要求指针稳定，而 `clone_htod` 每次分配新缓冲。于是有第三种构造模式——`new_preallocated` 按最坏情况分配一次，之后 `update_batch_with_cta_tile_q` 用 `memcpy_htod` 原地覆写，指针永不动。

#### 4.1.2 核心流程

一个多请求 plan 的宿主侧计算（`BatchPlanHost::compute`）大致是：

```text
输入: 每请求的 page_indices / last_page_len / start_pos / seq_len，全局头数与 head_dim

1. 校验 num_q_heads 是 num_kv_heads 的正整数倍（GQA 约束）
2. 拼接 CSR 页表:
     all_page_indices = 各请求页号顺序拼接
     page_indptr      = [0, 累计页数...]            (长度 = batch + 1)
     kv_chunk_sizes   = 每请求 start_pos + seq_len  (即该请求的 KV 总长)
3. per-token 数组（总长 = sum(seq_len)）:
     batch_indices[token] = 所属请求号（repeat_n 展开）
     positions[token]     = start_pos .. start_pos + seq_len（RoPE 绝对位置）
4. q_indptr = [0, 累计 token 数...]                  (CSR)
5. tile 计划:
     向 FFI 查询 cta_tile_q（FlashInfer 的 FA2DetermineCtaTileQ，或显式 override）
     对每个请求: packed_qo_len = seq_len * group_size
                 num_tiles_req = ceil(packed_qo_len / cta_tile_q)
     展开为 (request_indices, qo_tile_indices, kv_tile_indices=0) 三条等长数组
6. 全部 clone_htod 上传；宿主侧保留 q_indptr_host 与 num_tiles 等标量
```

tile 数量的数学：

\[ \text{num\_tiles} = \sum_{r} \left\lceil \frac{\text{seq\_len}_r \times g}{\text{cta\_tile\_q}} \right\rceil \]

其中 \( g \) 是 GQA 组大小。`cta_tile_q` 必须与 kernel 分发用的值一致，否则 Rust 算的 tile 数组和 CUDA 侧实际启动的 CTA 布局对不上——所以它不是 Rust 里重写一遍公式，而是通过 FFI 调 C 侧的 `FA2DetermineCtaTileQ` 拿回来（`resolve_prefill_cta_tile_q` 只接受 override 为 0（自动）、16、64、128，其余返回 0 触发错误）。

#### 4.1.3 源码精读

先看结构体本身，字段按「页表 / per-token / tile 计划 / 宿主侧标量」分组：

- [src/ops/attention.rs:27-56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L27-L56)：`PrefillPagedPlan` 的文档注释与字段。`_d` 后缀表示设备侧 `CudaSlice<i32>`；`q_indptr_host` 特意留在宿主侧，注释解释了原因——需要自己定 grid 的 launcher 要读它，且 batch 大小就是它的长度减一，不必再养一个字段。

`batch_size()` 是推导出来的而非存储的：

- [src/ops/attention.rs:129-141](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L129-L141)：`batch_size()` 用 `saturating_sub(1)` 从 `q_indptr_host` 推导。饱和而不是直接减是为了让「零请求」（只剩前导 0 的空 plan）得到 0 而不是回绕成负数。

两个前置校验：

- [src/ops/attention.rs:58-67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L58-L67)：`checked_head_relation` 要求 `num_q_heads` 是 `num_kv_heads` 的正整数倍——GQA 组大小是整数除法，plan 的 tile 算术和 C 侧 tile 查询都不设防。
- [src/ops/attention.rs:69-90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L69-L90)：`checked_page_metadata` 求最大页号和最大尾页长度并断言非负、尾页长度 ≥ 1——hd256 滑窗 wrapper 用这两个值对池几何做检查，kernel 内部没有设备侧越界检查。

单请求构造入口（`new_with_cta_tile_q`）的关键段落：

- [src/ops/attention.rs:161-183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L161-L183)：先跑两个校验，把所有 `usize` 过一遍 `checked_i32`（防静默截断，承接 u3-l1 的工具函数），然后上传页表三件套；`positions` 直接由 `start_pos..start_pos+seq_len` 生成——注意这是「绝对位置」，prefix cache 命中时 `start_pos > 0`。
- [src/ops/attention.rs:185-219](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L185-L219)：通过 FFI 查 `num_tiles` 和 `cta_tile_q`（两个都要求 > 0），随后把 tile 计划三条数组上传。查询函数声明在 [src/ffi/shared.rs:616-638](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L616-L638)，实现落在 C 侧 [csrc/shared/paged_attention.cu:161-204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L161-L204)——注意这两个是纯计算函数，返回 `i32` 而不是错误码，所以 Rust 侧 `unsafe` 调用后紧跟 `ensure!(> 0)`。

多请求的宿主计算全部收在 `BatchPlanHost` 里，fresh 与 refill 两条路径共用，保证永不分叉：

- [src/ops/attention.rs:457-474](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L457-L474)：`BatchPlanHost` 结构体，字段就是 4.1.2 流程的产物清单。
- [src/ops/attention.rs:506-525](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L506-L525)：CSR 页表拼接循环。注意 `ensure!(!pages.is_empty())` 的报错信息解释了为什么空页表非法：attention kernel 从请求自己的页数推导 KV 长度，零页意味着它拿到零长 KV。
- [src/ops/attention.rs:527-544](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L527-L544)：`batch_indices` 用 `repeat_n` 展开、`positions` 用区间展开、`q_indptr` 前缀和。
- [src/ops/attention.rs:546-578](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L546-L578)：tile 计划——每请求按 \( \lceil \text{seq\_len} \times g / \text{cta\_tile\_q} \rceil \) 展开 `(req_idx, tile, 0)` 三元组；`kv_tile_indices` 恒为 0（prefill 不做 KV 分区）。

「一次构建」的两种上传方式与「原地重填」：

- [src/ops/attention.rs:246-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L246-L298)：`new_batch_with_cta_tile_q`——`BatchPlanHost::compute` 后全部 `clone_htod`（新分配）。
- [src/ops/attention.rs:300-333](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L300-L333)：`new_preallocated`——按四个最坏情况容量（max_total_tokens / max_total_pages / max_batch / max_tiles）`alloc_zeros` 一次，几何字段先置零（「update before forward」）。
- [src/ops/attention.rs:335-343](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L335-L343)：`update_batch_with_cta_tile_q` 的文档注释直说设计意图：**宿主计算与 fresh 路径完全相同，只有上传方式不同**（覆写 vs 新分配），且 `memcpy_htod` 只拷 `src.len()` 个元素、容忍更大的目标缓冲。
- [src/ops/attention.rs:371-418](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L371-L418)：refill 前的容量断言逐数组检查「新 batch 的长度 ≤ 预分配容量」。
- [src/ops/attention.rs:420-453](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L420-L453)：全部走 `memcpy_htod` 原地覆写，最后刷新宿主侧标量（`num_tiles`、`total_tokens`、`max_page_index` 等）。

#### 4.1.4 代码实践

**实践目标**：用真实测试验证「refill 与 fresh 等价、且指针从不移动」，并从报错路径理解复用的安全边界。

1. 打开 [tests/prefill_plan_refill.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L1-L8)，先读模块注释：CI 只编译不运行，必须在 GPU 机器上手动跑，`PEGAINFER_REQUIRE_GPU=1` 会把「没设备」变成失败而不是跳过（这是 u9-l1 要讲的测试门禁纪律的实例）。
2. 在有 GPU 的机器上运行：

   ```bash
   PEGAINFER_REQUIRE_GPU=1 cargo test --release -p pegainfer-kernels --test prefill_plan_refill -- --nocapture
   ```

   无 GPU 时跳过本步，做纯阅读版：逐行读完测试主体 [tests/prefill_plan_refill.rs:138-184](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L138-L184)。
3. 观察 `Batch::row(first_page, kv_len, query)` 的构造方式（[tests/prefill_plan_refill.rs:36-44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L36-L44)）：它编码了「一个 KV 长度为 kv_len 的请求，最后 query 个 token 是本步的 query」——即 prefill 行（query = kv_len）与 decode 行（query = 1）混在同一个 plan 里。
4. 重点看三个断言：`assert_matches`（refill 后每个数组的前缀与 fresh plan 逐元素相等）、`pointers == pinned`（四次 refill 后所有设备指针没动过）、以及 `too_wide` 用例（[tests/prefill_plan_refill.rs:168-183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L168-L183)）：9 行请求撞 8 行容量被拒绝，且拒绝后指针没动、上一个成功 batch 的数据仍在。

**需要观察的现象**（待本地验证）：测试通过；若你把 `new_preallocated(ctx, 1024, 1024, 8, 512)` 的 `8` 改成 `9`，`too_wide` 的 `expect_err` 会拿不到错误而 panic——这直观展示容量参数就是 refill 的硬边界。

**预期结果**：`test result: ok. 1 passed`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `q_indptr_host` 要在宿主侧留一份，而其他数组只留设备侧？

**答案**：launcher（如 Qwen3 统一路径）需要在宿主侧用 query 边界来定 grid 尺寸或拼接批数据；另外 `batch_size()` 直接由它的长度减一推导，「派生而非存储」避免了一个可能与设备侧不同步的第二份事实。其他数组没有宿主侧消费者。

**练习 2**：`update_batch_with_cta_tile_q` 为什么在 `memcpy_htod` 之前先做全部容量 `ensure!`，而不是边拷边检查？

**答案**：一旦开始拷贝，缓冲就处于半新半旧的混合状态。先全部检查再统一拷贝，被拒绝的 refill 不产生任何副作用，上一个成功 batch 的数据完好——这正是测试末尾「a refused refill moved an array / still holds the last accepted batch」两条断言守护的性质。

**练习 3**：Qwen3-14B 是 40 个 query 头 / 8 个 KV 头。用 `new_batch_with_cta_tile_q`、`cta_tile_q = 64`、某请求 `seq_len = 300`，算出该请求贡献多少 tile。

**答案**：group = 40/8 = 5，packed_qo_len = 300 × 5 = 1500，num_tiles = ⌈1500/64⌉ = 24。

### 4.2 prefill_attention_paged_into：三段式 per-layer 注意力链

#### 4.2.1 概念说明

`prefill_attention_paged_into` 是模型 crate 每层注意力段调用的总入口。它把四件事排成一条流上的三次 kernel launch：

1. **QK RMSNorm + RoPE**（手写 CUDA，原地修改 q_batch/k_batch）；
2. **paged KV scatter**（把本步新产生的 K/V token 写进该层在页池里的块）；
3. **paged prefill attention**（FlashInfer `BatchPrefillWithPagedKVCache`，causal，非分区）。

V 不做 norm/RoPE，直接 scatter。三步都跑在 `active_cu_stream` 上（承接 u2-l1 的流纪律），plan 提供页表、位置和 tile 计划，层号 `layer` 只影响 KV 在页池内的偏移。

#### 4.2.2 核心流程

```text
prefill_attention_paged_into(ctx, q, k, v, norms, caches, kv_buffer, layout, layer, plan, out, ...):
  1. checked_paged_geometry(layout, layer):
       - 拒绝非 Split 格式 / 非 Bf16 存储（fp8 除外，见 u3-l3）
       - 用 derive_strides 重新推导 kv_block_len/layer_stride/page_stride 并与 layout 字段比对
       - k_offset = layer * layer_stride；v_offset = k_offset + kv_block_len
  2. 取所有设备指针（wrapper 标准五步：取指针→转类型→传流→unsafe 调用→收 Result）
  3. launch ① qk_norm_rope_batched_decode_cuda: 位置取 plan.positions_d
  4. launch ② paged_kv_scatter_cuda: 按 batch_indices/positions 散写 K/V 到页池
  5. launch ③ batch_prefill_paged_cuda_with_cta_tile_q: sm_scale = 1/√head_dim, causal
```

其中每层的 KV 在页池内的位置由布局唯一决定：

\[ k\_offset = \text{layer} \times \text{layer\_stride}, \quad v\_offset = k\_offset + \text{kv\_block\_len} \]

而页内 NHD 步长为 \( \text{kv\_strides} = [\text{page\_stride},\ \text{num\_kv\_heads} \times \text{head\_dim},\ \text{head\_dim}] \)。

#### 4.2.3 源码精读

Rust wrapper 主体：

- [src/ops/attention.rs:598-622](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L598-L622)：函数文档写明「Token positions（RoPE、scatter、attention）全部来自 plan 的 per-token position 数组，因此 chunked 或 prefix-cached prefill（start > 0）对任意 batch 都成立」。
- [src/ops/attention.rs:665-686](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L665-L686)：第一段 launch，`positions` 传 `pos_ptr`。这段代码里藏着一条重要的历史注释：曾经有一个 `batch_size == 1` 时用标量 `start_pos` 的快速路径，它会**把 prefix-cache 命中的后缀从位置 0 开始旋转**——被删掉了。读注释能学到：位置数组是唯一真源，标量捷径在这个架构里是 bug 温床。
- [src/ops/attention.rs:688-716](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L688-L716)：第二段 scatter。`src_stride_n = kv_dim`、`src_stride_h = head_dim`——源是 HiddenStates 的行主序（token 连续），散写到页池的 NHD 布局。失败时 `anyhow::bail!` 带上 `ffi_exception_message`（承接 u2-l3 的错误契约）。
- [src/ops/attention.rs:718-750](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L718-L750)：第三段 batch prefill。一次调用把 plan 的 10 个设备数组全部传下去，`padded_batch_size` 传 `plan.num_tiles`，`cta_tile_q` 传 `plan.cta_tile_q()`。
- [src/ops/attention.rs:1535-1655](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1535-L1655)：`checked_paged_geometry`——wrapper 侧的布局防线：拒绝非 `KvFormat::Split`（[L1548-1553](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1548-L1553)）、拒绝非 Bf16 存储（除非 wrapper 带 fp8 twin）、用 `derive_strides` 单一推导重验 layout 的三个 stride 字段（[L1590-1624](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1590-L1624)），最后算出 `k_offset/v_offset`。这直接消费 u3-l3 讲过的 `paged_kv.rs` 元数据。

CUDA 侧第一段——手写的 warp 级 QK norm + RoPE kernel：

- [csrc/shared/prefill_attention.cu:18-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L18-L37)：设计说明。一个 warp 负责一个 (head, token)；每 lane 持 4 个连续维度（一次 `float2` 的 64 位加载）；RMSNorm 归约用 shuffle butterfly、RoPE 的 rotate-half 伙伴用 `__shfl_xor_sync(.., 16)` 交换（lane^16 恰好是 dim±half），全程不用 shared memory。注释还记录了实测调优：四个独立 load（q/k、norm_w、cos、sin）提前发射以提升内存级并行，5090 上测得 −5% 耗时。
- [csrc/shared/prefill_attention.cu:51-64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L51-L64)：grid/block 映射——`blockIdx.x = head_global`（query 头 + KV 头拼成一个轴）、`blockIdx.y * QK_TOKENS_PER_BLOCK + threadIdx.y = token`；`is_q = head_global < num_q_heads` 决定这个 warp 服务 Q 还是 K，`base = token * dim_stride + head_local * head_dim + d0` 定位到本 lane 的 4 个元素。
- [csrc/shared/prefill_attention.cu:66-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L66-L78)：提前发射的四个独立 load；`pos = __ldg(positions + token)`，越界直接 `__trap()`（kernel 内没有静默回退）。
- [csrc/shared/prefill_attention.cu:84-99](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L84-L99)：RMSNorm——先每 lane 局部平方和，再 butterfly 归约：

  \[ \text{inv\_rms} = \text{rsqrt}\!\left(\frac{\sum_d x_d^2}{\text{head\_dim}} + \varepsilon\right) \]

  归一化后乘 norm 权重，每步都经 bf16 舍入（这是对拍测试要留容差的原因）。
- [csrc/shared/prefill_attention.cu:101-122](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L101-L122)：RoPE rotate-half——partner 由 `__shfl_xor_sync(.., 16)` 取得；低半段输出 \( x_{lo} \cos\theta - x_{hi}\sin\theta \)，高半段输出 \( x_{lo}\sin\theta + x_{hi}\cos\theta \)，最后一次 `float2` 写回原地。
- [csrc/shared/prefill_attention.cu:285-322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L285-L322)：C 入口 `qk_norm_rope_batched_decode_cuda`。注意两点：`QK_ASSERT(head_dim == 128)`（[L305-310](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L305-L310)）——warp kernel 硬编码 head_dim 128，唯一调用方 Qwen3 在任何 TP 度下 head_dim 都是 128（TP 切头数不切 head_dim），别的值是接线 bug；grid 的 y 维按 `QK_TOKENS_PER_BLOCK = 4` 个 token 一块切分（[L312-314](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/prefill_attention.cu#L312-L314)）。这个入口同时服务 decode（batch = 每请求 1 token）和 paged prefill（batch = 全部 query token），位置语义统一由 `positions` 数组承载。

CUDA 侧第三段——`prefill_paged_launch` 模板：

- [csrc/shared/paged_launch.cuh:18-24](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L18-L24)：`Variant = DefaultAttention<custom_mask=false, sliding_window=false, logits_soft_cap=false, alibi=false>`——FlashInfer kernel 变体的编译期选择，注释说明 `window_left = -1` 退化为全注意力（gemma4 的滑窗走别的变体，见 u6-l1）。
- [csrc/shared/paged_launch.cuh:33-63](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L33-L63)：`make_paged_kv` 把 Rust 传来的裸指针 + `k_offset/v_offset/stride_page` 组装成 FlashInfer 的 `paged_kv_t`，NHD 步长三元组就在这里拼出来。
- [csrc/shared/paged_launch.cuh:239-347](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L239-L347)：`prefill_paged_launch` 主体。参数装配段（[L303-313](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L303-L313)）值得逐条读：`merge_indptr = nullptr`（不做 KV 分区合并）、`o_indptr = q_indptr`（非分区路径下输出行直接落在请求边界）、`block_valid_mask = nullptr`、`padded_batch_size = num_tiles`、`partition_kv = false`。分发段（[L315-344](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L315-L344)）先 `resolve_prefill_cta_tile_q`（[L223-237](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L223-L237)，override 只认 0/16/64/128），非法值写 `last_error` 返回 -1（u2-l3 的错误契约），再经 `DISPATCH_CTA_TILE_Q` 宏实例化 `BatchPrefillWithPagedKVCacheDispatched<CTA_TILE_Q, HEAD_DIM=128, MaskMode::kCausal, Variant>`。
- C 入口本身在 [csrc/shared/paged_attention.cu:206-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L206-L224)，Rust 侧声明在 [src/ffi/shared.rs:668-694](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L668-L694)。这些 op 在 KERNELS.md 的登记行见 [KERNELS.md:28-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L28-L31)（`qwen3_4b.attn.qk_norm_rope` / `qwen3_4b.kv.scatter` / `qwen3_4b.attn.prefill_paged` / `qwen3_4b.attn.decode_paged`）。

#### 4.2.4 代码实践

**实践目标**：写一个 GQA=4 的合成页表对拍测试——同一段 Q/K/V，先走 `single_prefill_nhd_causal_into`（连续 NHD 缓存参考路径），再走 `prefill_attention_paged_into`（页池路径），断言两者输出一致。诀窍是把 RoPE 变成恒等、norm 权重置 1，让两条路径吃到**位级相同**的 Q/K，从而把差异隔离在 attention kernel 本身。

1. 在 `tests/` 下新建 `paged_prefill_parity_practice.rs`（示例代码骨架，待本地验证）：

   ```rust
   //! 示例代码：GQA=4 时 paged prefill 与连续 NHD 参考 prefill 的对拍。
   mod common;

   use half::bf16;
   use pegainfer_kernels::ops::{
       prefill_attention_paged_into, single_prefill_nhd_causal_into, PrefillPagedPlan,
   };
   use pegainfer_kernels::paged_kv::PagedKvLayout;
   use pegainfer_kernels::tensor::{DeviceContext, DeviceVec, HiddenStates};

   const NUM_Q_HEADS: usize = 8;   // GQA 组 = 8 / 2 = 4
   const NUM_KV_HEADS: usize = 2;
   const HEAD_DIM: usize = 128;    // 两条路径都只实例化了 128
   const SEQ_LEN: usize = 100;
   const PAGE_SIZE: usize = 16;
   const CTA_TILE_Q: i32 = 64;

   #[test]
   fn paged_prefill_matches_contiguous_reference() {
       let Some(ctx) = common::device_or_skip() else { return };
       let q_dim = NUM_Q_HEADS * HEAD_DIM;
       let kv_dim = NUM_KV_HEADS * HEAD_DIM;

       // 确定性输入（复用 tests/common 的 junk 生成器）
       let q_host = common::fill(1, SEQ_LEN * q_dim);
       let k_host = common::fill(2, SEQ_LEN * kv_dim);
       let v_host = common::fill(3, SEQ_LEN * kv_dim);

       // norm 权重全 1、cos 全 1 / sin 全 0 → RoPE 恒等，
       // 两条路径看到的 Q/K 就是同一个 kernel 归一化后的同一位级结果。
       let ones = DeviceVec::from_host(&ctx, &vec![bf16::from_f32(1.0); HEAD_DIM]).unwrap();
       let max_pos = 2048;
       let cos = DeviceVec::from_host(&ctx, &vec![bf16::from_f32(1.0); max_pos * HEAD_DIM]).unwrap();
       let sin = DeviceVec::from_host(&ctx, &vec![bf16::from_f32(0.0); max_pos * HEAD_DIM]).unwrap();

       // 页池：1 层、2 KV 头、hd128、页 16；7 页恰好装下 100 token
       let layout = PagedKvLayout::new(1, NUM_KV_HEADS, HEAD_DIM, PAGE_SIZE);
       let num_pages = SEQ_LEN.div_ceil(PAGE_SIZE); // 7
       let kv_buffer = ctx.stream.alloc_zeros(num_pages * layout.page_stride).unwrap();
       let page_indices: Vec<i32> = (0..num_pages as i32).collect();
       let last_page_len = SEQ_LEN - (num_pages - 1) * PAGE_SIZE; // 100 - 96 = 4

       let plan = PrefillPagedPlan::new_with_cta_tile_q(
           &ctx, &page_indices, last_page_len, /*start_pos=*/0, SEQ_LEN,
           NUM_Q_HEADS, NUM_KV_HEADS, HEAD_DIM, CTA_TILE_Q,
       ).unwrap();

       let mut q = HiddenStates::from_host(&ctx, &q_host, q_dim, SEQ_LEN).unwrap();
       let mut k = HiddenStates::from_host(&ctx, &k_host, kv_dim, SEQ_LEN).unwrap();
       let v = HiddenStates::from_host(&ctx, &v_host, kv_dim, SEQ_LEN).unwrap();
       let mut out_paged = HiddenStates::zeros(&ctx, q_dim, SEQ_LEN).unwrap();

       prefill_attention_paged_into(
           &ctx, &mut q, &mut k, &v,
           &ones, &ones, &cos, &sin,
           &kv_buffer, &layout, /*layer=*/0, &plan,
           &mut out_paged, NUM_Q_HEADS, NUM_KV_HEADS, HEAD_DIM,
           /*rms_eps=*/1e-6,
       ).unwrap();

       // 参考路径：paged 调用已把 q/k 原地归一化，HiddenStates 的
       // [token, kv_dim] 布局恰好就是 single_prefill 要的连续 NHD 缓存。
       let mut out_ref = HiddenStates::zeros(&ctx, q_dim, SEQ_LEN).unwrap();
       single_prefill_nhd_causal_into(
           &ctx, &q, /*row_offset=*/0, SEQ_LEN,
           &k, &v, &mut out_ref,
           NUM_Q_HEADS, NUM_KV_HEADS, HEAD_DIM, /*kv_len=*/SEQ_LEN,
       ).unwrap();

       let a = out_paged.to_host(&ctx).unwrap();
       let b = out_ref.to_host(&ctx).unwrap();
       let (worst, at) = common::worst_delta(&a, &b);
       assert!(worst < 5e-3, "worst delta {worst} at {at}");
   }
   ```

2. 运行：

   ```bash
   PEGAINFER_REQUIRE_GPU=1 cargo test --release -p pegainfer-kernels \
     --test paged_prefill_parity_practice -- --nocapture
   ```

**需要观察的现象**：测试通过，`worst` 打印出一个小量（两个 FlashInfer kernel 分块累加顺序不同导致；若断言阈值过紧可放宽到 1e-2 并记录实际值）。

**预期结果**：两条路径在恒等 RoPE + 单位 norm 权重下输出一致（容差内）。这正是「页表只是寻址方式、不是数值语义」的直接验证。GPU 不可用时按 u4-l2 的源码阅读模式做纸面对照：列出两条路径的 Q 指针、KV 指针、步长与 mask 模式，论证为何应当一致。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prefill_attention_paged_into` 的 `sm_scale` 是 `1/sqrt(head_dim)` 而不接收外部参数？

**答案**：这是标准缩放点积注意力的缩放因子：logits 为 \( q \cdot k / \sqrt{d} \)。本 wrapper 服务的模型族都没有非标准缩放需求，所以直接从 `head_dim` 推导；C 侧签名保留 `sm_scale` 参数是为了其他调用方（如 gemma4 线）复用同一 FFI 入口。

**练习 2**：如果同一层在同一 plan 下被调用两次（即重复 scatter 两次再 attention 两次），会发生什么？

**答案**：scatter 是幂等写（同一 token 位置写同样的值），第二次 attention 读到的 KV 不变，输出一致。但注意 plan 是「每步一次构建」的：下一次真实 prefill 步必须 refill 或重建 plan，否则页表/位置还是旧的。

**练习 3**：为什么 `checked_paged_geometry` 要用 `derive_strides` 重新算一遍 stride 再与 `layout` 字段比对，而不是直接信任字段？

**答案**：`PagedKvLayout` 的字段全是 public，安全调用方可以构造出自相矛盾的 layout（比如改了 `page_size` 却没改 `page_stride`）。矛盾布局必须在 Rust 侧、任何 unsafe launch 之前失败，否则 kernel 会用错误步长读池子，表现为难以定位的静默错值。同时「重新推导」用的是 layout 自己的同一个函数，避免第二份算法漂移。

### 4.3 batch decode 与 split-KV 变体

#### 4.3.1 概念说明

decode 时每个请求只有一个新 query token，注意力 = 一个 query 对整段历史 KV 做点积。`paged_attention_batch_decode_into` 与 prefill 共用同一张页池和同一套 scatter，但 attention 换成 FlashInfer 的 `BatchDecodeWithPagedKVCache`。

问题：非分区 decode kernel 的并行度是 `batch_size × num_kv_heads` 个 CTA。当 batch 小（比如 1-2 个请求）而上下文很长（几十万 token）时，GPU 上千个 SM 大量闲置，单个 CTA 串行扫完整个 KV。**split-KV（partition-KV）** 把每个请求的 KV 切成多个 chunk 并行算出局部 softmax 状态（部分输出 `tmp_v` 与部分 logsumexp `tmp_s`），再用一次 merge 归并——用两遍计算换并行度。这就是「低 batch、长上下文」的 decode 变体。

#### 4.3.2 核心流程

**非分区路径**（`paged_attention_batch_decode_into`）：

```text
Step 1  paged_kv_scatter_cuda: 每请求 1 个新 token 的 K/V 写入页池
Step 2  paged_attention_decode_cuda:
          partition_kv = false, o_indptr = nullptr, tmp_v = nullptr
          → 无 merge 步骤，一次 kernel 出结果
```

**split-KV 路径**（`paged_attention_batch_decode_split_kv_into`）：

```text
Step 1  paged_kv_scatter_cuda（同上）
Step 2  paged_attention_decode_split_kv_cuda:
          调用方预建 split 计划（设备数组）:
            request_indices[slot]   → 该 slot 服务哪个请求
            kv_tile_indices[slot]   → 该 slot 处理第几个 KV chunk
            o_indptr[request]       → 该请求的活跃 slot 范围（merge 用）
            block_valid_mask[slot]  → padding slot 置 0（graph 稳定性）
          tmp_v/tmp_s 承载每 chunk 的部分状态，由 FlashInfer merge
```

split 计划数组是调用方（模型 crate 的调度器）构建的，本 crate 只负责把它们接到 FFI——`kv_chunk_size_ptr` 在 split 路径里是「GPU 指针指向每请求 kv 长度」，让 kernel 能在设备上算 chunk 数。

#### 4.3.3 源码精读

Rust 侧两个 wrapper：

- [src/ops/attention.rs:1221-1244](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1221-L1244)：`paged_attention_batch_decode_into` 的文档与签名——Q/output 是 `[q_dim, batch_size]`，元数据数组按 CSR 跨请求拼接；页表三件套 + `positions_d` + `request_indices_d/kv_tile_indices_d/kv_chunk_size_d` 由调用方传入（decode 的 plan 不在本 crate，属运行时策略对象，见 u1-l1 的职责边界）。
- [src/ops/attention.rs:1279-1310](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1279-L1310)：Step 1 scatter——与 prefill 完全同一条 `paged_kv_scatter_cuda` 通路，只是 `nnz = batch_size`（每请求一行），`batch_indices` 换成 `request_indices`。
- [src/ops/attention.rs:1312-1342](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1312-L1342)：Step 2 非分区 decode 调用。
- [src/ops/attention.rs:1346-1380](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1346-L1380)：split 变体的文档与签名。注意 `row_offset` 参数：Q/K/V/output 都只在 `[row_offset, row_offset + batch_size)` 行窗口上读写——统一步（unified step）里 decode 行排在 prefill 行后面，这就是窗口存在的原因。所有 split 元数据数组都从本地行 0 开始索引。
- [src/ops/attention.rs:1381-1404](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1381-L1404)：四个 `checked_row_offset` 调用把行窗口换成字节偏移（防溢出版本的指针推进，注释解释了 release 构建 overflow-checks 关闭时 `usize::MAX` 偏移会在下次同步才以 `CUDA_ERROR_ILLEGAL_ADDRESS` 爆炸）。
- [src/ops/attention.rs:1480-1514](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L1480-L1514)：split decode 的 FFI 调用，多传 `o_indptr/block_valid_mask/tmp_v/tmp_s/padded_batch_size`。

C 侧两个 launch 模板的差异全在 params 装配：

- [csrc/shared/paged_launch.cuh:65-138](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L65-L138)：`decode_launch`——[L117-123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L117-L123) 设 `padded_batch_size = batch_size`、`o_indptr = nullptr`、`block_valid_mask = nullptr`、`partition_kv = false`，且 dispatch 时 `tmp_v = nullptr`——FlashInfer 看到空 tmp 就知道走非分区路径，不需要 merge。
- [csrc/shared/paged_launch.cuh:140-150](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L140-L150)：split-KV 的注释块，把四个 split 计划数组的语义各写了一行——这是理解该路径最好的五分钟阅读材料。
- [csrc/shared/paged_launch.cuh:151-221](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L151-L221)：`decode_split_kv_launch`——`padded_batch_size` 改为调用方给的 split slot 数（含 padding），`o_indptr/block_valid_mask` 接上，`tmp_v/tmp_s` 作为 merge 中间缓冲传进 dispatch。
- 两个 C 入口在 [csrc/shared/paged_attention.cu:14-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L14-L50)，都是对模板的一行转发，`window_left = -1`。

#### 4.3.4 代码实践

**实践目标**：通过参数对照表掌握两条 decode 路径的差异（源码阅读型实践）。

1. 打开 [src/ffi/shared.rs:775-810 附近](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L775-L830) 的两条 `extern` 声明，逐参数对照 `paged_attention_decode_cuda` 与 `paged_attention_decode_split_kv_cuda`。
2. 列出 split 版**多出**的参数并各写一句用途：`o_indptr`、`block_valid_mask`、`tmp_v`、`tmp_s`、`padded_batch_size`。
3. 再对照 C 侧 [csrc/shared/paged_launch.cuh:117-123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L117-L123) 与 [L202-207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L202-L207)，验证你写的每条用途与实现一致。
4. 最后回答：`block_valid_mask` 为什么存在？（提示：CUDA Graph 回放时 split slot 数固定，真实 slot 数随请求变化，多出来的 slot 必须被标记为无效而不是从 launch 里消失。）

**预期结果**：一张 5 行参数对照表，每行的语义都能在 C 侧实现里指出对应代码。不需要 GPU。

#### 4.3.5 小练习与答案

**练习 1**：非分区 decode 的 CTA 数是多少？batch=1、num_kv_heads=8 时会发生什么？

**答案**：`batch_size × num_kv_heads` = 8 个 CTA。现代 GPU 有上百个 SM，8 个 CTA 意味着超过九成算力闲置且每个 CTA 要串行扫完整个 KV——这正是 split-KV 变体存在的场景（注释原文：low-batch, long-context decode）。

**练习 2**：为什么 decode 的 plan 数组（`request_indices_d` 等）不由本 crate 构建，而 `PrefillPagedPlan` 在本 crate？

**答案**：decode 的 split 计划依赖运行时调度状态（哪些请求活跃、KV 多长、graph 的固定 slot 数），属于「何时算、给谁算」的策略层——按 KERNELS.md 的 Scope 归模型/运行时 crate（u1-l1 的职责边界）。prefill plan 的元数据只依赖本步输入几何，且要被 CUDA Graph 的指针稳定约束驱动成「预分配 + 重填」，做成共享基础设施所有模型线都能复用。

**练习 3**：split-KV 路径把 `kv_chunk_size_ptr` 描述为「GPU 指针 → 每请求 kv 长度」，为什么这个信息必须留在设备上？

**答案**：split slot 的有效性判定发生在 kernel 内部（每个 CTA 要知道自己 chunk 的实际长度），如果把长度搬回宿主再决定 launch 形状，就会引入 D2H 同步点并破坏 graph 回放的形状稳定性；`block_valid_mask` + 固定 slot 数正是把「变化」留在设备数据里、把「形状」留在 launch 里的组合。

### 4.4 SUPPORTED_GQA_GROUP_SIZES：只门控优化，不门控正确性

#### 4.4.1 概念说明

FlashInfer 的 decode dispatcher 按 GQA 组大小做模板实例化（`DISPATCH_GQA_GROUP_SIZE` 宏）。钉住的 FlashInfer 版本实际编译出的组大小是 `1, 2, 3, 4, 6, 8`——这就是 `SUPPORTED_GQA_GROUP_SIZES`。它是一条**能力探测常量**，不是校验规则：

- 组大小在列表内 → 走编译好的 batch decode kernel（可被 CUDA Graph 捕获）。
- 组大小不在列表内（如 Qwen3-14B 的 40/8 = 5）→ **不拒绝服务**，decode 改道 eager：用 prefill kernel 一次算一步（每请求一个 query 的「伪 prefill」），CUDA-graph decode 与 `--decode-overlap` 不可用，精度不受影响。

所以这条列表「gates an optimization, not correctness」（常量文档原话）。注意 prefill 不受此门控——`prefill_paged_launch` 的分发维度是 `cta_tile_q`（16/64/128）而非 GQA 组，任意正整数组都能跑。

#### 4.4.2 核心流程

```text
模型启动（Qwen3 为例）:
  Config::from_file 读取 num_attention_heads / num_key_value_heads
  → decode_group_is_compiled(): SUPPORTED_GQA_GROUP_SIZES.contains(heads / kv_heads)
  → 未编译: log::warn "decode runs eager ... CUDA-graph decode disabled"

每步 decode:
  execute_decode(plan):
    if !decode_group_is_compiled():
        改道 execute_unified(prefill_requests = [], decode_requests = plan.requests)
        （eager prefill 路径，无 graph）
    else:
        正常 paged batch decode（可 graph 捕获）
```

#### 4.4.3 源码精读

- [src/ops/attention.rs:16-21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L16-L21)：常量定义与完整文档注释——「a ratio outside it is not refused — it takes the eager fallback — so this list gates an optimization, not correctness. Keep it in step with the pinned FlashInfer.」最后一句是维护约定：升级 FlashInfer 时要同步这条列表。
- [src/ops.rs:51-62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L51-L62)：`pub use attention::*` 的再导出清单——本讲所有入口（`PrefillPagedPlan`、`prefill_attention_paged_into`、两个 decode wrapper、`single_prefill_*`）都从这里出，模型 crate 看到的路径是 `pegainfer_kernels::ops::`（core 再经 `pegainfer_core::ops` 转发常量）。
- 消费端一（启动探测）：[../pegainfer-qwen3/src/config.rs:232-248](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/config.rs#L232-L248)——`decode_group_is_compiled()` 直接 `contains` 查表；未编译打警告，注明「decode runs eager through the prefill path (CUDA-graph decode disabled, --decode-overlap unavailable)」。注释还点出 GQA 比是 TP 不变量（TP 同时切分子数与 KV 头数，比值不变），所以用全局头数判断即可。
- 消费端二（执行分支）：[../pegainfer-qwen3/src/executor.rs:2629-2639](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L2629-L2639)——`execute_decode` 开头：组未编译就把 decode 请求塞进 `execute_unified`（prefill 请求为空），返回统一结果。graph 捕获处也有对应跳过（executor.rs:1454-1455 的 `enable_cuda_graph && decode_group_is_compiled()`）。
- 交叉证据：[tests/prefill_plan_refill.rs:17-19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L17-L19) 的常量注释特意选了 40/8（Qwen3-14B，组 = 5，无编译 decode kernel）并写明「Qwen3's uncompiled GQA groups refill one plan per unified step」——GQA=5 的模型只能走 eager 统一路径，而统一路径每步 refill 一个 plan，这正好把 4.1 的 plan 复用机制变成它的生死门禁测试。

#### 4.4.4 代码实践

**实践目标**：追踪一次完整的「GQA 组未编译 → eager 回退」决策链（纯源码阅读，无需 GPU）。

1. 从 [src/ops/attention.rs:21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L21) 出发，用编辑器跳转追 `SUPPORTED_GQA_GROUP_SIZES` 的全部消费者（kernels → core → qwen3/qwen35 的 config）。
2. 对三个模型算一遍组大小并判定走哪条路：Qwen3-4B（32/8）、Qwen3-14B（40/8）、你任选的一个假想模型（如 48/8）。
3. 在 `pegainfer-qwen3` 里找到 `execute_unified` 的定义，确认 decode-only 的统一步里 prefill 请求为空时注意力走的是哪条 kernel（应能指回 `batch_prefill_paged_cuda` 家族）。

**预期结果**：一条三跳链 `ops 常量 → config 探测 → executor 分支`，以及三个判定：4 → 编译路径；5 → eager；6 → 编译路径（6 在列表内）。

#### 4.4.5 小练习与答案

**练习 1**：为什么「prefill 不受 GQA 门控」？从分发维度回答。

**答案**：prefill 的模板分发维度是 `cta_tile_q`（16/64/128）与 HEAD_DIM（128），GQA 组只进入 `packed_qo_len = seq_len × group` 的长度计算，不选择模板实例；decode dispatcher 则按组大小实例化专用 kernel，只有列表内的组有编译产物。

**练习 2**：升级 FlashInfer 后新版本多编译了组 12，需要改哪些地方？

**答案**：改 [src/ops/attention.rs:21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L21) 的列表加上 12，并确认新 FlashInfer 真的为组 12 编译了实例（常量注释的「Keep it in step with the pinned FlashInfer」）；模型侧无需改动，能力探测自动生效（Qwen4 的 QSA bring-up 文档就记录过组 12 不在列表导致的 eager 决策）。

**练习 3**：一个用户抱怨 Qwen3-14B decode 吞吐比 Qwen3-4B 低得多，日志里有本讲的警告。你怎么解释？

**答案**：14B 的 GQA 组 = 5 不在编译列表，decode 走 eager 统一路径：无 CUDA Graph（每步真实 launch 全部 kernel）、无 `--decode-overlap`、且每步要 refill plan 并走 prefill 形状的注意力。精度相同但延迟与吞吐显著劣化——这是能力探测门控「优化而非正确性」的完整代价面。

## 5. 综合实践

把本讲四个模块串成一个端到端的小任务（在有 GPU 的机器上完成；无 GPU 则全部转为源码阅读并逐项写结论）：

**任务：为一个 GQA=4、head_dim=128 的假想层写一份「plan → prefill → decode」的调实验报告。**

1. **plan 层**：用 `new_preallocated` 建一个 (max_tokens=512, max_pages=64, max_batch=8, max_tiles=128) 的 plan，先 refill 一个「3 个请求、混合 prefill/decode 行」的 batch，再 refill 一个「5 个请求全 decode」的 batch，仿照 [tests/prefill_plan_refill.rs:103-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/prefill_plan_refill.rs#L103-L110) 用 `device_ptr` 断言两次 refill 之间指针未移动。
2. **prefill 层**：完成 4.2.4 的对拍测试并记录 `worst_delta`。
3. **decode 层**：在同一页池上追加 1 个 token（构造新的页表三件套：如果尾页没满则 `last_page_len + 1` 且页数不变；满了则加一页、`last_page_len = 1`），调用 `paged_attention_batch_decode_into`，把输出与你手算（或 NumPy 复现）的 单 query 注意力对比——用 `to_host` 拉回宿主验证 softmax 公式 \( \text{out} = \mathrm{softmax}(qK^\top/\sqrt{d})V \)。
4. **报告**：写清每步的输入 shape、用了哪个 FFI 符号（对照 KERNELS.md 的 `qwen3_4b.attn.*` 行）、以及 plan 复用的正确性条件清单：
   - refill 前所有容量检查通过（否则整体拒绝、无副作用）；
   - 缓冲指针在分配后永不再变（graph 可捕获）；
   - 宿主元数据每步完全重算（fresh 与 refill 共用 `BatchPlanHost::compute`，不会漂移）;
   - 消费方只读数组前缀，行数由 `total_num_rows`/`num_tiles` 驱动，多余尾元素从不被读。

## 6. 本讲小结

- `PrefillPagedPlan` 把页表、per-token 位置与 tile 调度计划一次构建、全层共享；三种构造模式里「预分配 + `memcpy_htod` 原地重填」为 CUDA Graph 保住了指针稳定性，容量检查先于任何拷贝，拒绝无副作用。
- `prefill_attention_paged_into` 是三段式链条：手写 warp 级 QK RMSNorm+RoPE（positions 数组是唯一位置真源）→ `paged_kv_scatter` 写页池 → FlashInfer `BatchPrefillWithPagedKVCache`（causal、非分区、按 `cta_tile_q` 分发）。
- decode 有两条 paged 路径：非分区版 `partition_kv=false`、`tmp_v=nullptr` 一步出结果；split-KV 版用调用方预建的 slot 计划（`o_indptr`/`block_valid_mask`/`tmp_v`/`tmp_s`）把长上下文低 batch 的 KV 切块并行再 merge。
- `SUPPORTED_GQA_GROUP_SIZES = [1,2,3,4,6,8]` 是能力探测常量：只决定 decode 走编译 kernel（可 graph 化）还是 eager prefill 路径，不影响正确性；prefill 按 `cta_tile_q` 分发、不受此门控。
- wrapper 层的防线模式贯穿始终：`checked_head_relation`/`checked_page_metadata`/`checked_paged_geometry`/`checked_row_offset` 把矛盾输入挡在 unsafe launch 之前，错误经 `ffi_exception_message` 折叠成 `anyhow::Error`。

## 7. 下一步学习建议

- **u3-l6（norm 家族）**：本讲的 warp kernel 里已经见过一次 RMSNorm 的 shuffle butterfly 归约；下一讲系统对比 FlashInfer 委托与自研 norm，并精读 `rms_norm_gated_kernel` 的 grid/block 映射。
- **u4-l3（CUDA Graph 安全）**：本讲反复出现的「指针稳定」「graph-stability padding slot」「`block_valid_mask`」将在那一讲获得完整的 capture/replay 视角。
- **u5-l1（qwen35 混合注意力）**与 **u6-l1（gemma4 滑窗/全局注意力）**：看其他模型线如何复用或绕开本讲的共享层——qwen35 的 hd256 特化与 gemma4 的 `WindowVariant`（`sliding_window=true`）是两条不同的演化路径。
- 继续阅读源码的建议：精读 [csrc/shared/paged_launch.cuh](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L1-L407) 全文（本讲只读了三个模板），再追一次 vendored FlashInfer 头文件里 `BatchPrefillWithPagedKVCacheDispatched` 的宏展开，验证 `DISPATCH_CTA_TILE_Q` 与 `DISPATCH_GQA_GROUP_SIZE` 的实例化集合。
