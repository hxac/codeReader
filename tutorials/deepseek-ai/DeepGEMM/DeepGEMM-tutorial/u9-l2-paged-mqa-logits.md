# 分页 MQA logits

## 1. 本讲目标

本讲承接 [u9-l1](u9-l1-mqa-scoring-indexer.md) 的非分页 MQA 评分内核，进入 **解码（decode）阶段** 的分页版本 `fp8_fp4_paged_mqa_logits`。学完后你应该能够：

- 理解 **paged KV cache** 的物理布局：为什么 KV 以「块（page）」为单位离散存放、`block_table` 如何把逻辑块号映射到物理块号、以及 `fused_kv_cache` 是怎样把「数值区 + 缩放因子区」零拷贝拆成两个 TMA 视图的。
- 掌握 `get_paged_mqa_logits_metadata` 产出的 `[num_sms+1, 2]` 调度元数据张量是**如何被 kernel 用来做跨 SM 负载均衡**的：宿主先用一个小型 metadata kernel 把「每个请求的工作量」做前缀和，再把总工作量均摊到每个 SM，写出一对 `(q_token_idx, kv_split_idx)` 起点。
- 看懂 **split-K（split_kv / splits_per_chunk）** 在长上下文评分中的作用：为什么这里的 split-K 不是「需要归约的部分和」，而是「把输出列轴按 KV 段切给不同 SM」的无归约并行。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自依赖讲义）：

- **u9-l1**：MQA logits 的数学定义 \( \text{out}[i,j]=\sum_h w[i,h]\,\mathrm{ReLU}(q[i,h]\cdot kv[j]) \)、逐 token 缩放因子 `sf_kv`、`cu_seq_len_k_start/end` 可见窗口、arch_major 与 `is_fp4` 双开关派发。
- **u4-l1 / u4-l2**：`device_runtime->get_num_sms()` 返回设备 SM 数；TMA 描述符（CUtensorMap）由宿主用 `make_tma_2d_desc / make_tma_3d_desc` 构造后以 `__grid_constant__` 传入 kernel。
- **u6-l2 / u6-l3**：SM100 的 UMMA/tcgen05、TMEM 累加器、UTCCP 把打包 UE8M0 缩放因子搬进 TMEM、以及 TMA 异步拷贝配 mbarrier 同步。

补充两个本讲要用到的术语：

- **paged KV cache（分页 KV 缓存）**：在解码阶段，每个请求的 KV 序列长度不一，且显存碎片化。常见做法是把 KV 切成固定大小的「页（page，本库里就是 `block_kv` 个 token）」，按需分配物理块；一个请求的 KV 由一组**不连续**的物理块组成，用一张 `block_table` 记录「第 i 个逻辑块 → 物理块号」。
- **split-K**：把一个乘加的规约轴（这里是 KV/token 轴）切成若干段并行计算。本讲的 split-K 仅切分输出列、不产生跨段部分和，这点会在 4.3 重点澄清。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [csrc/apis/attention.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp) | 宿主 API 层：`fp8_fp4_paged_mqa_logits`、`get_paged_mqa_logits_metadata` 的参数校验、`fused_kv_cache` 拆视图、输出分配与架构派发。 |
| [csrc/jit_kernels/impls/sm100_mqa_logits.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp) | 宿主 Runtime 类：生成 `.cu`、构造分页 TMA 描述符（含 `make_tma_3d_desc` 的 KV 页描述符）、组装 LaunchArgs 启动。 |
| [deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh) | 设备侧：metadata kernel（写 `[num_sms+1,2]`）与 `SM100PagedMQALogitsScheduler`（设备侧任务遍历 + 页寻址）。 |
| [deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh) | 设备侧：分页与非分页共享的 `sm100_mqa_logits_core_impl`，以及分页入口 `sm100_paged_mqa_logits`；含按页 TMA 加载与 split-K 写回逻辑。 |

端到端调用链（分页路径）：`fp8_fp4_paged_mqa_logits`（attention.hpp，校验 + 拆 fused cache + 派发）→ `sm100_paged_mqa_logits`（host runtime，JIT + 构造 TMA 描述符 + 启动）→ `sm100_paged_mqa_logits`（device entry，构造 `SM100PagedMQALogitsScheduler`）→ `sm100_mqa_logits_core_impl`（device core，TMA/math warp 分工）。注意：**metadata 必须先于主 kernel 调用**，由用户在 Python 侧显式调用 `get_paged_mqa_logits_metadata` 生成 `schedule_meta` 并传入（见 4.2）。

---

## 4. 核心概念与源码讲解

### 4.1 paged KV 寻址与 fused 布局

#### 4.1.1 概念说明

非分页版（u9-l1）里 KV 是一根**连续**张量 `[seq_len_kv, head_dim]`，kernel 用 `cu_seq_len_k_start/end` 切出每个 query 的可见窗口即可。但在解码阶段：

1. 每个请求的上下文长度 `context_len` 千差万别，连续存放会有大量 padding 浪费；
2. KV 缓存随生成动态增长，显存碎片化严重。

**分页（paging）** 的解法是：把 KV 切成固定大小 `block_kv`（SM100 上为 32 或 64、SM90 上为 64）个 token 的「页」，每页是显存池里的一个物理块。一个请求的 KV 由若干个**逻辑块**拼成，而这些逻辑块在显存池里的**物理位置不连续**，靠一张 `block_table` 记录映射关系。

此外，DeepGEMM 还把 KV 的**数值**和它的**逐 token 缩放因子（SF）** 物理上放在同一个块里，称为 **fused（融合）布局**：访问一块 KV 时，它的数值与对应 SF 都在邻近显存，对 L2/缓存友好，也省去两次独立的全局加载。

#### 4.1.2 核心流程

fused KV cache 的拆解流程：

1. 用户提供一个 `uint8`（`torch::kByte`）张量 `fused_kv_cache`，形状记作 `[num_kv_blocks, block_kv, 1, head_dim_with_sf]`，其中 `head_dim_with_sf` 在 FP8 下为 `head_dim + 4`（4 = `sizeof(float)`）、FP4 下为 `head_dim/2 + 4`。
2. **每个物理块内部**，先连续存放所有 `block_kv` 个 token 的**数值区**（FP8: `block_kv*head_dim` 字节；FP4: `block_kv*head_dim/2` 字节），再连续存放所有 token 的 **SF 区**（FP8: 每个 token 一个 float；FP4: 每个 token 一个打包 UE8M0 的 int32）。
3. DeepGEMM 用 `torch::from_blob` 把这同一片显存**零拷贝**拆成两个视图交给 TMA：
   - `kv_cache`（数值，FP8 视为 `[num_kv_blocks, block_kv, head_dim]` 的 E4M3 张量、FP4 视为 `[num_kv_blocks, block_kv, head_dim/2]` 的 packed-FP4 张量）；
   - `kv_cache_sf`（SF，FP8 为 `[num_kv_blocks, block_kv]` 的 float、FP4 为 `[num_kv_blocks, block_kv]` 的 int32）。
4. `block_table`（`int`，`[batch, max_blocks]`）把「请求 r 的第 page_offset 个逻辑块」映射到物理块号 `block_table[r][page_offset]`。

页寻址在设备侧的逻辑（核心一句话）：给定当前请求在 `block_table` 中的行号 `cur_block_table_row`，以及页内偏移 `page_offset`，物理块号 = `block_table[cur_block_table_row * stride + page_offset]`，再用该块号作为 TMA 描述符的坐标去加载那一页。

#### 4.1.3 源码精读

先看宿主如何把 `fused_kv_cache` 拆成两个视图（FP8 分支）：

[csrc/apis/attention.hpp:305-328](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L305-L328) — 这段先做形状/类型断言，再用 `from_blob` 派生 `kv_cache`（数值）与 `kv_cache_sf`（SF）。注意两个关键点：

- `head_dim_with_sf == head_dim + sizeof(float)`：每 token 留 4 字节给 SF；
- `kv_cache` 的 token 步长是 `head_dim`（数值区紧凑排列），`kv_cache_sf` 起点偏移 `block_kv * head_dim` 字节（数值区之后），印证了 4.1.2 的「数值区 + SF 区」布局。

FP4 分支结构完全对称，只是数值区按 `head_dim/2` 字节（packed e2m1）、SF 是 int32（打包 UE8M0）：

[csrc/apis/attention.hpp:271-294](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L271-L294)

再看宿主如何为分页 KV 构造 **3D TMA 描述符**（这是分页与非分页的关键差异之一——非分页 KV 用 2D 描述符、分页用 3D）：

[csrc/jit_kernels/impls/sm100_mqa_logits.hpp:429-437](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp#L429-L437) — `make_tma_3d_desc(kv_cache, head_dim, page_kv, num_kv_blocks, ...)` 把 KV 看作 `[num_kv_blocks, page_kv, head_dim]` 的三维对象：外维是「物理块号」、中维是「页内 token」、内维是 `head_dim`。设备侧 TMA 加载时，把从 `block_table` 查到的物理块号作为外维坐标即可命中正确的那一页。

最后看设备侧的页寻址函数：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:306-311](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L306-L311) — `get_kv_page_coord_by_page_offset` 做的就是「行号 × stride + 页偏移 → block_table 查表」。`page_offset >= cur_request_num_kv_pages` 时返回 0，是对**最后一个不完整 split** 的越界保护（请求的 KV 长度未必是 split 大小的整数倍，见 4.3）。

为了让页查询不变成全局内存瓶颈，device core 把最多 32 个连续页号**一次性预取进一个 warp 的 lane 寄存器**，之后用 `__shfl_sync` 广播给所有需要的 page_idx：

[deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh:180-216](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L180-L216) — `cached_kv_page_coord` 由每个 lane 调一次 `get_kv_page_coord_by_page_offset` 得到一个页号（lane k 缓存 `cached_kv_page_base + k` 号页），随后 `kNumPagesPerSplit`（≤32）个 page 通过 `__shfl_sync` 从对应 lane 取回，再逐页发 TMA。断言 `kNumPagesPerSplit <= 32` 正是为了保证一个 warp 的 32 个 lane 足够装下一个 split 的所有页。

#### 4.1.4 代码实践

**实践目标**：对照 `tests/test_attention.py` 中 KV cache 的量化与布局，亲手写出 fused 缓冲，验证你理解的「数值区 + SF 区」结构正确。

**操作步骤**：

1. 阅读 [tests/test_attention.py:294-316](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L294-L316) 的 `kv_cache_cast_to_fp8` / `kv_cache_cast_to_fp4`，看清它如何把 `[num_blocks, block_size, 1, head_dim]` 的 BF16 张量压成 `[num_blocks, block_size, 1, head_dim+4]`（FP8）或 `[num_blocks, block_size, 1, head_dim/2+4]`（FP4）的 `uint8` 张量：先写 `block_size*head_dim` 字节数值，再写 `block_size*4` 字节 SF，最后 `.view(...)` 成 4D。
2. 在自己的脚本里构造一个 `num_blocks=2, block_kv=64, head_dim=128` 的 FP8 fused cache（参考上述函数），填入已知数值。
3. 用 `torch.from_blob` 按 attention.hpp:317-328 的步长把它拆成 `kv_cache` 与 `kv_cache_sf` 两个视图。
4. 遍历 `block_table`，验证 `kv_cache[block_table[r][p]]` 取到的就是逻辑页 `p` 的数值。

**需要观察的现象**：拆出的 `kv_cache_sf` 起点指针比 `kv_cache` 起点**晚 `block_kv*head_dim` 字节**；同一物理块内 token 步长是 `head_dim`（数值紧凑排列），而不是 `head_dim+4`。

**预期结果**：`kv_cache`、`kv_cache_sf` 与原 `fused_kv_cache` 共享同一片显存（修改其一会影响另一个对应区域），且按 `block_table` 取出的数值与你写入的一致。若手边没有 SM100/SM90 GPU，数值校验部分可在 CPU 上用 `torch.from_blob` 的 CPU 变体完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fused_kv_cache` 的 `stride(1)` 等于 `head_dim_with_sf`（即 `head_dim+4`），而 DeepGEMM 派生出的 `kv_cache` 视图 token 步长却是 `head_dim`？这两个步长矛盾吗？

**答案**：不矛盾。`fused_kv_cache` 是一个**承载容器**，其声明的 `stride(1)=head_dim+4` 只用于携带形状/越界校验信息；物理上每个块是「`block_kv*head_dim` 字节数值区 + `block_kv*4` 字节 SF 区」紧凑排列。DeepGEMM 不直接用容器访问 KV，而是用 `from_blob` 按真实物理布局重新定义两个视图（数值步长 `head_dim`、SF 步长 1），故二者描述的不是同一种访问模式。

**练习 2**：FP4 路径下，`kv_cache_sf` 的 dtype 为什么是 `torch::kInt32` 而不是 float？

**答案**：SM100 用打包 UE8M0 缩放因子（见 u2-l2），4 个 UE8M0（8 位指数、0 位尾数）打包进一个 int32；每 `gran_k=32` 个 KV 通道共用一个 SF，`head_dim=128` 对应 4 个 SF，正好塞进一个 int32，故每 token 一个 int。

---

### 4.2 调度元数据与跨 SM 负载均衡

#### 4.2.1 概念说明

分页场景下，各请求的 Q token 数、上下文长度都不同，总工作量极不均衡。如果像非分页 scheduler 那样简单按 `block_idx % num_sms` 静态分配瓦片，会出现「长上下文请求的 SM 排队、短上下文请求的 SM 空转」。

DeepGEMM 的解法是**两段式调度**：

1. 用户在调用主 kernel **之前**，先调一次 `get_paged_mqa_logits_metadata(context_lens, block_kv, num_sms, indices=...)`，它内部启动一个轻量 **metadata kernel**，把每个请求的工作量做前缀和，再均摊到每个 SM，产出一个 `[num_sms+1, 2]` 的 int 张量 `schedule_meta`。
2. 主 kernel launch 时 grid 恰为 `num_sms` 个 CTA，第 `sm_idx` 个 CTA 读取 `schedule_meta[sm_idx]`（自己的起点）与 `schedule_meta[sm_idx+1]`（自己的终点），在 `[start, end)` 区间内遍历属于自己的任务。

`schedule_meta` 的每一行是一对 `(q_token_idx, kv_split_idx)`，表示「该 SM 从哪个请求的哪个 KV split 开始算」。`[num_sms+1, 2]` 比 SM 数多一行，最后一行是「全场结束」的哨兵，让最后一个 SM 也有明确的 `end` 可读。

> 这与 u6-l4 / u7 的持久化调度思想一致（grid 恰为 `num_sms`），但负载划分不再是编译期静态公式，而是**运行时由 metadata kernel 动态算出**——这正是解码阶段「token 数与上下文长度都到运行时才知道」的必然要求。

#### 4.2.2 核心流程

metadata kernel 的工作流：

1. 为每个「逻辑请求」算工作量。非 varlen 模式下，请求 = `q_token_idx / next_n`，每请求 `next_n` 个 token；varlen 模式下，请求 = `indices` 中一段等值连跑（maximal run of equal indices），用于把变长序列无 padding 地拍平。
2. 每个请求的工作量定义为

\[
\text{work}[r] \;=\; \text{num\_kv\_splits}[r] \;\times\; \text{num\_q\_tokens}[r]
\]

其中 `num_kv_splits = ceil_div(context_len, SPLIT_KV)`。这个权重同时考虑了「Q 侧工作量」和「KV 侧长度」，比单纯按 token 数均摊更准。

3. 对 `work[...]` 做前缀和，得 `num_total_work`。
4. 把总工作量均摊到 `kNumSMs` 个 SM：每个 SM 分到 `q = num_total_work / kNumSMs` 个工作单元，前 `rem = num_total_work % kNumSMs` 个 SM 多分 1 个。
5. 对每个 SM 的起点工作单元 `w`，用**二分查找**定位「第一个前缀和 > w 的请求」，并记录该请求起点 `q_token_idx` 与该工作单元在请求内的 split 偏移 `kv_split_idx = w_in_request / num_q_tokens`。

#### 4.2.3 源码精读

先看宿主 API：`get_paged_mqa_logits_metadata` 分配 `[num_sms+1, 2]` 张量并派发到架构特定的 metadata 实现。

[csrc/apis/attention.hpp:198-231](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L198-L231) — 注意它接收 `block_kv`、`num_sms` 与可选 `indices`（varlen 才有），并断言 SM100 下 `block_kv ∈ {32, 64}`、varlen 时 `next_n == 1`。`schedule_metadata` 形状严格是 `{num_sms + 1, 2}`。

再看设备侧 metadata kernel 本体——这是本讲最核心的一段：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:67-168](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L67-L168) — 逐段对应 4.2.2：

- **工作量计算**（行 117-122）：`prefix_work[r] = info.num_kv_splits * info.num_q_tokens`，正是上面的权重公式；`info` 由 `RequestInfo::from_q_token` 解析得到（请求几何：Q token 数、KV split 数、KV page 数）。
- **单 warp 前缀和**（行 124-136）：用 `math::warp_inclusive_sum` 在 warp 0 内做跨请求的 inclusive scan，`carry` 跨 32-请求组传递。`num_total_work = prefix_work[num_logical_requests - 1]`。
- **SM 起点发射**（行 140-167）：`q = num_total_work / kNumSMs, rem = num_total_work % kNumSMs`，每个 SM 的起点工作单元 `w = sm_idx*q + (sm_idx<rem ? sm_idx : rem)`；二分查找（`while (lo < hi)`）定位请求；最后把 `schedule_meta[sm_idx*2]=q_token_idx`、`schedule_meta[sm_idx*2+1]=kv_split_idx`。
- **尾部哨兵**（行 161-164）：请求越界时写 `q_token_idx = num_q_tokens_total, kv_split_idx = 0`，即 `[num_sms, :]` 这一行表示「全部结束」，供最后一个 SM 当作 `end` 读。

请求几何由 `RequestInfo` 解析，它同时承载 varlen 与非 varlen 两种请求定义：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:23-49](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L23-L49) — varlen 时沿 `indices` 向后扫描等值连跑得到 `num_q_tokens`、用连跑末 token 的 `context_lens[t]` 作为请求长度；非 varlen 时请求 = `q_token_idx/kNextN`、长度取 `context_lens[request_id*next_n + next_n - 1]`（即请求内最后一个 token 的长度）。`num_kv_splits = ceil_div(context_len, SPLIT_KV)`、`num_kv_pages = ceil_div(context_len, PAGE_KV)`（后者用于 4.1 的页越界保护）。

最后，宿主 metadata runtime 把这个 kernel JIT 出来启动，grid 恒为 1、block 256 线程：

[csrc/jit_kernels/impls/sm100_mqa_logits.hpp:31-53](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp#L31-L53) — 模板参数把 `next_n / is_context_lens_2d / is_varlen / split_kv(=256) / num_sms` 烤进编译期；`launch_args = LaunchArgs(1, 256, smem_size)`，smem 装得下 `2 * num_requests * sizeof(int)`（前缀和数组 + 请求起点数组）。注意 `cudaGridDependencySynchronize()`（scheduler 行 79）：metadata 与主 kernel 用 PDL/CDP 串成依赖链，主 kernel 会等 metadata 写完 `schedule_meta` 再读。

#### 4.2.4 代码实践

**实践目标**：用本讲 4.2.2 的公式手算一个最小例子的 `schedule_meta`，再与 `get_paged_mqa_logits_metadata` 的真实输出对照，确认你理解了 `[num_sms+1, 2]` 如何驱动 SM 分配。

**操作步骤**：

1. 构造一个最小场景：`batch_size=2`、`next_n=1`、非 varlen、`SPLIT_KV=256`、`num_sms=4`，两个请求 `context_len=[256, 1024]`（即请求 0 有 1 个 KV split、请求 1 有 4 个 KV split），每请求 1 个 Q token。
2. 手算工作量：`work=[1*1, 4*1]=[1,4]`，前缀和 `[1,5]`，`num_total_work=5`。`q=5/4=1, rem=5%4=1`，所以 SM0 起点工作单元 `w=0*1+0=0`、SM1 起点 `w=1*1+1=2`、SM2 起点 `w=2*1+1=3`、SM3 起点 `w=3*1+1=4`，尾部哨兵 `w=4*1+1=5`。
3. 对每个 `w` 二分定位请求：`w=0,1 → 请求0`，`w=2,3,4,5 → 请求1`；`kv_split_idx = w_in_request / num_q_tokens = w_in_request`。于是 `schedule_meta` 大致为 `[[0,0],[1,1],[1,2],[1,3],[2,0]]`（最后一行 `[num_q_tokens_total=2, 0]` 是哨兵）。
4. 在 GPU 上（SM100）跑：

```python
# 示例代码（仅在已安装 DeepGEMM 的 SM100 环境可运行）
import torch, deep_gemm
context_lens = torch.tensor([[256], [1024]], device='cuda', dtype=torch.int32)  # [batch, next_n]
meta = deep_gemm.get_paged_mqa_logits_metadata(
    context_lens, block_kv=64, num_sms=deep_gemm.get_num_sms(), indices=None)
print(meta.shape)   # torch.Size([num_sms + 1, 2])
print(meta)         # 与手算对照（注意 num_sms 取真实设备值，列的请求/split 含义一致）
```

**需要观察的现象**：`meta.shape == (num_sms+1, 2)`；最后一行第一列等于 `batch*next_n`（总 Q token 数，即哨兵）；上下文更长的请求 1 对应的 `(q_token_idx, kv_split_idx)` 跨越更多 split，被分配给更多 SM。

**预期结果**：手算的「请求/split 划分」与 kernel 输出语义一致（具体列值随 `num_sms` 真实值而变，但「长上下文请求被多个 SM 分担、短上下文请求占用更少 SM」的趋势必然成立）。若无 GPU，可只完成步骤 1–3 的手算推导。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `schedule_meta` 是 `[num_sms+1, 2]` 而不是 `[num_sms, 2]`？

**答案**：第 `sm_idx` 个 SM 需要读自己的起点 `schedule_meta[sm_idx]` 和终点 `schedule_meta[sm_idx+1]`。最后一个 SM（`sm_idx = num_sms-1`）的终点是 `schedule_meta[num_sms]`，因此必须多一行。该行被写成 `(num_q_tokens_total, 0)` 作为「全部结束」哨兵。

**练习 2**：metadata kernel 为什么 grid 只有 1 个 block（`LaunchArgs(1, 256, ...)`），却能算出「每个 SM 的起点」？

**答案**：它把所有请求的工作量前缀和放进共享内存，在 warp 0 内完成 scan，再用 256 个线程**并行地为每个 SM 发射一行** `schedule_meta`（`for (sm_idx = thread_idx; sm_idx <= kNumSMs; sm_idx += num_threads)`）。请求数通常远小于 SM 数，单 block 足够；二分查找也是线程内串行，无需求多 block。

---

### 4.3 split-K 评分与设备侧任务遍历

#### 4.3.1 概念说明

长上下文（本库测试到 65536 token）下，单个请求的 KV 轴极长。若一个请求的评分只能由一个 SM 串行算完，会成为瓶颈。**split-K** 把 KV 轴切成 `SPLIT_KV=256` 个 token 一段，让**多个 SM 并行处理同一请求的不同 KV 段**。

关键认知（务必区分）：传统 split-K GEMM 里，切 K 轴会产生「部分和」，必须跨 SM 做归约（如 atomic add）。**但 MQA logits 不需要**——因为输出 `out[i,j]` 对每个 `(i,j)` 独立，切 KV 轴等价于切**输出的列轴**：处理 split `s` 的 SM 只往 `[s*256, (s+1)*256)` 这段列写结果，不同 SM 写的是**互不重叠的列**。所以这里的 split-K 是「输出列轴的无归约并行」，而非「K 轴部分和归约」。

#### 4.3.2 核心流程

设备侧 `SM100PagedMQALogitsScheduler` 在构造时读取自己 SM 的 `[start, end)`，然后在 `next_q_block` 里按 **chunk 外层、Q-block 内层** 的顺序产出任务：

1. 读 `start = schedule_meta[sm_idx]`、`end = schedule_meta[sm_idx+1]`，得到本 SM 拥有的 `(q_token_idx, kv_split_idx)` 半开区间。
2. 对当前请求，按 chunk 推进 `cur_kv_split_base`：每个 chunk 最多 `kSplitsPerChunk=16` 个 split（或剩余 split），每个 split 是 `SPLIT_KV=256` 个 token。
3. 每个 chunk 内，逐 Q-block 发射任务；一个任务 = `(q_block, kv_split_base, num_kv_splits)`。
4. kernel 收到任务后，对其中每个 split：
   - 用页寻址加载该 split 的 `kNumPagesPerSplit = SPLIT_KV/PAGE_KV` 个页（4.1）；
   - 跑 UMMA/tcgen05 计算点积并做加权 ReLU 归约；
   - 把结果写到 logits 的列 `(kv_split_base + kv_split_idx)*SPLIT_KV + math_thread_idx`。

列映射公式保证不同 split 写到不同列、不同 SM 写到不重叠的列区间，故无需任何跨 SM 归约。

#### 4.3.3 源码精读

宿主侧确定 split 参数与每 chunk 大小：

[csrc/apis/attention.hpp:374-386](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L374-L386) — `constexpr int split_kv = 256` 是写死的 KV 段大小；SM100 分支里 `constexpr int splits_per_chunk = 16`，再把二者连同页大小 `block_kv` 一起传给 `sm100_paged_mqa_logits`。`aligned_max_context_len = align(align(max_context_len, split_kv), stride_logits_alignment)` 保证输出行步长对齐到 256 且按 dtype 满足 1024 字节对齐（TMA/写回要求）。

设备侧 scheduler 的构造函数读取 `[start, end)` 并判定本 SM 是否一上来就 done：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:211-241](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L211-L241) — `start/end` 通过把 `schedule_meta` 当 `uint2*` 数组取出（`reinterpret_cast<const uint2*>(schedule_meta)[sm_idx]`，`.x=q_token_idx, .y=kv_split_idx`）；`done` 在「起点超出总 token 数」或「起点等于本 SM 终点」时立即置真。随后用 `Info::from_q_token(start.x, ...)` 解析起始请求几何。

任务发射与 chunk 推进逻辑：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:249-292](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L249-L292) — `next_q_block` 先捕获当前 `(q_block, chunk)` 的寻址几何（block_table 行、Q-block token 基址与有效 token 数、请求 KV 页数上界），再算本任务的 `num_kv_splits`：请求只有 1 个 Q-block 时直接给 `remaining`，否则取 `min(remaining, kSplitsPerChunk)`（行 264-265），即「chunk 上限 16」。随后推进顺序为「先 Q-block、Q-block 用尽再推进 chunk、chunk 用尽再换请求」（行 267-290），印证 chunk-外层 / Q-block-内层。换请求时若新请求起点恰为本 SM 的 `end`，则 `done=true`（行 285-286）。

输出列与 KV 偏移的映射（证明这是「无归约 split-K」的关键）：

[deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh:302-321](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L302-L321) — `get_kv_tma_offset(kv_split_base, kv_split_idx) = (kv_split_base + kv_split_idx)*SPLIT_KV`（非分页路径用线性偏移直接 TMA），`get_logits_col(...) = (kv_split_base + kv_split_idx)*SPLIT_KV + math_thread_idx`。可见 split `s` 严格写到列段 `[s*256, s*256+256)`，不同 split、不同 SM 的列段天然不重叠。

device core 里 math warp 实际用这两个访问器写回结果：

[deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh:407-498](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L407-L498) — 行 408 `kv_offset = scheduler.get_logits_col(kv_base, kv_split_idx, math_thread_idx)` 算出列、行 489 `q_offset = scheduler.get_logits_row(q_block_idx, i) * logits_stride` 算出行，行 496 `logits[q_offset + kv_offset] = result` 直接写回（分页默认 `kIsCompressedLogits=false`）。整个写回过程没有任何 atomic 或跨 SM 通信——这正是 4.3.1 所说的「输出列轴无归约并行」。

最后看 device 入口如何把 scheduler 注入共享 core、并把 grid 设为 `num_sms`：

[deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh:551-591](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L551-L591) — `make_scheduler` lambda 构造 `SM100PagedMQALogitsScheduler`；注意模板实参里 `kNumSMs = 0`（行 585）——core 里 `kNumSMs` 仅用于非分页 scheduler 的静态 `block_idx += kNumSMs` 步进，分页路径改由 `schedule_meta` 驱动 grid-stride，故传 0 占位。`BLOCK_Q = 128/kNumHeads` 与 `kNumPagesPerSplit = SPLIT_KV/PAGE_KV` 在此静态求出并断言整除。

#### 4.3.4 代码实践

**实践目标**：对比非分页与分页两条 MQA 路径在「split-K 与列写回」上的差异，理解为何分页 split-K 无需归约。

**操作步骤**：

1. 打开非分页 scheduler [deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh:42-63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh#L42-L63)，注意它 `next_q_block` 里 `current_q_block_idx += kNumSMs` 的**静态 grid-stride**，以及 `num_kv_splits = ceil_div(end - kv_token_base, SPLIT_KV)` 也是把 KV 切段、但起点 `kv_token_base` 来自 `cu_seq_len_k_start` 而非 metadata。
2. 打开分页 scheduler [sm100_paged_mqa_logits.cuh:249-292](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_paged_mqa_logits.cuh#L249-L292)，对比：分页路径的「本 SM 算哪些 split」由 `schedule_meta` 的 `[start,end)` **动态**决定，而非 `+= kNumSMs`。
3. 在两份 scheduler 里分别找到 `get_logits_col`：非分页版（行 77-81）与分页版（行 317-321）公式形式相同——`(base + split_idx)*SPLIT_KV + thread_idx`，说明**两者都是「切段即切输出列、无归约」**；区别仅在 `base` 的来源（非分页=`kv_token_base`，分页=`kv_split_base`）。
4. （可选，需 SM100）参考 [tests/test_attention.py:291-445](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L291-L445) 的 `test_paged_mqa_logits`，把 `context_len` 设成一个远大于 `SPLIT_KV` 的值（如 8192），用 `DG_JIT_DEBUG=1` 运行，观察 metadata 如何把该请求的多个 split 派给不同 SM。

**需要观察的现象**：非分页用静态 `+= kNumSMs` 步进，分页用 `schedule_meta` 半开区间步进；两者的 `get_logits_col` 都把不同 split 映射到不重叠列段，写回处均无 atomic。

**预期结果**：你能用一句话说清——「分页 MQA 的 split-K 只是按 KV 段切输出列，每个 (q_token, kv_token) 由且仅由一个 (SM, split) 写入，因此不需要任何跨 SM 归约」。若无 GPU，完成步骤 1–3 的源码对照即可。

#### 4.3.5 小练习与答案

**练习 1**：`split_kv=256` 与 `splits_per_chunk=16` 各自限制的是什么？为什么不让一个任务直接发射一个请求的全部 split？

**答案**：`split_kv=256` 是 KV 段（也是一个 UMMA 的 N/block 粒度相关常量、TMA 搬运单元）大小；`splits_per_chunk=16` 是**单个任务内**最多发射的 split 数（请求只有 1 个 Q-block 时才放开为 `remaining`）。把整请求的 split 一次性发射会让内层 `for (kv_split_idx ...)` 循环过长、KV 共享内存多缓冲队列压力变大；按 16 个 split 分 chunk 既限制了循环与寄存器压力，又让多个 SM 能在一个请求内并行接力不同 chunk。

**练习 2**：如果把 `SPLIT_KV` 改小（如 64），`get_logits_col` 仍正确吗？会有什么副作用？

**答案**：列映射公式 `(base+split_idx)*SPLIT_KV + thread_idx` 与 `SPLIT_KV` 强耦合，改小后公式仍自洽（列段更窄）。但 `SPLIT_KV` 还绑定 UMMA 累加器列布局、TMA 搬运量、`kNumPagesPerSplit` 等多处编译期假设（见 core 里的 `DG_STATIC_ASSERT(SPLIT_KV == kNumMathWarpGroups*UMMA_M ...)`），单独改它会被静态断言拦下；它是一个被多处约束的全局常量，不能随意调。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「从 fused 缓冲到 SM 调度」的端到端追踪。

**任务**：给定一个 2 请求、上下文长度悬殊的分页 MQA 场景，画出数据从 `fused_kv_cache` 抵达某个 SM 的某个 logits 列的完整路径。

**步骤**：

1. 设 `batch=2, next_n=1, head_dim=128, num_heads=64, block_kv=64`，请求 0 `context_len=256`、请求 1 `context_len=4096`，FP8 路径。计算 `BLOCK_Q=128/64=2`、每请求的 `num_kv_splits`（请求 0 = 1、请求 1 = 16）、`kNumPagesPerSplit = 256/64 = 4`。
2. **fused 拆视图**：按 4.1，说明 `kv_cache` 与 `kv_cache_sf` 的指针偏移与步长。
3. **metadata**：按 4.2.2 手算 `work=[1, 16]`（`num_q_tokens` 均为 1）、前缀和、`schedule_meta` 各行（在你设备的真实 `num_sms` 下），指出长请求 1 的 16 个 split 是如何被摊到多个 SM 的。
4. **任务遍历 + 页寻址**：任选一个被分配到请求 1 中部 split 的 SM，跟踪它 `next_q_block` 得到的 `cur_block_table_row`（= 请求 1 的 block_table 行）、`get_kv_page_coord_by_page_offset` 查到的 4 个物理页号，以及最终 `get_logits_col` 写回的列段 `[s*256, s*256+256)`。
5. **无归约验证**：指出请求 1 的 16 个 split 对应 16 段不重叠列，没有任何 atomic。

**交付物**：一张含「fused 缓冲 → 拆视图 → metadata 前缀和 → SM 区间 → 页查表 → 列写回」的流程图，并标注每一步对应的源码行号。若在 SM100 上运行 [tests/test_attention.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py) 的 `test_paged_mqa_logits`，可用 `ref_paged_mqa_logits`（行 248-276）作为参考输出验证数值。

## 6. 本讲小结

- **fused 布局**：`fused_kv_cache` 是 `[num_kv_blocks, block_kv, 1, head_dim_with_sf]` 的 `kByte` 张量，每物理块内部为「数值区 + SF 区」紧凑排列；DeepGEMM 用 `from_blob` 零拷贝拆成 `kv_cache`（数值）与 `kv_cache_sf`（SF）两个 TMA 视图，KV 用 3D 描述符、外维即物理块号。
- **页寻址**：`block_table` 把「逻辑页偏移」映射到「物理块号」，设备侧 `get_kv_page_coord_by_page_offset` 查表；为减少全局访存，一个 warp 一次性把最多 32 个页号预取进 lane 寄存器、用 `__shfl_sync` 广播。
- **调度元数据**：`get_paged_mqa_logits_metadata` 跑一个 1-block metadata kernel，按 `work = num_kv_splits * num_q_tokens` 做前缀和并均摊到 SM，产出 `[num_sms+1, 2]` 的 `(q_token_idx, kv_split_idx)` 起点/哨兵；主 kernel grid 恰为 `num_sms`，每个 CTA 读 `[start, end)` 区间动态领任务。
- **varlen**：`indices` 张量把变长序列拍平，请求 = 等值连跑；varlen 仅 SM100、`next_n==1` 支持。
- **split-K 无归约**：`SPLIT_KV=256`、`splits_per_chunk=16`，每个 split 写到不重叠的输出列段 `(base+split_idx)*SPLIT_KV + thread`，多个 SM 并行处理同一请求的不同 KV 段而**无需任何跨 SM 归约**——因为输出 `out[i,j]` 逐 (i,j) 独立。
- **与非分页的关键差异**：非分页 KV 连续、用 2D TMA + `cu_seq_len_k_start/end`、静态 `+= kNumSMs` 步进；分页 KV 离散、用 3D TMA + `block_table`、`schedule_meta` 动态步进，且需多一次 metadata kernel 调用。

## 7. 下一步学习建议

- **工程化与剖析**：本讲的 metadata/主 kernel 依赖 PDL 依赖链（`cudaGridDependencySynchronize`）。若想系统掌握这类调试旋钮，进入 [u10-l4 环境变量、调试与性能剖析](u10-l4-env-vars-debug-profiling.md)，用 `DG_JIT_DUMP_SASS=1` + NCU 观察 metadata 与主 kernel 的 overlap、以及寄存器/TMEM 占用。
- **回归测试与数值校验**：想为新的 `(num_heads, head_dim, block_kv)` 组合补测试，进入 [u10-l3 测试、基准与数值校验](u10-l3-testing-benchmark-numeric.md)，复用 `tests/generators.py` 与 `calc_diff`。
- **底层 PTX**：若对 4.1 的按页 TMA、UTCCP（FP4 SF 进 TMEM）细节感兴趣，可重读 [u6-l3 PTX 内联函数：TMA 加载与栅栏](u6-l3-ptx-tma-and-barriers.md) 与 [u6-l2 MMA 抽象](u6-l2-mma-wgmma-vs-umma.md) 中 SM100 UMMA/tcgen05 部分。
