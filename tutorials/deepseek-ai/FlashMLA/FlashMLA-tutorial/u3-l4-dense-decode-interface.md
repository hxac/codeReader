# Dense decode 接口与 split-KV 编排

## 1. 本讲目标

前几讲我们已经走完了「Python 包装 → pybind 绑定 → `csrc/api` 接口函数 → kernel 命名空间」的调用链（u2-l1），读懂了 `params.h` 里的参数契约（u2-l2），也深入了 SM90 dense decode 主 kernel 的 seesaw 调度（u3-l3）。

本讲聚焦调用链上**承上启下**的一层——`csrc/api/dense_decode.h` 里的 `dense_attn_decode_interface`。它是 dense 解码路径的「接口函数」，对上接 pybind 绑定 `dense_decode_fwd`，对下串起三个 kernel：

```
get_decoding_sched_meta  →  splitkv_mla（主 kernel）  →  combine
```

读完本讲你应该能够：

- 说清接口函数对输入张量做了哪些 **dtype / shape / layout 校验**，以及为什么要把 Q 的 head 维做一次**重排**。
- 解释 `num_sm_parts` 与 `total_num_splits` 这两个数是怎么算出来的、各自决定了哪块缓冲的形状。
- 把「调度元数据生成 → 主 kernel 写 accumulate → combine 归并」三段式**完整串成一条流程**，并理解单 split 早退为何仍然正确。
- 回答一个易被忽略的细节：为什么 `seqlen_q == 1` 时接口会强制把 `is_causal` 置为 `false`。

## 2. 前置知识

- **Paged KV cache**：KV 不是一根连续长向量，而是被切成 `page_block_size`（本库固定为 64）大小的页，存在 `blocked_k` 池子里，用 `block_table` 记录每个请求用了哪些页。这是 vLLM/FlashDecoding 系列的通用做法。
- **MQA / GQA 与 MLA**：MLA 解码阶段多个 Q 头**共享同一份压缩 KV**（`num_heads_k` 个 KV 头，`num_heads_q` 个 Q 头，`num_heads_q % num_heads_k == 0`）。本讲的「head 维重排」就是为了把这种共享关系暴露给 kernel。
- **Flash-Decoding / Split-KV**：解码时每个请求的 Q 很短、KV 很长，单卡单 kernel 难以喂饱 SM。解决办法是把**长 KV 横向切成多段（split）**，每段各算一份局部输出和局部 log-sum-exp（lse），最后用一个 combine kernel 做 rescale 归并。本讲只讲「缓冲怎么分配、怎么编排」，combine 的逐行数学留到 u4-l2。
- **online softmax 与 lse**：注意力里不直接存 softmax 权重，而是存每行的 log-sum-exp `lse = logsumexp(scores)`，配合「rescale + 累加」实现流式合并。注意本库存在 **base-2 与 base-e（自然对数）两套 lse 约定**，接口层负责在返回前转成 base-e（PyTorch 惯例）。
- **`Arch` / `KU_CHECK_*` / `DISPATCH_*`**：架构检测、张量校验宏、编译期派发宏，均在 `csrc/api/common.h`（u2-l3）和 `kerutils`（u8-l1）中讲过，本讲直接复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | **本讲主角**。dense 解码的接口函数：校验、head 重排、缓冲分配、三段式编排、输出 reshape。 |
| [csrc/params.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h) | 接口与 kernel 之间的数据契约：`DenseAttnDecodeParams` / `CombineParams` / `GetDecodeSchedMetaParams` / `DecodingSchedMeta`。 |
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | `Arch`（查 SM 与 `num_sms`）、`KU_CHECK_*` 的声明来源在 kerutils。 |
| [csrc/sm90/decode/dense/splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh) | 主 kernel。本讲只看它的「输出落点」逻辑（单 split 直写 vs 多 split 写 accumulate）。 |
| [csrc/smxx/decode/combine/combine.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu) | combine kernel。本讲看它的 grid 划分、单 split 早退、base-2→base-e 转换。 |
| [csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu) | 调度元数据 kernel。本讲只看它产出的 `payload`（每 SM part 的工作量上限）。 |
| [flash_mla/flash_mla_interface.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py) | Python 端 `flash_mla_with_kvcache`：dense 分支调用、sched_meta 复用契约。 |
| [tests/test_flash_mla_dense_decoding.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py) | dense 解码的端到端测试，含 `generate_test_data`，是本讲实践的依据。 |

> 提示：主 kernel 的 seesaw 调度、combine 的 rescale 公式、tile scheduler 的负载均衡分别在 u3-l3、u4-l2、u4-l3 详讲。本讲只取**与接口编排直接相关**的片段。

## 4. 核心概念与源码讲解

### 4.1 校验与 head 维重排

#### 4.1.1 概念说明

接口函数是「不信任调用方」的第一道防线。它要做两件事：

1. **防御性校验**：架构对不对（dense decode 只支持 SM90a）、dtype 对不对、张量在不在 CUDA 上、最后一维连不连续、形状符不符合 MLA 的硬约束（`head_size_k ∈ {512, 576}`、`head_size_v == 512`、`page_block_size == 64`、`h_q % h_k == 0`）。任何一个不过就 `TORCH_CHECK` 抛异常，把错误挡在 kernel 启动之前。

2. **Q 的 head 维重排**：这是 dense 解码性能的关键一步。MLA 解码是 MQA/GQA 形态——多个 Q 头共用一组 KV。kernel 想高效复用 KV，就希望「同一份 KV 服务的所有 Q 行」在内存里**相邻排布**，能塞进同一个 `BLOCK_SIZE_M=64` 的 query tile。接口层通过一次 `view → transpose → reshape` 把这种共享结构提前「摊平」到行维上。

#### 4.1.2 核心流程

接口函数的整体骨架是「校验 → 重排 → 算派发参数 → 三段式编排 → reshape 返回」。本节关注前两步：

```
1. Arch arch;  if (!arch.is_sm90a()) 报错            # 架构门禁
2. 校验 dtype / device / layout / shape 约束
3. 由 q.sizes() 取 batch_size / seqlen_q / h_q / head_size_k
4. 由 kcache.sizes() 取 page_block_size / h_k / num_blocks
5. if (seqlen_q == 1) is_causal = false;             # 4.3 节解释
6. head 重排：
   num_q_heads_per_hk = h_q / h_k
   q_seq_per_hk      = seqlen_q * num_q_heads_per_hk
   num_heads          = h_k
   q -> [b, s_q, h_k, num_q_heads_per_hk, d] -> transpose(2,3)
     -> [b, q_seq_per_hk, num_heads, d]
```

重排的直觉：把「Q 头」这一维拆成 `(KV 头数, 每个 KV 头名下的 Q 头数)`，再把后者与 `s_q` 合并成一个新的「有效查询行数」`q_seq_per_hk`。这样 kernel 里「head 维」就退化成 `num_heads = h_k`，而所有共享同一份 KV 的 Q 行被铺在一起，便于 TMA 一次性搬 K/V、喂给一整块 64 行的 WGMMA。

#### 4.1.3 源码精读

架构门禁，dense decode 只认 SM90a：[csrc/api/dense_decode.h:26-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L26-L29)

```cpp
Arch arch = Arch();
if (!arch.is_sm90a()) {
    TORCH_CHECK(false, "Dense decode MLA is only supported on SM90a architecture");
}
```

`Arch` 在构造时通过 `at::cuda::getCurrentDeviceProperties()` 缓存 `major/minor/num_sms`，`is_sm90a()` 判定 `major==9 && minor==0`（[csrc/api/common.h:21-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L21-L41)）。这正是 u1 说的「dense decode 仅 SM90」在代码里的硬门禁。

dtype / layout 校验，注意最后一维必须连续（TMA/GEMM 要求）：[csrc/api/dense_decode.h:31-53](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L31-L53)

```cpp
TORCH_CHECK(q_dtype == torch::kBFloat16 || q_dtype == torch::kHalf);
TORCH_CHECK(kcache.dtype() == q_dtype, "query and key must have the same dtype");
TORCH_CHECK(seqlens_k.dtype() == torch::kInt32, "seqlens_k must be dtype int32");
...
TORCH_CHECK(q.stride(-1) == 1, "q must have contiguous last dimension");
TORCH_CHECK(kcache.stride(-1) == 1, "kcache must have contiguous last dimension");
```

`KU_CHECK_DEVICE` / `KU_CHECK_CONTIGUOUS` 是 kerutils 提供的宏，对 `optional<Tensor>` 会自动「无值即放行」，所以同一套校验既能用在必填张量上、也能用在可空的 `tile_scheduler_metadata` 上（[csrc/kerutils/include/kerutils/supplemental/torch_tensors.h:56-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h#L56-L71)）。

MLA 形状约束——注意第 61 行**报错文案写的是 576，实际断言的是 512**，以代码（`== 512`）为准：[csrc/api/dense_decode.h:60-69](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L60-L69)

```cpp
TORCH_CHECK(head_size_k == 576 || head_size_k == 512, "Only head_size_k == 576 or 512 is supported");
TORCH_CHECK(head_size_v == 512, "Only head_size_v == 576 is supported");  // 文案笔误，实判 512
...
TORCH_CHECK(page_block_size == 64, "Currently page_block_size must be 64");
TORCH_CHECK(num_heads_q % num_heads_k == 0, "Number of heads in key/value must divide ...");
```

> 读真实源码时这种「文案与判定值不一致」的小坑值得留意——以 `TORCH_CHECK` 的**条件表达式**为准，不要被字符串误导。

`seqlen_q == 1` 强制非 causal（4.3 节展开原因）：[csrc/api/dense_decode.h:71-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L71-L71)

**head 维重排**——本节的核心几行：[csrc/api/dense_decode.h:73-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L73-L78)

```cpp
const int num_q_heads_per_hk = num_heads_q / num_heads_k;
const int q_seq_per_hk = seqlen_q_ori * num_q_heads_per_hk;
const int num_heads = num_heads_k;
q = q.view({batch_size, seqlen_q_ori, num_heads_k, num_q_heads_per_hk, head_size_k})
      .transpose(2, 3)
      .reshape({batch_size, q_seq_per_hk, num_heads, head_size_k});
```

举两个具体例子帮助建立直觉（设 `d = 576`，`b = 1`）：

| 输入 `(s_q, h_q, h_k)` | `num_q_heads_per_hk` | `q_seq_per_hk` | 重排后 Q 形状 |
| --- | --- | --- | --- |
| `(1, 128, 1)` 纯 MQA 解码 | 128 | 128 | `[1, 128, 1, 576]` |
| `(2, 128, 2)` GQA | 64 | 128 | `[1, 128, 2, 576]` |

第一个例子里，128 个 Q 头共享 1 份 KV，重排后变成「128 行查询 × 1 个 head」，kernel 只要搬 1 次 K/V 就能服务 128 行 Q——这正是 MQA 友好的布局。reshape 之后紧跟一句 `KU_CHECK_SHAPE(q, batch_size, q_seq_per_hk, num_heads, head_size_k)` 确认重排结果符合后续 kernel 的预期（[csrc/api/dense_decode.h:80-80](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L80-L80)）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 head 重排的形状变化，并确认它与 PyTorch 参考实现（`reference_torch`）的语义一致。

**操作步骤**（无 GPU 也能跑，纯 CPU 张量形状推演）：

1. 打开 [tests/test_flash_mla_dense_decoding.py:29-70](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L29-L70) 阅读 `generate_test_data`，记下 `q` 的形状是 `[b, s_q, h_q, d]`、`blocked_k` 是 `[num_blocks, block_size, h_kv, d]`。
2. 在本地起一个 Python，用 CPU 张量复现重排：

   ```python
   import torch
   b, s_q, h_q, h_k, d = 2, 1, 128, 1, 576
   q = torch.randn(b, s_q, h_q, d)
   num_q_heads_per_hk = h_q // h_k
   q_seq_per_hk = s_q * num_q_heads_per_hk
   q2 = q.view(b, s_q, h_k, num_q_heads_per_hk, d).transpose(2, 3).reshape(b, q_seq_per_hk, h_k, d)
   print(q2.shape)  # 期望 torch.Size([2, 128, 1, 576])
   ```

3. 再用 `s_q=2, h_q=128, h_k=2` 跑一次，期望得到 `[1, 128, 2, 576]`。

**需要观察的现象**：重排**不改变数据**，只改变维度切分与排列顺序；`q2` 的元素总数与 `q` 相同。

**预期结果**：两个例子的输出形状分别如上表。若想验证语义正确，可以把 `q2[k]`（第 k 个查询行）映射回原始 `(s_q_idx, q_head_idx)`，确认它对应 `q[:, s_q_idx, q_head_idx]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么校验里要求 `q.stride(-1) == 1`（最后一维连续），却允许 `seqlens_k` 用 `KU_CHECK_CONTIGUOUS`（整张连续）即可？

> **答**：主 kernel 用 TMA / WGMMA 访问 Q、K，要求 `head_size` 这一最内维在内存里连续（`stride(-1)==1`），但更外维可以跨步；而 `seqlens_k` / `block_table` 是小张量，kernel 里按下标直接 `__ldg`，要求整张连续更简单也更安全。

**练习 2**：若用户传入 `head_size_k = 256`，会在哪一行、报什么错？

> **答**：在 [dense_decode.h:60](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L60) 抛出 `"Only head_size_k == 576 or 512 is supported"`。注意这是**运行时**校验，与 u2-l3 讲的把 `head_dim` 编译期化的 `DISPATCH_HEAD_DIM` 宏不同——dense decode 路径在接口层直接拒绝非法值，合法值再透传给 kernel 模板。

---

### 4.2 num_sm_parts 与缓冲分配

#### 4.2.1 概念说明

Split-KV 的并行模型是：把所有 SM 分成若干 **SM part**，每个 part 负责一段「请求 × KV 块」区间，产出一段局部结果。两个关键数决定整套缓冲：

- **`num_sm_parts`**：把 SM 切成几份。query 的「工作量」越多（Q 头多、s_q 大），单份 KV 就够喂饱 SM，`num_sm_parts` 趋近 1；query 越少（典型解码 `s_q=1`、`h_k=1`），就把同一份长 KV 切给更多 part 去并行——这正是 Flash-Decoding 暴露并行度的旋钮。
- **`total_num_splits`**：整个 batch 一共产出多少段局部结果的上界。它决定两块 accumulate 缓冲（`lse_accum`、`out_accum`）的第一维大小。接口必须在 kernel 启动**之前**就分配好缓冲，所以这里要给一个安全的上界，而不是运行时才确定的精确值。

#### 4.2.2 核心流程

```
num_M_tiles = ceil_div(q_seq_per_hk, 64)        # 每 head 的 query 行切成几个 64 行 tile
num_sm_parts = max(num_sms / num_heads_k / num_M_tiles, 1)
# 含义：query 侧的「问题数」= num_heads_k * num_M_tiles
#       问题少 → 每个 query tile 分到更多 SM 去切 KV → num_sm_parts 大
#       问题多 → 不需要切 KV → num_sm_parts 塌缩到 1

total_num_splits = batch_size + num_sm_parts      # 安全上界
lse_accum = empty[total_num_splits, num_heads, q_seq_per_hk]        (float32)
out_accum = empty[total_num_splits, num_heads, q_seq_per_hk, d_v]   (float32)
```

> 关于上界：每个请求**至少**贡献 1 段 split（baseline 共 `batch_size` 段）；当一个请求被切到多个 SM part 上时，会额外产生若干段碎片，而 part 边界最多 `num_sm_parts` 处。因此总 split 数 `≤ batch_size + num_sm_parts`，接口据此开缓冲；真正的段数由调度 kernel 写入 `num_splits[batch_size]`，combine 只读前这么多。

#### 4.2.3 源码精读

`num_sm_parts` 的计算（注意 `/` 从左到右结合，整除）：[csrc/api/dense_decode.h:78-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L78-L78)

```cpp
int num_sm_parts = std::max(arch.num_sms / num_heads_k / cutlass::ceil_div(seqlen_q_ori*num_heads_q/num_heads_k, 64), 1);
```

读法：`num_sm_parts = max( num_sms ÷ num_heads_k ÷ num_M_tiles, 1 )`，其中 `num_M_tiles = ceil_div(q_seq_per_hk, 64)`，`64` 即主 kernel 的 `BLOCK_SIZE_M`（见 [csrc/sm90/decode/dense/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/config.h)，u3-l2）。H800 有 132 个 SM，举两组真实数字：

| 配置 `(s_q, h_q, h_k)` | `q_seq_per_hk` | `num_M_tiles` | `num_sm_parts = 132/h_k/num_M_tiles` |
| --- | --- | --- | --- |
| `(1, 128, 1)` 单条解码 | 128 | 2 | `132/1/2 = 66` |
| `(2, 128, 1)` | 256 | 4 | `132/1/4 = 33` |

注意 `batch_size` **不直接**进入 `num_sm_parts`——它通过 tile scheduler 被「摊」到各 part 上（4.3 节）。这与 u3-l1 的理论呼应：`h_q·s_q` 小（query 工作量少）时，必须靠切 KV 来补并行度，于是 `num_sm_parts` 变大。

两块输出张量 `out` / `lse` 的形状（重排后的内部布局，返回前还会 reshape）：[csrc/api/dense_decode.h:90-91](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L90-L91)

```cpp
at::Tensor out = torch::empty({batch_size, num_heads, q_seq_per_hk, head_size_v}, opts);
at::Tensor lse = torch::empty({batch_size, num_heads, q_seq_per_hk}, opts.dtype(at::kFloat));
```

`total_num_splits` 与 accumulate 缓冲分配：[csrc/api/dense_decode.h:164-171](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L164-L171)

```cpp
const int total_num_splits = batch_size + params.num_sm_parts;
at::Tensor lse_accum = torch::empty({total_num_splits, num_heads, q_seq_per_hk}, opts.dtype(at::kFloat));
at::Tensor out_accum = torch::empty({total_num_splits, num_heads, q_seq_per_hk, head_size_v}, opts.dtype(at::kFloat));
...
params.total_num_splits = total_num_splits;
params.softmax_lseaccum_ptr = lse_accum.data_ptr<float>();
params.oaccum_ptr = out_accum.data_ptr<float>();
```

这两块缓冲**总是按 float32 分配**（哪怕 Q/K 是 bf16），因为 split-KV 的局部 rescale 需要更高精度。它们最终被填进 `DenseAttnDecodeParams`（[csrc/params.h:56-58](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L56-L58)）透传给主 kernel 与 combine。

#### 4.2.4 代码实践

**实践目标**：把「形状来源」彻底理清，并量化 `num_sm_parts` 如何随 query 配置变化。

**操作步骤**：

1. 追踪一次 dense decode 调用，按下表填空（以 `b=64, s_q=1, h_q=128, h_k=1, d=576, d_v=512`，SM 数 132 为例）：

   | 张量 | 形状（内部布局） | 来自哪一行 |
   | --- | --- | --- |
   | `out` | `[b, num_heads, q_seq_per_hk, d_v]` = ? | dense_decode.h:90 |
   | `lse` | ? | dense_decode.h:91 |
   | `lse_accum` | `[total_num_splits, num_heads, q_seq_per_hk]` = ? | dense_decode.h:165 |
   | `out_accum` | ? | dense_decode.h:166 |

2. 计算 `num_sm_parts` 与 `total_num_splits`，验证 `lse_accum` / `out_accum` 的第一维。

**需要观察的现象 / 预期结果**（待本地验证）：

- `q_seq_per_hk = 1 × (128/1) = 128`；`num_M_tiles = ceil(128/64) = 2`；`num_sm_parts = max(132/1/2, 1) = 66`；`total_num_splits = 64 + 66 = 130`。
- 因此 `out` 内部形状 `[64, 1, 128, 512]`，`lse_accum` 形状 `[130, 1, 128]`，`out_accum` 形状 `[130, 1, 128, 512]`。
- 注意 `out_accum` 是 float32，字节数 = `130 × 1 × 128 × 512 × 4 ≈ 34 MB`，是这块配置下最大的临时缓冲。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `num_sm_parts` 公式里没有 `batch_size`？batch 很大时会发生什么？

> **答**：`num_sm_parts` 衡量的是「**每个 query tile** 能分到几个 SM 来切 KV」，只取决于 query 侧的并行度（`num_heads_k × num_M_tiles`）。batch 维度的并行是由 tile scheduler 把不同请求**分给不同 part** 来消化的（4.3 节）。batch 很大时，每个 part 拿到多个请求顺序处理，`num_sm_parts` 本身不变，但 `total_num_splits = batch_size + num_sm_parts` 会随 batch 线性增长。

**练习 2**：把 `h_k` 从 1 改成 2（其余不变），`num_sm_parts` 会变大还是变小？

> **答**：变小。`num_sm_parts = num_sms / num_heads_k / num_M_tiles`，`num_heads_k` 在分母上，从 1→2 直接让结果减半（例如 66→33）。直觉：KV 头多了，query 侧「问题数」翻倍，单份 KV 不再那么「闲」，需要切分的并行度下降。

---

### 4.3 调度元数据与 combine 编排

#### 4.3.1 概念说明

把校验、重排、缓冲都备齐后，接口要按顺序启动三个 kernel，并保证它们的数据依赖：

1. **`get_decoding_sched_meta`**：一个单 CTA、32 线程的小 kernel，把 batch 里各请求的 KV 块**均衡地**切给 `num_sm_parts` 个 part，产出每个 part 的 `DecodingSchedMeta`（负责 `[begin_req, end_req]`、块区间、split 起点等）和一个 `num_splits[0..batch]` 前缀和数组。它只在**首次调用**（`tile_scheduler_metadata` 为空）时跑；后续调用复用上次结果——这就是 Python 端 `FlashMLASchedMeta`「首次初始化、后续复用」模式的落点。
2. **`splitkv_mla`（主 kernel）**：每个 SM part 按 `DecodingSchedMeta` 扫描自己的请求/块，跑 online softmax + seesaw 调度，把结果写到「单 split 直写 `out`/`lse`」或「多 split 写 `out_accum`/`lse_accum`」。
3. **`combine`**：对每个 (请求, q 行, head)，跨 split 做 rescale 归并，把 `out_accum` 合并成最终 `out`，把 `lse_accum` 合并成最终 `lse`（并转成 base-e）。若某请求只有 1 个 split，combine 直接早退——因为主 kernel 已把最终结果直写进 `out`/`lse`。

#### 4.3.2 核心流程

```
if (tile_scheduler_metadata 为空):           # 首次调用
    分配 tile_scheduler_metadata[num_sm_parts, 8] (int32)
    分配 num_splits[batch+1] (int32)
    run_get_decoding_sched_meta_kernel(...)  # 产出 DecodingSchedMeta + 前缀和
else:                                         # 复用
    校验已存在张量的 dtype/device/shape

装配 DenseAttnDecodeParams（指针、stride、缓冲、stream）
run_flash_splitkv_mla_kernel<bf16/half>(params)   # 主 kernel

装配 CombineParams（指向同一批缓冲）
run_flash_mla_combine_kernel<bf16/half>(combine_params)  # 归并

out  -> reshape 回 [b, s_q, h_q, d_v]
lse  -> reshape 回 [b, h_q, s_q]
return {out, lse, tile_scheduler_metadata, num_splits}
```

combine 的归并数学（base-2，\(l_s\) 为第 s 段的局部 lse，\(O_s\) 为第 s 段的局部输出）：

\[
\text{global\_lse} = \log_2\!\Big(\sum_s 2^{\,l_s}\Big),\qquad
\text{scale}_s = 2^{\,l_s - \text{global\_lse}},\qquad
O = \sum_s \text{scale}_s \cdot O_s
\]

最终返回给 PyTorch 的 lse 要转成自然对数：\(\text{lse}_{\text{base-}e} = \text{global\_lse} / \log_2 e\)。

#### 4.3.3 源码精读

**首次调用才生成调度元数据**——这是 sched_meta 复用模式的 C++ 侧落点：[csrc/api/dense_decode.h:95-123](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L95-L123)

```cpp
if (!tile_scheduler_metadata.has_value()) {
    tile_scheduler_metadata = torch::empty({num_sm_parts, sizeof(DecodingSchedMeta)/4}, opts.dtype(torch::kInt32));
    num_splits = torch::empty({batch_size+1}, opts.dtype(torch::kInt32));
    ...
    GetDecodeSchedMetaParams get_sched_meta_params = {
        batch_size, seqlen_q_ori,
        64,        // block_size_n = page_block_size
        5,         // fixed_overhead_num_blocks（每 part 固定开销，影响负载均衡粒度）
        -1, -1,    // topk, extra_topk = -1 → dense 模式
        nullptr, nullptr,
        seqlens_k.data_ptr<int>(),
        (DecodingSchedMeta*)tile_scheduler_metadata->data_ptr(),
        num_splits->data_ptr<int>(),
        num_sm_parts, ...
    };
    smxx::decode::run_get_decoding_sched_meta_kernel(get_sched_meta_params);
} else {
    // 复用：仅校验 dtype/device/shape
}
```

`topk = -1` 与 `extra_topk = -1` 是 dense 模式的开关——同一个 `GetDecodeSchedMetaParams` 结构在 sparse 解码里会被填成真实 topk（见 [csrc/params.h:127-143](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L127-L143) 与 u4-l3）。调度 kernel 内部用每个 part 的工作量上限 `payload = ceil_div(total_num_blocks, num_sm_parts) + fixed_overhead_num_blocks` 来均衡切块（[get_decoding_sched_meta.cu:62-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L62-L62)）。

Python 侧的复用契约——「形状与 `cache_seqlens` 等取值必须跨调用一致」：[flash_mla/flash_mla_interface.py:115-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L115-L149)。典型用法是：同一次解码步内，模型有几十层都要跑 `flash_mla_with_kvcache`，它们的 `cache_seqlens` 完全相同，于是**生成一次 sched_meta、所有层复用**，把调度开销摊薄到一次。

> 含义提醒：跨「不同解码步」（`cache_seqlens` 每步 +1）**不能**复用同一 sched_meta，因为元数据里编码了按块分布的切分，序列长度变了切分就失效——Python 端会用一连串 `assert` 把不一致挡下。

**主 kernel 的输出落点——单 split 直写 vs 多 split 写 accumulate**：[csrc/sm90/decode/dense/splitkv_mla.cuh:1230-1262](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1230-L1262)

```cpp
if (is_no_split) {
    store_o<T, true>(rO, gO, ...);                 // 直写最终 out
    gSoftmaxLse(i) = ... logf(cur_L) + sM(i)/M_LOG2E;   // base-e 直写最终 lse
} else {
    int split_idx = params.num_splits_ptr[batch_idx] + n_split_idx;
    store_o<T, false>(rO, gOAccum, ...);           // 写 out_accum[split_idx]
    gSoftmaxLseAccum(i) = ... log2f(cur_L) + sM(i);     // base-2 写 lse_accum[split_idx]
}
```

`is_no_split` 的判定：一个请求若完整地落在单个 part 内（既不在开头被切、也不在结尾被切），就没有 split-KV 必要，主 kernel 把最终结果直接写进 `params.o_ptr` / `params.softmax_lse_ptr`（注释见 [splitkv_mla.cuh:968-968](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L968-L968)，判定见 [splitkv_mla.cuh:1033-1033](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1033-L1033)）。注意两条路径的 **lse 进制不同**：直写用 `logf + /M_LOG2E`（base-e，可直接返回），accumulate 用 `log2f`（base-2，留给 combine 做 exp2f rescale）。

**combine 的单 split 早退**——正因为主 kernel 已直写最终结果，combine 对 `my_num_splits==1` 的请求直接 `return`：[csrc/smxx/decode/combine/combine.cu:36-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L36-L41)

```cpp
const int start_split_idx = __ldg(params.num_splits_ptr + batch_idx);
const int end_split_idx   = __ldg(params.num_splits_ptr + batch_idx + 1);
const int my_num_splits = end_split_idx - start_split_idx;
if (my_num_splits == 1) {
    return;   // 主 kernel 已直写 out/lse，无需归并
}
```

combine 的 grid 划分——每个 CTA 处理一个 (batch, q 行) 的 8 个头（注意这里的 `BLOCK_SIZE_M=8`，与主 kernel 的 64 是两回事）：[csrc/smxx/decode/combine/combine.cu:24-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L24-L34)，launch 见 [combine.cu:202-210](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L202-L210)。归并后把 base-2 的 `global_lse` 转成 base-e 写回：[combine.cu:99-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L99-L100)

```cpp
if (lane_idx == 0)
    gLse(warp_idx) = global_lse / (float)M_LOG2E;   // base-2 → base-e，匹配 PyTorch 惯例
```

> 三个 kernel 之间还用了 Hopper 的 **PDL（Programmatic Dependent Launch）**：主 kernel 在尾部 `cudaTriggerProgrammaticLaunchCompletion()`（[splitkv_mla.cuh:1226](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1226-L1226)）通知 combine 可以提前启动，combine 入口 `cudaGridDependencySynchronize()`（[combine.cu:59](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L59-L59)）等到数据真就绪再往下走，从而重叠两个 kernel 的尾部/头部。

**CombineParams 的装配**——把主 kernel 写过的 accumulate 缓冲与最终输出缓冲一并交给 combine：[csrc/api/dense_decode.h:187-217](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L187-L217)（关键几行）：

```cpp
CombineParams combine_params = {
    batch_size, seqlen_q_ori, num_heads_q, head_size_v,
    params.softmax_lse_ptr, params.o_ptr, ...,           // 最终输出
    params.softmax_lseaccum_ptr, params.oaccum_ptr, ...,  // 每段局部结果
    params.tile_scheduler_metadata_ptr,
    params.num_splits_ptr, params.num_sm_parts,
    nullptr,                                              // attn_sink：dense 路径恒为 nullptr
    at::cuda::getCurrentCUDAStream().stream()
};
```

`attn_sink = nullptr` 是 dense 与 sparse 的分水岭：Python 端断言 dense 时 `attn_sink is None`（[flash_mla_interface.py:163-163](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L163-L163)），于是 combine 里那段对输出做 `exp(lse)/(exp(lse)+exp(attn_sink))` 缩放的分支被跳过。

**最后 reshape 回用户视角**：[csrc/api/dense_decode.h:219-224](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L219-L224)

```cpp
out = out.view({batch_size, num_heads_k, seqlen_q_ori, num_q_heads_per_hk, head_size_v})
        .transpose(1, 2).reshape({batch_size, seqlen_q_ori, num_heads_q, head_size_v});
lse = lse.view({batch_size, num_heads_k, seqlen_q_ori, num_q_heads_per_hk})
        .transpose(2, 3).reshape({batch_size, num_heads_q, seqlen_q_ori});
return {out, lse, tile_scheduler_metadata, num_splits};
```

这正是 4.1 节 head 重排的**逆操作**：把内部 `[b, h_k, q_seq_per_hk]` 拆回 `(h_k, s_q, h_q/h_k)` 再转置成 `[b, h_q, s_q]`。返回的四元组里后两个（`tile_scheduler_metadata`、`num_splits`）会被 Python 存回 `FlashMLASchedMeta` 供下次复用。

#### 4.3.4 代码实践

**实践目标**：回答本讲标题里那个容易被忽略的问题——为什么 `seqlen_q == 1` 时接口强制 `is_causal = false`？

**操作步骤**：

1. 读 [dense_decode.h:71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L71-L71)：`if (seqlen_q_ori == 1) { is_causal = false; }`。
2. 对照参考实现里的 causal 掩码生成：[tests/test_flash_mla_dense_decoding.py:102-108](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L102-L108)，注意它的守卫是 `if is_causal and query.size(1) > 1:`。
3. 用一段 CPU 小代码验证：当 `s_q = 1` 时，下三角 causal mask 是否恒为全 True（即对结果无影响）：

   ```python
   import torch
   s_q, s_k = 1, 4096
   mask = torch.ones(s_q, s_k, dtype=torch.bool).tril(diagonal=s_k - s_q)
   print(mask.all().item())   # 期望 True：单 query 行的 causal 掩码不挡任何 key
   ```

**需要观察的现象 / 预期结果**：输出 `True`。这说明对 `s_q=1` 的纯解码，causal 掩码是空操作。接口层因此把它强制置 `false`，目的有二：(1) 让主 kernel 走非 causal 的快速路径，省掉掩码相关分支；(2) 与参考实现保持一致的语义。两者结果数值等价，所以这是一个**安全**的优化。

#### 4.3.5 小练习与答案

**练习 1**：combine 对 `my_num_splits == 1` 直接 `return`，那这个请求的最终 `out` / `lse` 是谁写的？为什么不会是未初始化内存？

> **答**：由主 kernel 写。主 kernel 在 `is_no_split` 为真时（请求完整落在单个 part 内，即只产生 1 个 split），已经把最终结果**直写**进 `params.o_ptr` / `params.softmax_lse_ptr`（[splitkv_mla.cuh:1230-1239](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1230-L1239)）。combine 的早退恰恰依赖这一点：1 个 split 意味着无需归并，主 kernel 已给出终值。

**练习 2**：主 kernel 直写 lse 时用 `logf + /M_LOG2E`，写 accumulate 时用 `log2f`，combine 出口又除一次 `M_LOG2E`。为什么两条路径要混用 base-e 和 base-2？

> **答**：combine 的 rescale 用 `exp2f`（base-2，Hopper 上 `exp2f` 比 `expf` 快），所以 accumulate 路径全程 base-2（`log2f` 存、`exp2f` 合并），最后出口再 `/M_LOG2E` 转回 PyTorch 惯例的 base-e。而单 split 直写路径**不经 combine**，没有 rescale 步骤，于是主 kernel 当场就用 `logf + /M_LOG2E` 直接给出 base-e。两条路径出口都是 base-e，对调用方一致。

**练习 3**：如果用户在第二次调用时把 `cache_seqlens` 改了（序列变长），但仍然传同一个 `sched_meta`，会怎样？

> **答**：C++ 侧不会重跑调度 kernel（走 `else` 复用分支），但 Python 侧 [flash_mla_interface.py:140-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L140-L149) 的一串 `assert` 会检查形状一致性（注意：它校验的是形状与配置字段，`cache_seqlens` 的**值**一致性由文档约定、需调用方自行保证）。若值变了却复用，`DecodingSchedMeta` 里的块切分与真实序列长度不匹配，会导致主 kernel 按错误的块区间读取 KV——所以复用契约要求 `cache_seqlens` 值也必须不变。

---

## 5. 综合实践

把本讲三个最小模块串起来：**追踪一次完整的 dense decode 调用，画出从 Python 到 combine 的数据流与形状流。**

1. 阅读测试入口 [tests/test_flash_mla_dense_decoding.py:144-172](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L144-L172)，确认调用序列是 `get_mla_metadata()` → 首次 `flash_mla_with_kvcache(...)`。
2. 选定一组配置（例如 `TestParam(b=4, s_q=2, s_k=4096, h_q=128, h_kv=1, is_causal=True)`），在一张纸上完成下表（无 GPU 则纯手算，标注「待本地验证」）：

   | 量 | 值 | 出处 |
   | --- | --- | --- |
   | `q_seq_per_hk` | ? | dense_decode.h:74 |
   | `num_M_tiles` | ? | dense_decode.h:78（÷64） |
   | `num_sm_parts`（SM=132） | ? | dense_decode.h:78 |
   | `total_num_splits` | ? | dense_decode.h:164 |
   | `out` 返回形状 | ? | dense_decode.h:220 |
   | `lse` 返回形状 | ? | dense_decode.h:222 |
   | `out_accum` 形状（float32） | ? | dense_decode.h:166 |
3. 画出三段式时序图：`get_decoding_sched_meta`（首次）→ `splitkv_mla`（主 kernel，标注「单 split 直写 / 多 split 写 accumulate」两条落点）→ `combine`（标注「单 split 早退 / 多 split rescale」），并在两个 kernel 交界处标上 PDL。
4. 思考并回答：若把 `is_causal=True` 但 `s_q=1`，接口会在哪一行改写它？改写后与不改写的结果有何区别？

> 参考答案要点：`q_seq_per_hk=2×128=256`、`num_M_tiles=4`、`num_sm_parts=max(132/1/4,1)=33`、`total_num_splits=4+33=37`、`out` 形状 `[4,2,128,512]`、`lse` 形状 `[4,128,2]`、`out_accum` 形状 `[37,1,256,512]` float32。`is_causal` 在 dense_decode.h:71 被强制为 `false`，结果与不改写数值等价（单 query 行的 causal 掩码为空操作）。

## 6. 本讲小结

- 接口函数 `dense_attn_decode_interface` 是 dense 解码的「校验 + 编排」中枢：架构门禁只放行 SM90a，dtype/shape/layout 不符即在 kernel 启动前抛错。
- **Q 的 head 维重排**（`view→transpose→reshape`）把 MQA/GQA 的共享结构摊平成 `[b, q_seq_per_hk, h_k, d]`，让 kernel 用最少 K/V 搬运服务最多 Q 行。
- `num_sm_parts = max(num_sms / h_k / ceil_div(q_seq_per_hk,64), 1)`：query 工作量少→多切 KV；`total_num_splits = batch_size + num_sm_parts` 是 accumulate 缓冲的安全上界。
- 三段式编排：**首次**才跑调度元数据 kernel（sched_meta 跨层复用），主 kernel 按元数据扫描请求，combine 跨 split rescale 归并。
- 主 kernel 对**单 split 直写**最终 `out`/`lse`（base-e）、对**多 split 写 accumulate**（base-2）；combine 据此对单 split 早退、对多 split 用 exp2f 归并并出口转 base-e——两条路径对调用方一致。
- `seqlen_q == 1` 时强制 `is_causal = false`，因为单 query 行的 causal 掩码是空操作，与参考实现 `if is_causal and query.size(1) > 1` 同义且数值等价。

## 7. 下一步学习建议

- **u4-l1（Split-KV 缓冲与 Flash-Decoding 思想）**：本讲只点到了 split-KV 的动机与缓冲，下一讲会用纯 PyTorch 复现简化版 split-KV，亲手验证「分段 online softmax + logsumexp 合并」与整体 softmax 数值等价。
- **u4-l2（Combine kernel 逐段精读）**：本讲的 combine 只看了 grid 与早退，下一讲会逐行讲 rescale 合并、`attn_sink` 缩放、三段合并伪代码。
- **u4-l3（Tile scheduler metadata）**：本讲把 `get_decoding_sched_meta` 当黑盒，下一讲会手算小例子（`b=3, 块数 [4,1,5], num_sm_parts=2`）讲清负载均衡与 `num_splits` 前缀和。
- 若想往**横向**拓展，可对比 [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h)：同样的三段式骨架，但多了 FP8 反量化、`ImplBase` feature 派发与 `attn_sink`，能加深对本讲「公共编排」的理解。
