# 统一参数结构 params.h

## 1. 本讲目标

上一篇（u2-l1）我们画出了从 Python 到 CUDA kernel 的完整调用链，知道接口函数 `csrc/api/*.h` 是连接 pybind 绑定与底层 kernel 命名空间的「中间层」。这一篇就来拆开这个中间层的**数据契约**——`csrc/params.h`。

`params.h` 只有一个职责：定义一组**纯数据结构（POD struct）**，把 kernel 启动所需的全部信息（形状、指针、stride、split-KV 缓冲、调度元数据、stream）打包成一个对象，从接口函数一路透传到 kernel 内部。

学完本讲，你应当能够：

1. 说出 `params.h` 里 6 个核心结构（`DecodingSchedMeta`、`DenseAttnDecodeParams`、`SparseAttnDecodeParams`、`CombineParams`、`GetDecodeSchedMetaParams`、`SparseAttnFwdParams`）各自承载哪些字段、被谁消费。
2. 区分三类字段：**指针字段**（指向哪块显存）、**stride 字段**（步长以「元素」还是「字节」计）、**split-KV 字段**（局部累加缓冲如何挂接）。
3. 理解 `ModelType`（V32 / MODEL1）与 `SparseAttnFwdMode`（Prefill / DecodeWithSplitKV）两个枚举如何编码「量化布局」与「同一 kernel 的两种运行模式」，并看懂 `SparseFwdArgT` 这个类型别名。
4. 能够拿起 `DenseAttnDecodeParams`，逐字段倒推它在 `csrc/api/dense_decode.h` 里是由哪个张量、哪一维 stride 填进去的。

> 本讲**只读数据结构的定义与赋值**，不展开 kernel 内部如何使用这些字段（那是 u3/u4/u5/u6 的任务）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：为什么用一个 struct，而不是把参数逐个传给函数？**

CUDA kernel 的启动参数动辄二三十个：张量指针、各维 stride、batch size、head 数、scale、causal 开关、split 缓冲……如果全部塞进函数签名，既难读又容易写错顺序。FlashMLA 的做法是：在接口函数（`csrc/api/*.h`）里**校验张量、装配一个 `params` 结构体**，再把这个结构体按值（或按 const 引用）丢给 kernel 启动函数。这样 kernel 侧只接收一个对象，字段有名有姓，新增字段也只需改结构体，不必改函数签名。

**直觉二：什么是一个张量的「stride」？**

PyTorch 里每个张量底层是一块连续显存 + 一组「步长」（stride）。对一个形状为 `[b, s, h, d]` 的张量 `T`，`T.stride(i)` 表示：**沿第 i 维走一步，需要跨过多少个元素**（注意是元素数，不是字节数）。例如 `T` 是 contiguous 的，则 `stride(3)=1`、`stride(2)=d`、`stride(1)=h*d`、`stride(0)=s*h*d`。

FlashMLA 的 stride 字段几乎全部来自 `tensor.stride(i)`，意义就是「在显存里沿某一维跳一格要跨多少元素」。理解了 stride，就能在结构体里把「逻辑维度」与「物理布局」对应起来。

**直觉三：什么是 split-KV（拆分 KV）？**

解码阶段每个 query 要对很长的 KV 序列做 attention。一块 GPU 上单次放不下、或放得下但算不满时，FlashMLA 把 KV 序列切成多段（多个 split），每段各自做一遍 online softmax、产出一个**局部的** `o`（部分和）与 `lse`（局部 log-sum-exp），写进一块累加缓冲；最后由 combine kernel 把这些局部结果按数学公式合并成全局结果。因此你会看到 `params` 里反复出现 `o_accum` / `lse_accum` / `num_splits` 这一类「拆分与归并」字段。这块的原理在 u4-l1/u4-l2 详讲，本讲只需记住：**这些字段是 split-KV 流水线需要的中转缓冲**。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再辅以三个「装配现场」。

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| [csrc/params.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h) | 定义 6 个参数结构 + 2 个枚举 + 2 个模板辅助 | 主角，逐结构精读 |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | dense 解码接口，装配 `DenseAttnDecodeParams` / `CombineParams` / `GetDecodeSchedMetaParams` | 实践任务的主现场 |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | sparse 解码接口，装配 `SparseAttnDecodeParams` | 对比 sparse 字段如何填充 |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | sparse prefill 接口，装配 `SparseAttnFwdParams` | 看 `SparseAttnFwdMode` 的使用 |

> 通用约定先记在这里：`params.h` 里**所有 stride 都以「元素」为单位**（不是字节）；指针一律带 `__restrict__` 提示编译器做别名优化；可空张量（如 `attn_sink`、`topk_length`）用裸指针 + 约定「`nullptr` 表示禁用」，而不是 `std::optional`。

## 4. 核心概念与源码讲解

### 4.1 DecodingSchedMeta：tile 调度的紧凑元数据

#### 4.1.1 概念说明

`DecodingSchedMeta` 是本讲里**最小、却被引用最多**的结构。它不属于任何单个 kernel 的输入，而是 **tile scheduler**（任务切分器）的产出物：在真正跑 attention kernel 之前，先有一个小 kernel（`get_decoding_sched_meta`）把整个 batch 的 KV 工作量均衡地切给若干个「SM part」，每个 SM part 拿到一份 `DecodingSchedMeta`，记录「我负责哪些请求的哪些 KV 块」。

为什么单独定义这么个结构？因为解码是「一个请求一个长序列」，不同请求长度差异大，必须显式做负载均衡，并把均衡结果用一个紧凑、定长的对象存下来，供主 kernel 与 combine kernel 共享。你会在 `DenseAttnDecodeParams`、`SparseAttnDecodeParams`、`CombineParams`、`GetDecodeSchedMetaParams` 四个结构里都看到 `tile_scheduler_metadata_ptr` 字段——它们指的都是同一片 `DecodingSchedMeta` 数组。

#### 4.1.2 核心流程

```
GetDecodeSchedMetaParams（输入：batch、seqlens、topk…）
        │  run_get_decoding_sched_meta_kernel
        ▼
tile_scheduler_metadata[]   ← 一片 DecodingSchedMeta[num_sm_parts]
        │
        ├──► 主 attention kernel：每个 SM part 读自己的 meta，知道自己算哪段 KV
        └──► combine kernel：读 meta + num_splits，知道每个请求有几段要合并
```

每个 `DecodingSchedMeta` 描述一个 SM part 的「工作区间」。

#### 4.1.3 源码精读

结构定义本身极短：

[csrc/params.h:10-17](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L10-L17)

```cpp
struct __align__(4*8) DecodingSchedMeta {
    int begin_req_idx, end_req_idx;     // Both inclusive
    int begin_block_idx, end_block_idx; // Inclusive, exclusive
    int begin_split_idx;
    int is_first_req_splitted, is_last_req_splitted;
    int _pad[1];
};
static constexpr int DecodingSchedMetaSize = sizeof(DecodingSchedMeta);
```

逐字段含义：

- `begin_req_idx` / `end_req_idx`：这个 SM part 负责的**请求编号区间**，两端都 inclusive（闭区间）。
- `begin_block_idx` / `end_block_idx`：在 `begin_req_idx` 和 `end_req_idx` 这两个**边界请求**里，分别从第几个 KV 块开始/结束（左闭右开）。中间的请求则是整段都归这个 part。
- `begin_split_idx`：这个 part 产出的第一段局部结果，应写入累加缓冲的第几个 split 槽位。
- `is_first_req_splitted` / `is_last_req_splitted`：边界请求是否被「劈」给了相邻的 part（一个请求的 KV 被两个 part 分担）。这决定主 kernel 是否需要把同一请求的 attention 拆成两次 online softmax。
- `_pad[1]`：把结构补齐到 8 个 `int`。

两个工程细节值得记下：

1. `__align__(4*8)` 即 32 字节对齐。这是为了 **GPU 上一次 32 字节的事务性读/写**（transaction）能一次搬完整个结构，配合 SM100 上 cluster transaction barrier 使用。
2. `DecodingSchedMetaSize` 是编译期常量。它被 Python 侧用来算 metadata 张量的列数——见 [dense_decode.h:84](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L84) 的 `KU_CHECK_SHAPE(tile_scheduler_metadata, num_sm_parts, DecodingSchedMetaSize/sizeof(int))`：因为 metadata 张量 dtype 是 `int32`，所以列数 = `DecodingSchedMetaSize / 4`。

#### 4.1.4 代码实践

**实践目标**：确认 `DecodingSchedMeta` 的「物理体积」与 Python 侧 metadata 张量形状的对应关系。

**操作步骤**（源码阅读型，无需 GPU）：

1. 数一数 `DecodingSchedMeta` 有几个 `int` 字段（含 `_pad`），算出 `sizeof`。
2. 打开 [dense_decode.h:96](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L96)，看 metadata 张量怎么分配：`torch::empty({num_sm_parts, sizeof(DecodingSchedMeta)/4}, ...dtype(torch::kInt32))`。
3. 对比 [u1-l4](u1-l4-python-api-quickstart.md) 里 `FlashMLASchedMeta.tile_scheduler_metadata` 的形状。

**需要观察的现象 / 预期结果**：`DecodingSchedMeta` 共 8 个 `int` → 32 字节 → metadata 张量形状为 `[num_sm_parts, 8]`、dtype `int32`。`DecodingSchedMetaSize/sizeof(int) = 8`。这正是 `flash_mla_interface.py` 里 `tile_scheduler_metadata` 列数为 8 的来源。

#### 4.1.5 小练习与答案

**练习 1**：如果未来给 `DecodingSchedMeta` 新增一个 `int` 字段（不加 `_pad`），`sizeof` 会变成多少？Python 侧需要同步改什么？

**答案**：新增一个 `int` 后字段变为 8 个，`sizeof = 32` 字节恰好不变（原 `_pad[1]` 被新字段替代）；若新增后超过 8 个 `int`，则 `sizeof` 变为 36，破坏 32 字节对齐，需调整 `__align__` 并把 `_pad` 补齐。Python 侧 `tile_scheduler_metadata` 的列数由 `sizeof/4` 计算，只要用 `DecodingSchedMetaSize` 这一常量推导就不用改；但若硬编码了 `8`，则必须同步更新。

**练习 2**：`begin_block_idx` 是 inclusive 还是 exclusive？`end_block_idx` 呢？

**答案**：源码注释写明 `begin_block_idx` 与 `end_block_idx` 是 *Inclusive, exclusive*，即左闭右开——`[begin_block_idx, end_block_idx)`。而 `begin_req_idx`/`end_req_idx` 是 *Both inclusive*（双闭）。

---

### 4.2 DenseAttnDecodeParams 与 SparseAttnDecodeParams：解码参数双结构

#### 4.2.1 概念说明

解码（decode）是 FlashMLA 的主战场。它有两条路径——**dense**（dense decode，仅 SM90）与 **sparse**（FP8 sparse decode），分别由 `DenseAttnDecodeParams` 和 `SparseAttnDecodeParams` 承载参数。

这两个结构**字段高度相似**（都是「输入指针 + 输出指针 + stride + split-KV 缓冲 + 调度元数据 + stream」），但有三个关键差异需要牢记：

1. **stride 的整数类型不同**：dense 用 `index_t = int64_t`（见 [params.h:20](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L20)），sparse 用普通 `int`（[params.h:85-91](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L85-L91)）。这是因为 dense 的 KV cache（分页池）可能非常大，batch×序列×head×head_dim 的乘积会超过 `int32` 上限（约 21 亿），必须用 64 位 stride；sparse 走 token-level 索引、规模更小，用 32 位更省寄存器。（接口侧的 `int64_stride_to_int` 转换会在 sparse 接口里先做溢出保护，见 u2-l3。）
2. **KV cache 的 dtype 不同**：dense 的 KV 与 Q 同 dtype（`bf16`/`fp16`），指针用 `void*`；sparse 的 KV 是 **FP8**，Q/out 用 `cutlass::bfloat16_t*`（[params.h:71-72](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L71-L72)）。
3. **sparse 多了 `indices` / `topk` / `extra_*` / `model_type` 等 sparse 专属字段**，对应 token-level 稀疏索引与可选的「额外 KV 池」。

> 一个贯穿 MLA 的硬约束：`head_dim_k = 576`（含 64 维 RoPE）、`head_dim_v = 512`。dense 的 `d`/`d_v`、sparse 的 `d_qk`/`d_v` 都直接来自这两个常量，接口函数里会 `TORCH_CHECK` 强制校验（见 [dense_decode.h:60-61](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L60-L61)）。

#### 4.2.2 核心流程

解码参数的「装配」流程（dense 与 sparse 同构）：

```
接口函数 sparse_attn_decode_interface / dense_attn_decode_interface
   │  1. 校验 dtype / shape / device / stride
   │  2. 推导 num_sm_parts（按 SM 数 / head 数 / s_q 等分）
   │  3. 首次调用时：分配 metadata + num_splits，跑 get_sched_meta kernel
   │  4. 装配 *AttnDecodeParams（填指针、stride、scale、split 缓冲）
   ▼
   主 attention kernel：读 params，每 SM part 处理一段 KV，写 o_accum/lse_accum
   ▼
   装配 CombineParams → combine kernel：归并各 split，写最终 out/lse
```

#### 4.2.3 源码精读

**先看 dense** —— [params.h:19-61](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L19-L61)。它分四组：

```cpp
// (1) 形状与标量
int b;              // batch size
int s_q;
int q_seq_per_hk;   // 每个 KV head 对应多少个 (q_token × q_head)，= h_q/h_k * s_q
int d, d_v;         // K/V dimension（576 / 512）
int h_q, h_k;
int num_blocks;     // 分页池总块数
int q_head_per_hk;  // = h_q / h_k
bool is_causal;
float scale_softmax, scale_softmax_log2;
```

注意 `q_seq_per_hk`：dense decode 把 Q 的 head 维与 seq 维「合并」看待（见下文 dense_decode.h 的 reshape）。`scale_softmax_log2 = scale * log2(e)`，因为 kernel 内部 softmax 在 log2 域做，省一次除法。

```cpp
// (2) 指针（void* 统一承载 bf16/fp16）
void *__restrict__ q_ptr, *__restrict__ k_ptr, *__restrict__ o_ptr;
float *__restrict__ softmax_lse_ptr;
```

```cpp
// (3) stride（int64，单位是元素）
index_t q_batch_stride, k_batch_stride, o_batch_stride;
index_t q_row_stride,   k_row_stride,   o_row_stride;
index_t q_head_stride,  k_head_stride,  o_head_stride;
```

```cpp
// (4) 分页 / 调度 / split-KV
int *__restrict__ block_table;       index_t block_table_batch_stride;
int page_block_size;
int *__restrict__ seqlens_k_ptr;
DecodingSchedMeta *__restrict__ tile_scheduler_metadata_ptr;
int num_sm_parts;
int *__restrict__ num_splits_ptr;
int total_num_splits;
float *__restrict__ softmax_lseaccum_ptr;   // [total_num_splits, h, q_seq_per_hk]
float *__restrict__ oaccum_ptr;             // [total_num_splits, h, q_seq_per_hk, d_v]
cudaStream_t stream;
```

`block_table` + `page_block_size` + `seqlens_k_ptr` 三者共同描述**分页 KV cache**：KV 存在一个 `[num_blocks, page_block_size, h_k, d]` 的池子里，每个请求用 `block_table[b]` 这张「页表」把自己的逻辑块序号映射到物理块，`seqlens_k[b]` 给出该请求的真实长度。

**再看 sparse** —— [params.h:63-103](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L63-L103)。在 dense 的骨架上，sparse 新增了：

```cpp
int d_qk, d_v;
int num_blocks, page_block_size, topk;
ModelType model_type;                       // 决定 FP8 字节布局
cutlass::bfloat16_t* q;                     // [b, s_q, h_q, d_qk]
cutlass::bfloat16_t* kv;                    // [num_blocks, page_block_size, d_qk]（FP8）
int* indices;                               // [b, s_q, topk]  token-level 稀疏索引
int* topk_length;   // [b], may be nullptr
float* attn_sink;   // [h_q], may be nullptr
// extra KV 池（可选）
int extra_num_blocks, extra_page_block_size, extra_topk;
cutlass::bfloat16_t* extra_kv;              // [extra_num_blocks, extra_page_block_size, d_qk]
int* extra_indices; int* extra_topk_length;
```

`sparse` 还**显式标注了每个张量的逻辑形状注释**（`// [b, s_q, h_q, d_qk]` 等），这是读 sparse kernel 最关键的「地图」。注意 sparse 的 stride 字段是普通 `int`（[params.h:85-91](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L85-L91)），且 split-KV 字段集中在结构末尾（[params.h:95-102](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L95-L102)）。

> sparse 接口怎么填充这些字段？见 [sparse_decode.h:385-415](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L385-L415)——用聚合初始化（aggregate init）一次性按声明顺序填满。可空张量统一用 `ku::get_optional_tensor_ptr<T>(opt)` 取裸指针（`nullopt` 时返回 `nullptr`），这正是「用 nullptr 表示禁用」约定的落地。

#### 4.2.4 代码实践

**实践目标**：选定 `DenseAttnDecodeParams`，逐字段倒推它在 [dense_decode.h:126-173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L126-L173) 中由什么填入。这是本讲的主实践。

**操作步骤**（源码阅读型）：

1. 先读 [dense_decode.h:55-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L55-L78)，理解 Q 被重排成什么形状。关键两行：

   ```cpp
   const int q_seq_per_hk = seqlen_q_ori * num_q_heads_per_hk;
   q = q.view({batch, seqlen_q_ori, num_heads_k, num_q_heads_per_hk, head_size_k})
        .transpose(2,3).reshape({batch, q_seq_per_hk, num_heads, head_size_k});
   ```
   
   即 Q 最终形状为 `[b, q_seq_per_hk, num_heads_k, head_size_k]`，输出 `out` 形状为 `[b, num_heads_k, q_seq_per_hk, head_size_v]`（[dense_decode.h:90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L90)）。

2. 对照下表，把每个字段填一遍（**这是实践交付物**）：

| `DenseAttnDecodeParams` 字段 | 类型 | 赋值来源（dense_decode.h） | 说明 |
|---|---|---|---|
| `b` | int | `batch_size`（sizes[0]） | 来自 Q 的第 0 维 |
| `s_q` | int | `seqlen_q_ori`（sizes[1]） | 原始 query 序列长 |
| `q_seq_per_hk` | int | `q_seq_per_hk`（L74 推导） | reshape 后 Q 的「行」数 |
| `d` / `d_v` | int | `head_size_k` / `head_size_v` | 576 / 512 |
| `h_q` / `h_k` | int | `num_heads_q` / `num_heads_k` | Q head / KV head |
| `is_causal` | bool | 入参 `is_causal`（L71：`s_q==1` 时强制 false） | 单 token 解码无需因果掩码 |
| `scale_softmax` | float | 入参 `softmax_scale` | |
| `scale_softmax_log2` | float | `softmax_scale * M_LOG2E`（L139） | log2 域缩放 |
| `q_ptr` | void* | `q.data_ptr()`（重排后的 Q） | bf16/fp16 统一用 void* |
| `k_ptr` | void* | `kcache.data_ptr()` | 分页 KV 池首地址 |
| `o_ptr` | void* | `out.data_ptr()`（L90 新建） | 输出 |
| `softmax_lse_ptr` | float* | `lse.data_ptr<float>()`（L91） | [b, h, q_seq_per_hk] |
| `q_batch_stride` | int64 | `q.stride(0)` | 跨 batch 的元素步长 |
| `k_batch_stride` | int64 | `kcache.stride(0)` | 跨「物理块」的步长 |
| `o_batch_stride` | int64 | `out.stride(0)` | |
| `q_row_stride` | int64 | `q.stride(1)` | Q 第 1 维 = q_seq_per_hk |
| `k_row_stride` | int64 | `kcache.stride(1)` | = page_block_size 维 |
| `o_row_stride` | int64 | `out.stride(2)` | out 第 2 维 = q_seq_per_hk |
| `q_head_stride` | int64 | `q.stride(2)` | = num_heads_k 维 |
| `k_head_stride` | int64 | `kcache.stride(2)` | = num_heads_k 维 |
| `o_head_stride` | int64 | `out.stride(1)` | out 第 1 维 = num_heads_k |
| `block_table` | int* | `block_table.data_ptr<int>()`（L156） | 页表 |
| `block_table_batch_stride` | int64 | `block_table.stride(0)` | |
| `page_block_size` | int | `kcache.size(1)`（=64，L67 强制） | 每块 64 token |
| `seqlens_k_ptr` | int* | `seqlens_k.data_ptr<int>()` | 每请求真实 KV 长 |
| `tile_scheduler_metadata_ptr` | `DecodingSchedMeta*` | `tile_scheduler_metadata->data_ptr()`（L160） | 首次调用时分配并填充 |
| `num_sm_parts` | int | L78 公式推导 | SM 数 / KV head / tile 数 |
| `num_splits_ptr` | int* | `num_splits->data_ptr<int>()` | [b+1]，split 数前缀和 |
| `total_num_splits` | int | `batch_size + num_sm_parts`（L164） | 累加缓冲第 0 维上界 |
| `softmax_lseaccum_ptr` | float* | `lse_accum.data_ptr<float>()`（L165 新建） | [total_num_splits, h, q_seq_per_hk] |
| `oaccum_ptr` | float* | `out_accum.data_ptr<float>()`（L166 新建） | [total_num_splits, h, q_seq_per_hk, d_v] |
| `stream` | cudaStream_t | `at::cuda::getCurrentCUDAStream().stream()`（L173） | |

**需要观察的现象 / 预期结果**：

- 三类字段一目了然——**指针字段**全部 `.data_ptr()`；**stride 字段**全部 `tensor.stride(i)`（元素单位，dense 用 int64）；**split-KV 字段**（`lse_accum`/`out_accum`/`total_num_splits`）在 [dense_decode.h:164-171](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L164-L171) 一次性分配并挂接。
- `total_num_splits = b + num_sm_parts` 是一个**保守上界**：每个请求最多被 `num_sm_parts` 个 part 劈分，故 split 槽位数取 `b + num_sm_parts`，宁可空着也不能越界。
- 「首次分配、后续复用」：`tile_scheduler_metadata` 与 `num_splits` 只在 `!has_value()` 时分配并跑 `get_sched_meta` kernel（[dense_decode.h:95-113](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L95-L113)），否则直接复用——这正是 u1-l4 讲过的 `FlashMLASchedMeta` 复用模式在 C++ 侧的体现。

#### 4.2.5 小练习与答案

**练习 1**：dense 的 stride 用 `int64_t`，sparse 用 `int`。请从「KV cache 规模」角度解释原因。

**答案**：dense 走分页 KV 池，规模为 `num_blocks × page_block_size × h_k × d`，乘积极易超过 `int32` 上限（约 2.1×10⁹）。例如 `d=576`、`page_block_size=64` 时，`num_blocks` 只要几万，`k_batch_stride` 就可能破 2³¹，必须用 `int64_t`。sparse 走 token-level 索引，单次访问的物理偏移小，且 kernel 内部用 32 位寄存器更省资源，故用 `int`，由接口侧 `int64_stride_to_int` 做溢出保护。

**练习 2**：`o_row_stride` 为什么取 `out.stride(2)` 而不是 `out.stride(1)`？

**答案**：因为 dense 输出张量 `out` 的形状是 `[b, num_heads_k, q_seq_per_hk, head_size_v]`（[dense_decode.h:90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L90)），「row」（即 q 序列维 `q_seq_per_hk`）落在第 2 维，head 维落在第 1 维。所以 `o_row_stride = out.stride(2)`、`o_head_stride = out.stride(1)`。命名里的「row/head」是逻辑语义，与物理维序号并不一一对应，必须以张量实际形状为准。

**练习 3**：sparse 结构里 `attn_sink` / `topk_length` 是裸指针，靠 `nullptr` 表示「未提供」。这种设计相对 `std::optional<T*>` 有什么好处？

**答案**：POD 结构里塞 `std::optional` 会引入额外状态字节、破坏紧凑布局与对齐，也不便直接传给 CUDA device 代码；裸指针 + nullptr 约定让结构保持纯数据、可直接拷贝到 device，kernel 内部用 `if (ptr != nullptr)` 即可判断开关，开销最低。

---

### 4.3 CombineParams、GetDecodeSchedMetaParams、SparseAttnFwdParams：辅助与 prefill 参数

#### 4.3.1 概念说明

除了两个解码主结构，`params.h` 还有三个结构，分别服务三件事：

- `CombineParams`：喂给 **combine kernel**——把各 split 的局部 `o_accum`/`lse_accum` 归并成全局 `out`/`lse`。dense 与 sparse 解码**共用同一个** `CombineParams`。
- `GetDecodeSchedMetaParams`：喂给 **tile scheduler kernel**——产出 `DecodingSchedMeta[]` 与 `num_splits[]`。它是 `*AttnDecodeParams` 与 `CombineParams` 的「上游」。
- `SparseAttnFwdParams`：喂给 **token-level sparse prefill kernel**。prefill 不做 split-KV（没有累加缓冲字段），字段比解码结构精简。

#### 4.3.2 核心流程

三者构成解码的「前-中-后」三段式，而 prefill 是另一条更短的链：

```
解码链（三段式）:
  GetDecodeSchedMetaParams ──► sched_meta kernel ──► DecodingSchedMeta[] / num_splits[]
        │                                                    │
        ▼                                                    ▼
  *AttnDecodeParams ──► 主 attention kernel ──► o_accum[] / lse_accum[]
                                                         │
                                                         ▼
        CombineParams ──► combine kernel ──► out / lse

prefill 链（无 combine）:
  SparseAttnFwdParams ──► phase1 kernel ──► out / lse / max_logits
```

#### 4.3.3 源码精读

**`CombineParams`** —— [params.h:105-125](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L105-L125)：

```cpp
struct CombineParams {
    int b, s_q, h_q, d_v;
    float* lse;        void* out;             // 全局输出
    /* out/lse 的 stride */
    float* lse_accum;  float* o_accum;        // 各 split 的局部结果
    /* accum 的 stride */
    DecodingSchedMeta* tile_scheduler_metadata_ptr;
    int* num_splits_ptr;  int num_sm_parts;   // 知道每个请求有几段要合并
    float* attn_sink;   // [h_q], may be nullptr —— 输出缩放，且不影响返回的 lse
    cudaStream_t stream;
};
```

关键点：combine 需要 `num_splits_ptr` 才知道每个请求实际产生了多少段局部结果（前缀和数组，详见 u4-l2）；`attn_sink` 可空，非空时对最终 `out` 做缩放，但**不影响返回的 `lse`**（这是一个容易踩坑的语义，u4-l2 会详述）。

它的装配现场在 [dense_decode.h:187-207](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L187-L207) 与 [sparse_decode.h:470-489](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L470-L489)——注意 dense 的 combine 装配里 `attn_sink` 写死成 `nullptr`（dense 不支持 attn_sink），而 sparse 才透传。

**`GetDecodeSchedMetaParams`** —— [params.h:127-143](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L127-L143)：

```cpp
struct GetDecodeSchedMetaParams {
    int b, s_q, block_size_n, fixed_overhead_num_blocks;
    int topk, extra_topk;          // -1 if sparse attention (or extra topk) is disabled
    int *topk_length, *extra_topk_length;
    int *seqlens_k_ptr;            // Only necessary for dense attention
    DecodingSchedMeta *tile_scheduler_metadata_ptr;
    int *num_splits_ptr;  int num_sm_parts;
    cudaStream_t stream;
};
```

它是**唯一同时承载 dense 与 sparse 调度信息**的结构：`topk`/`extra_topk` 用 `-1` 表示「该稀疏特性未启用」，`seqlens_k_ptr` 仅 dense 需要（dense 按真实序列长切分，sparse 按索引数切分）。同一个调度 kernel 既能服务 dense 也能服务 sparse，靠这几个标志位区分——对比 [dense_decode.h:101-113](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L101-L113)（`topk=-1, extra_topk=-1`，传 `seqlens_k`）与 [sparse_decode.h:425-438](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L425-L438)（传 `topk`/`extra_topk`，`seqlens_k=nullptr`）。

**`SparseAttnFwdParams`** —— [params.h:145-168](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L145-L168)：

```cpp
struct SparseAttnFwdParams {
    int s_q, s_kv, h_q, h_kv, d_qk, d_v, topk;
    float sm_scale, sm_scale_div_log2;
    bf16* q;   bf16* kv;   int* indices;   // [s_q,h_q,d_qk] / [s_kv,h_kv,d_qk] / [s_q,h_kv,topk]
    float* attn_sink;   int* topk_length;  // may be nullptr
    /* q/kv/indices 的 stride */
    bf16* out;   float* max_logits;   float* lse;   // 输出三元组
    int num_sm;   cudaStream_t stream;
};
```

注意它与解码结构的差异：**没有 batch 维**（prefill 单条序列处理）、**没有 split-KV 缓冲**、多了一个 `max_logits` 输出（解码只返回 `lse`，prefill 额外返回每个 query 的最大 logit，供 DSA 后续路由用）、`h_kv` 可以 >1（sparse decode 强制 `h_kv==1` 即 MQA，prefill 则允许 MHA）。装配现场见 [sparse_fwd.h:169-189](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L169-L189)。

#### 4.3.4 代码实践

**实践目标**：体会「同一份调度结构服务 dense 与 sparse」的设计。

**操作步骤**：

1. 打开 [dense_decode.h:101-113](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L101-L113)，记录 dense 给 `GetDecodeSchedMetaParams` 各字段的取值。
2. 打开 [sparse_decode.h:425-438](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L425-L438)，同样记录 sparse 的取值。
3. 列出两份赋值里**值不同**与**值相同**的字段。

**需要观察的现象 / 预期结果**：

| 字段 | dense | sparse |
|---|---|---|
| `b`, `s_q` | 同 | 同 |
| `block_size_n` | 64 | `impl_meta.block_size_topk`（=64） |
| `fixed_overhead_num_blocks` | 5 | `impl_meta.fixed_overhead_num_blocks`（5 或 3） |
| `topk` / `extra_topk` | **-1, -1** | 真实 topk / extra_topk |
| `topk_length` / `extra_topk_length` | **nullptr, nullptr** | 真实指针 |
| `seqlens_k_ptr` | **真实指针** | **nullptr** |

`-1` 与 `nullptr` 就是 dense/sparse 在调度结构里的「开关位」。结论：调度 kernel 内部靠这些标志区分两种模式，因此无需为 dense/sparse 写两个调度 kernel。

#### 4.3.5 小练习与答案

**练习 1**：`SparseAttnFwdParams` 为什么没有 `o_accum`/`lse_accum` 这类 split-KV 字段？

**答案**：prefill 阶段每个 query 处理的 KV 由 `indices` 显式指定（token-level 稀疏），量级远小于「整条序列」，单次 kernel 即可在共享内存里完成 online softmax，无需把 KV 切成多段、也无需跨 split 归并，所以没有累加缓冲字段，链路更短。

**练习 2**：combine 装配时，dense 把 `attn_sink` 写成 `nullptr`，sparse 透传真实指针。这暗示 dense 解码是否支持 attn_sink？

**答案**：不支持。`DenseAttnDecodeParams` 根本没有 `attn_sink` 字段（对比 `SparseAttnDecodeParams` 有），所以 dense 的 combine 只能传 `nullptr`。attn_sink 是 sparse（DSA 稀疏注意力）专属特性。

---

### 4.4 ModelType 与 SparseAttnFwdMode：编码与运行模式枚举

#### 4.4.1 概念说明

`params.h` 末尾有两个枚举，回答两个问题：

1. **「这块 FP8 KV cache 是哪种字节布局？」** → `ModelType`：`V32`（DeepSeek-V3/V3.2，每 token 656 字节）或 `MODEL1`（另一种实验性布局）。两者 `d_qk` 不同（576 vs 512），量化粒度与 scale 排布也不同（u5-l1 详述）。
2. **「这个同时支持 prefill 与 decode 的 kernel，现在跑哪种模式？」** → `SparseAttnFwdMode`：`Prefill` 或 `DecodeWithSplitKV`。因为 FlashMLA 有几个 kernel（如 SM100 的 `fwd_for_small_topk/head128`）**一套模板同时服务 prefill 和 sparse decode**，靠这个枚举在编译期切换分支。

#### 4.4.2 核心流程

`SparseAttnFwdMode` 配合两个模板工具，把「同一 kernel、两种入参结构」统一成一个类型派发：

```
SparseAttnFwdMode::Prefill            → 用 SparseAttnFwdParams
SparseAttnFwdMode::DecodeWithSplitKV  → 用 SparseAttnDecodeParams

is_decode_v<MODE>     : 编译期 bool，判断是否 decode 模式
SparseFwdArgT<MODE>   : 类型别名 = conditional(is_decode, SparseAttnDecodeParams, SparseAttnFwdParams)
```

这样 kernel 模板可以写成 `template<SparseAttnFwdMode FWD_MODE> void run(... SparseFwdArgT<FWD_MODE> params)`，一份代码兼容两种入参。

#### 4.4.3 源码精读

[ModelType — params.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L5-L8)：

```cpp
enum class ModelType { V32, MODEL1 };
```

它的取值由 `d_qk` 决定：`d_qk==576 → V32`、`d_qk==512 → MODEL1`，见 [sparse_decode.h:318-325](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L318-L325)。`model_type` 接着进入 `SparseAttnDecodeParams`，被 `DISPATCH_MODEL_TYPE` 宏编译期化（u2-l3），为每种布局生成特化 kernel。

[SparseAttnFwdMode 与模板工具 — params.h:170-180](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L170-L180)：

```cpp
enum class SparseAttnFwdMode {
    Prefill,
    DecodeWithSplitKV,   // 让「双模 kernel」进入解码模式
};
template<SparseAttnFwdMode FWD_MODE>
inline constexpr bool is_decode_v = std::bool_constant<FWD_MODE == SparseAttnFwdMode::DecodeWithSplitKV>::value;
template<SparseAttnFwdMode FWD_MODE>
using SparseFwdArgT = std::conditional_t<is_decode_v<FWD_MODE>, SparseAttnDecodeParams, SparseAttnFwdParams>;
```

两处真实调用：[sparse_fwd.h:97](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L97) 用 `SparseAttnFwdMode::Prefill` 跑 prefill；[sparse_decode.h:179](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L179) 用 `SparseAttnFwdMode::DecodeWithSplitKV` 复用同一个 head128 small_topk kernel 跑 sparse decode。这是 SM100 head128 sparse 解码没有独立 kernel、转而复用 prefill kernel 的关键开关（u6-l3/u9-l1 详述）。

#### 4.4.4 代码实践

**实践目标**：验证 `SparseFwdArgT` 的类型派发，理解「双模 kernel」如何复用。

**操作步骤**（源码阅读型）：

1. 在仓库里搜索 `run_fwd_for_small_topk_phase1_kernel` 的声明，看它的模板参数与 `SparseFwdArgT` 的关系。
2. 对比 [sparse_fwd.h:97](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L97) 与 [sparse_decode.h:179](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L179) 两次调用，列出传入的 params 类型。

**需要观察的现象 / 预期结果**：prefill 调用传 `SparseAttnFwdParams`、decode 调用传 `SparseAttnDecodeParams`，但两者都进入**同一个** `run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::?, 512>` 模板。`SparseFwdArgT<Prefill> = SparseAttnFwdParams`、`SparseFwdArgT<DecodeWithSplitKV> = SparseAttnDecodeParams`，由 `is_decode_v` 在编译期选型。结论：枚举 + `conditional_t` 让一份 kernel 代码同时接两种结构，省去重复实现。

#### 4.4.5 小练习与答案

**练习 1**：`is_decode_v<Prefill>` 的值是什么？`SparseFwdArgT<Prefill>` 展开成哪个类型？

**答案**：`is_decode_v<Prefill> = false`（因为 `Prefill != DecodeWithSplitKV`）；`SparseFwdArgT<Prefill> = std::conditional_t<false, SparseAttnDecodeParams, SparseAttnFwdParams> = SparseAttnFwdParams`。

**练习 2**：为什么不直接用 `bool is_decode` 而要定义一个枚举 + `is_decode_v` 模板变量？

**答案**：因为「双模 kernel」需要在**编译期**沿 `FWD_MODE` 分支生成两套特化代码（解码模式多出 split-KV、调度元数据等处理），运行时 `bool` 无法驱动模板特化。把模式编码成枚举、再用 `template<...> constexpr bool is_decode_v` 转成编译期常量，才能在 `if constexpr` 或模板偏特化里分流。

## 5. 综合实践

把本讲四个模块串起来，做一次「**逆向工程**」：给定一次 sparse decode 调用，画出它**用到 `params.h` 里哪几个结构、各自由谁装配、又传给哪个 kernel**。

**任务**：

1. 选定 sparse 解码路径（[sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h)），列出本次调用会实例化的 `params.h` 结构（应有 3 个：`GetDecodeSchedMetaParams`、`SparseAttnDecodeParams`、`CombineParams`）。
2. 为每个结构标注：**装配代码行号**、**消费它的 kernel 函数名**、**它复用了哪些 `DecodingSchedMeta`/`num_splits` 缓冲**。
3. 重点回答：`DecodingSchedMeta[]` 这片缓冲，在 sparse decode 一次调用里被**写了几次、读了几次**？分别由哪个结构携带它的指针？

**预期结论**（可对照自检）：

- `GetDecodeSchedMetaParams`（[sparse_decode.h:425-438](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L425-L438)）→ `run_get_decoding_sched_meta_kernel`：**写** `tile_scheduler_metadata` 与 `num_splits`。
- `SparseAttnDecodeParams`（[sparse_decode.h:385-415](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L385-L415)）→ `run_flash_splitkv_mla_fp8_sparse_kernel`：**读** `tile_scheduler_metadata`/`num_splits`，**写** `o_accum`/`lse_accum`。
- `CombineParams`（[sparse_decode.h:470-489](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L470-L489)）→ `run_flash_mla_combine_kernel`：**读** `tile_scheduler_metadata`/`num_splits`/`o_accum`/`lse_accum`，**写** `out`/`lse`。
- `DecodingSchedMeta[]`：写 1 次（调度 kernel），读 2 次（主 kernel + combine kernel）；三处都经各自结构里的 `tile_scheduler_metadata_ptr` 字段携带同一指针。

> 无 GPU 时，这是一次纯源码追踪任务，结论可直接从源码行号得出，无需运行。

## 6. 本讲小结

- `params.h` 用一组 POD 结构作为接口层与 kernel 层之间的**数据契约**，统一承载指针、stride（元素单位）、split-KV 缓冲与调度元数据。
- `DecodingSchedMeta` 是 tile 调度的紧凑产物（32 字节、8 个 int），被 4 个结构以 `tile_scheduler_metadata_ptr` 共享，先写一次、再读两次。
- `DenseAttnDecodeParams` 与 `SparseAttnDecodeParams` 字段同构但三处关键差异：stride 类型（int64 vs int）、KV dtype（同 Q vs FP8）、sparse 专属的 `indices`/`topk`/`extra_*`/`model_type`。
- `CombineParams`（dense/sparse 共用）、`GetDecodeSchedMetaParams`（dense/sparse 共用）、`SparseAttnFwdParams`（无 batch、无 split-KV、多 `max_logits`）分别服务归并、调度、prefill 三件事。
- `ModelType`（V32/MODEL1，由 `d_qk` 决定）编码 FP8 布局；`SparseAttnFwdMode` + `is_decode_v` + `SparseFwdArgT` 让「双模 kernel」一份代码同时接 `SparseAttnFwdParams`（prefill）与 `SparseAttnDecodeParams`（decode）。
- 接口函数遵循固定套路：**校验 → 推导 num_sm_parts → 首次分配 sched_meta → 装配 params → 启动 kernel → 装配 combine → 启动 combine**；可空特性统一用 `nullptr` 表示禁用。

## 7. 下一步学习建议

本讲只读了「数据结构定义与装配」，还没有进入 kernel 内部如何**消费**这些字段。建议按以下顺序继续：

1. **u2-l3 Arch 检测与 DISPATCH 宏**：看 `DISPATCH_MODEL_TYPE` / `DISPATCH_NUM_HEADS` / `DISPATCH_HEAD_DIM` 如何把 `params.model_type`、`params.h_q`、`params.d_qk` 这些**运行时值编译期化**为模板常量，生成特化 kernel。这是本讲 `ModelType` 字段的直接下游。
2. **u2-l4 ImplBase 派发框架**：看 `ImplBase<SparseAttnDecodeParams, DecodeFeatures>` 如何以 `params` + feature 集合为入参，理解「结构体 + feature 集合」的组合派发模式。
3. **u4-l1 / u4-l2 Split-KV 与 combine**：本讲反复出现的 `o_accum`/`lse_accum`/`num_splits`/`total_num_splits` 字段，在那里会看到它们在数学上如何被归并——尤其 `CombineParams` 的跨 split rescale 公式与 `attn_sink` 缩放语义。
4. **u5-l1 FP8 KV cache 布局**：本讲 `ModelType` 字段对应的两种 656 字节级字节布局（V32 vs MODEL1）会在那里逐字节拆解。
