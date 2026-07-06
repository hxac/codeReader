# Dispatch copy epilogue 与 expand 布局

> 本讲是 U5「Dispatch 内核链路深入」的第三讲，承接 [u5-l1 直接模式 Dispatch](./u5-l1-direct-dispatch.md)。前置还包括 [u3-l2 缓冲区内存布局](./u3-l2-buffer-layout-sizing.md)（`TokenLayout`/`BufferLayout`）、[u4-4 内核启动框架](./u4-l4-launch-framework.md)（`LaunchArgs`/PDL）。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 dispatch 为什么被拆成「主 kernel + copy epilogue」两个内核，以及二者如何用 PDL 串接。
- 区分**非 expand（compact）**与 **expand（按专家展开）**两种接收布局，理解它们对下游 per-expert GEMM 输入的影响。
- 解释 `expert_alignment` 对齐、`do_zero_padding` 零填充的实现机制，以及为什么 `do_zero_padding` 必须依赖 `do_expand`。
- 读懂 `psum_num_recv_tokens_per_expert` 在 expand / 非 expand 两种模式下语义为何不同（这是本讲最微妙的一点）。
- 能在 `tests/elastic/test_ep.py` 中定位并运行相关用例，验证 padding 区被清零。

## 2. 前置知识

### 2.1 主 dispatch kernel 把 token 写到了哪里

在 [u5-l1](./u5-l1-direct-dispatch.md) 里我们讲过：主 dispatch kernel 的「dispatch warps」把 token 经 NVLink/RDMA 写进**对称缓冲区（NCCL 窗口内的 GPU buffer）**，而不是直接写到用户最终拿到的 `recv_x`。这个缓冲区里每个 token 是按 `TokenLayout` 打包的：`[hidden | SF | metadata | mbarrier]` 四段连续存放（详见 [u3-l2](./u3-l2-buffer-layout-sizing.md)）。

这种打包格式适合 TMA 大块拷贝和跨 rank 写入，但**不适合直接喂给 MoE 的 per-expert GEMM**——GEMM 期望的是「同一个专家的所有 token 在内存里连续排布」的纯 hidden 张量。

### 2.2 为什么主 kernel 不能顺便把拷贝做了

主 dispatch kernel 的设计目标是**省 SM**（把 SM 让给计算流，详见 `prefer_overlap_with_compute`），所以它只占用解析式算出的 `num_sms`（通常远小于全卡 SM 数）。而把 token 从打包 buffer「拆包重排」到用户张量是一个**带宽密集、可以吃满所有 SM** 的纯拷贝任务。把这两件事塞进同一个 kernel，要么牺牲通信 kernel 的低 SM 占用，要么牺牲拷贝的吞吐。因此 DeepEP 选择拆成两个 kernel：

| 内核 | 职责 | SM 数 | 启动方式 |
| --- | --- | --- | --- |
| `dispatch_impl` / `hybrid_dispatch_impl` | 跨 rank 通信，写对称 buffer | 解析式 `num_sms`（省） | `cooperative=true`、`cluster_dim=2-(num_sms%2)` |
| `dispatch_copy_epilogue_impl` | 把 buffer 拷出/重排到 `recv_x` 等 | 全卡 `get_num_sms()`（满） | `cooperative=false`、`cluster_dim=1`、**`pdl_enabled=true`** |

### 2.3 PDL（Programmatic Dependent Launch）

PDL 是 Hopper 引入的「内核依赖启动」机制：第二个内核可以在第一个内核**即将结束时**就被调度上 SM，并在代码里显式等待第一个内核完成的数据可见性。DeepEP 的 epilogue 用 `cudaGridDependencySynchronize()` 来阻塞，直到主 dispatch kernel 把所有 token 写完并释放可见性。这样主 kernel → epilogue 之间**不需要 CPU 侧介入**，整条链路一直在 GPU 上跑。

> 小白提示：「epilogue」直译是「尾声」，在 DeepEP 里指通信主内核之后紧跟的一个善后内核，负责把内部 buffer 重新整理成用户张量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/kernels/elastic/dispatch.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | 定义 `DispatchCopyEpilogueRuntime`（JIT 代码生成 + 启动）与 `launch_dispatch_copy_epilogue` host 入口。 |
| [`deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh) | epilogue 真正的 GPU 内核 `dispatch_copy_epilogue_impl`：TMA 拷贝、expand 重排、零填充全在这里。 |
| [`deep_ep/include/deep_ep/common/layout.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | `TokenLayout`（单 token 四段打包）与 `BufferLayout`（按 rank/token 展开），epilogue 靠它们定位 buffer 内每段数据。 |
| [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer::dispatch` 的 host 编排：分配输出张量、决定 expand/非 expand 的张量形状、调用 epilogue。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1) copy epilogue 主流程**、**(4.2) expand 布局**、**(4.3) expert_alignment 对齐与 do_zero_padding**。

### 4.1 copy epilogue 主流程：从打包 buffer 到用户张量

#### 4.1.1 概念说明

epilogue 内核 `dispatch_copy_epilogue_impl` 做的事情可以用一句话概括：**把对称 buffer 里打包好的 token，逐个 TMA 读进共享内存，再按目标布局 TMA 写出到 `recv_x` / `recv_sf` / `recv_topk_idx` / `recv_topk_weights` / `recv_src_metadata`**。

它接收的关键输入有：

- `buffer`：对称 GPU buffer（主 kernel 写入的源）。
- `psum_num_recv_tokens_per_scaleup_rank`：来自主 kernel 的 notify warps，按 scaleup rank 累计的接收 token 数（前缀和）。epilogue 靠它知道「第 i 个 token 属于哪个源 rank」。
- `psum_num_recv_tokens_per_expert`：每专家 token 计数前缀和（expand 模式用作原子计数器，见 4.2）。
- `num_unaligned_recv_tokens_per_expert`：每专家**未对齐的实际**计数（零填充要用，见 4.3）。
- 一组输出指针：`recv_x`、`recv_sf`、`recv_topk_idx`、`recv_topk_weights`、`recv_src_metadata`。
- `num_recv_tokens`：本 rank 实际收到的 token 总数。

#### 4.1.2 核心流程

epilogue 的 grid 是 `(num_sms, 1, 1)`，每个 CTA 含 `num_warps` 个 warp，warp 之间用步长法（stride loop）平摊所有 token。每个 warp 处理一段连续的 token：

```
启动 → cudaGridDependencySynchronize()   # 等主 kernel 完成
对每个 token i（warp 间步长为 num_warps * num_sms）:
    1. 由 psum_num_recv_tokens_per_scaleup_rank 二分定位 i 属于哪个源 rank
    2. TMA load：把该 token 的整段打包数据（hidden+SF+metadata）从 buffer 读进 smem
    3. 从 metadata 读出该 token 的目标专家 idx（每个 lane 一个 top-k 选择）
    4. 计算 dst_tensor_idx（写到 recv_x 的哪一行，见 4.2）
    5. TMA store hidden → recv_x[dst_tensor_idx]
    6. 散写 SF / topk_weights 到对应输出
    7. 写 recv_src_metadata[i]：源 token 全局 idx、源 rank+topk、（expand 时）dst_tensor_idx
若 do_zero_padding 且 do_expand：进入第二阶段，把每个专家尾部的对齐缝隙清零（见 4.3）
```

注意第 1 步的「rank 定位」：`psum_num_recv_tokens_per_scaleup_rank` 是一个**单调递增前缀和**（如 `[100, 250, 380, ...]`），epilogue 用一个游标 `current_rank_idx` 顺次推进——当 token 全局序号 `i` 越过当前 rank 的累计上界，就切到下一个 rank。这是个 O(num_recv_tokens + num_scaleup_ranks) 的线性扫描，比每个 token 二分更快。

#### 4.1.3 源码精读

**host 侧如何决定 epilogue 的 warp 数**——把共享内存「塞满」以最大化拷贝带宽：

[deep_ep/include/deep_ep/common/layout.cuh 引用 / csrc/kernels/elastic/dispatch.hpp:312-316](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L312-L316) —— `num_warps = min(num_smem_bytes / token_layout.get_num_bytes<true>(), 32)`，即「一个 CTA 的共享内存能放下几个 token 的 TMA 暂存区，最多 32 个 warp」。

**启动配置走 PDL，不走 cooperative**：

[csrc/kernels/elastic/dispatch.hpp:336](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L336) —— `LaunchArgs(num_sms, num_threads, num_smem_bytes, 1, false, true)`：`cluster_dim=1`、`cooperative=false`、`pdl_enabled=true`。对比主 dispatch kernel 的 `cooperative=true`，二者启动模型完全不同。

**内核入口先等主 kernel**：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:58-64](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L58-L64) —— `cudaGridDependencySynchronize()` 阻塞直到主 dispatch kernel 完成；注释明确「PDL is used, please do not use `__ldg`」（PDL 下的可见性要靠 grid sync 保证，不能用普通 `__ldg` 读）。紧接着若处于「无 CPU sync」最坏情况，从 GPU 张量读真实 `num_recv_tokens`。

**两套 BufferLayout 同时存在**——一个指向 smem 暂存区，一个指向全局 buffer：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:44-49](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L44-L49) —— `tma_buffer`（`BufferLayout<true>`，带 mbarrier，base 在 smem）与 `scaleup_buffer`（`BufferLayout<false>`，base 在全局 `buffer`，按 `num_scaleup_ranks` 展开）。`BufferLayout<...>::get_rank_buffer(r).get_token_buffer(t)` 提供 O(1) 的偏移定位（见 `layout.cuh:288-310`）。

**TMA 读 + 解析专家 + TMA 写**的主循环骨架：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:87-143](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L87-L143) —— 先 `tma_load_1d` 把整段 token 拉进 smem 并 `mbarrier_arrive_and_set_tx`；趁 TMA 延迟用 `__ldg`/寄存器读 top-k 专家 idx；算出 `dst_tensor_idx` 后 `tma_store_1d` 写 hidden 到 `recv_x`。

> 关键：epilogue 是**纯本地拷贝**，不再做任何跨 rank 通信。所有跨 rank 流量都已在主 kernel 完成。

#### 4.1.4 代码实践

**实践目标**：观察「主 dispatch」与「copy epilogue」两个内核各自的耗时与带宽分工。

**操作步骤**：

1. 在单机 8 卡运行 `tests/elastic/test_ep.py`（运行方式见 [u1-l4](./u1-l4-run-first-test.md)）：

   ```bash
   python tests/elastic/test_ep.py --num-tokens 4096 --hidden 7168 --num-experts 256 --num-topk 6
   ```

2. 关注输出里每行末尾的 `copy: ... GB/s, ... us` 字段。它来自 `bench_kineto` 同时测量两个内核名：

   [tests/elastic/test_ep.py:256-263](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L256-L263) —— `kernel_names=('dispatch_impl', 'dispatch_copy_epilogue_impl')`，`t` 是整体时间、`copy_t` 是 epilogue 单独时间。

**需要观察的现象**：`copy` 行的带宽通常明显大于 dispatch 的 SO/SU 逻辑带宽（因为 epilogue 是纯 HBM↔HBM 拷贝，吃满 SM，不受网络限制）；`copy_t` 远小于 `t`。

**预期结果**：epilogue 耗时只占整条 dispatch 链路的一小部分，证明「拆成两个内核」是划算的。

**待本地验证**：具体数字依赖硬件，请在你的机器上记录实际值。

#### 4.1.5 小练习与答案

**练习 1**：epilogue 用 `cooperative=false`，主 dispatch kernel 用 `cooperative=true`。为什么 epilogue 不需要 cooperative launch？

**答**：cooperative launch 用于「整个 grid 同步」（grid-wide barrier），主 dispatch kernel 需要它来做跨 rank 的归约与信号握手；epilogue 是无状态的纯拷贝，每个 warp 独立处理一段 token，warp 内 `__syncwarp()` 即可，不需要 grid 级同步，所以用更轻的 PDL 启动即可。

**练习 2**：epilogue 第 1 步用「线性游标」而不是「每个 token 二分查找」来定位源 rank，这样做的前提是什么？

**答**：前提是 token 按全局序号 `i` 顺序遍历，而 `psum_num_recv_tokens_per_scaleup_rank` 单调递增，因此游标只会前进不会回退，均摊 O(1)；若乱序访问则退化为二分。

---

### 4.2 expand 布局：按专家连续排布，喂给 per-expert GEMM

#### 4.2.1 概念说明

MoE 模型里，每个 token 被路由到若干专家，每个专家是一个独立的 FFN（一次 GEMM）。为了让 GEMM 高效，**同一个专家收到的所有 token 必须在内存里连续**，这样一次 `torch.mm` 就能算完一个专家。

DeepEP 提供两种接收布局：

| 模式 | `recv_x` 行顺序 | `recv_topk_idx` | 典型用途 |
| --- | --- | --- | --- |
| **非 expand（compact）** | 按 token **到达顺序**排列；`recv_topk_idx[i]` 告诉第 i 个 token 属于哪个专家 | `[num_recv_tokens, num_topk]`（每行多个 top-k） | 训练前向/反向、需要对每个 (token, topk) 单独处理 |
| **expand** | 按**专家分组**连续排列：`[专家0 的 token... | 专家1 的 token... | ...]` | **返回 `None`** | 推理 prefill/decode，直接切片做 per-expert GEMM |

在 expand 模式下，用户拿到 `handle.num_recv_tokens_per_expert_list`（每专家 token 数），就能对 `recv_x` 切片：`recv_x[start_e:start_e+count_e]` 就是专家 e 的全部输入，直接进 GEMM。

#### 4.2.2 核心流程

「目标行号」`dst_tensor_idx` 的计算是 expand 的核心，分三种情况：

```
若 非 expand:        dst_tensor_idx = i                      # 谁先到谁排前，按到达顺序
若 expand + cached:  dst_tensor_idx = recv_src_metadata 里预存的值   # 复用旧布局
若 expand + 非 cached: dst_tensor_idx = atomicAdd(psum[expert], 1)   # 抢占式分配槽位
```

第三种最关键：`psum_num_recv_tokens_per_expert` 在 expand 模式被当作**每专家的原子计数器**。每个 token 用 `atomicAdd` 在自己专家的计数器上抢一个槽位，抢到的值就是它在 `recv_x` 里的行号。因为每专家的计数器初值是该专家区域的**起始偏移**（base offset），所以同一个专家的 token 自然连续落在一起。

这就引出本讲最微妙的一点：**同一个张量 `psum_num_recv_tokens_per_expert`，在 expand 与非 expand 下语义不同**。host 侧的注释说得很直白：

[csrc/elastic/buffer.hpp:813](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L813) —— `// NOTES: for expand mode, the input is exclusive prefix sum, while for non-expand, it is inclusive`。

主 dispatch kernel 的 notify warps 往一个长度为 `num_local_experts + 1` 的张量里写**对齐后的**每专家计数的**前缀和（exclusive）**，形如：

\[
\text{psum} = [\,0,\ c_0,\ c_0{+}c_1,\ c_0{+}c_1{+}c_2,\ \dots\,]
\]

其中 \(c_e = \lceil u_e / a\rceil \cdot a\) 是专家 e 的**对齐后**计数，\(u_e\) 是实际（未对齐）计数，\(a\) 是 `expert_alignment`。然后 host 侧根据模式**切不同的切片**：

[csrc/elastic/buffer.hpp:1114-1122](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1114-L1122) —— expand 切 `[0, num_local_experts)`（exclusive 部分，即 base offsets，喂给 epilogue 当原子计数器）；非 expand 切 `[1, num_local_experts+1)`（inclusive 部分，返回给用户作信息）。

两种模式下的语义对照：

| 模式 | 切片 | epilogue 是否使用 | 返回给用户的含义 |
| --- | --- | --- | --- |
| 非 expand | inclusive 尾段 `[c_0, c_0+c_1, ...]` | 否（`dst_tensor_idx=i`） | 对齐后的累计边界，相邻差 = \(c_e\)（对齐计数） |
| expand | exclusive 头段 `[0, c_0, c_0+c_1, ...]` | 是（作 `atomicAdd` 计数器） | 被 epilogue 原子改写为 `base_e + u_e`，经特定公式可还原 \(u_e\)（未对齐计数） |

> 注意：expand 模式下，epilogue 对这个张量做了 in-place 原子加法，所以返回时它的值已经不是纯前缀和了——这是 4.2.4 实践要验证的重点。

#### 4.2.3 源码精读

**`dst_tensor_idx` 的三分支**：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:112-123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L112-L123) —— 非 expand 用 `elect_one_sync()` 让一个 lane 取 `dst_tensor_idx = i`；expand+cached 从 `recv_src_metadata[i, 2+lane]` 读预存值；expand+非 cached 用 `atomicAdd(psum_num_recv_tokens_per_expert + dst_expert_idx, 1)`。

**写回 dst_tensor_idx 供 combine/cached 复用**：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:204-206](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L204-L206) —— expand 模式把每个 top-k 选择算出的 `dst_tensor_idx` 写进 `recv_src_metadata[i, 2+lane]`，这样后续 combine 能据此把专家输出送回正确位置，cached 模式也能直接复用。

**host 侧按 expand 决定输出张量形状**：

[csrc/elastic/buffer.hpp:1075-1111](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1075-L1111) —— 关键三处：
- `num_allocated_tokens = do_expand ? num_expanded_tokens : num_recv_tokens`（expand 要为对齐后的全部槽位分配）。
- `recv_topk_idx` **只在非 expand 分配**（`if (not do_expand)`，L1102）；expand 时返回 `None`。
- `recv_topk_weights` 在 expand 下是 **1D** `[num_allocated_tokens]`，非 expand 下是 **2D** `[num_allocated_tokens, num_topk]`（L1107-L1109）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 expand 与非 expand 两种布局的形状差异，并理解 `psum_num_recv_tokens_per_expert` 的语义差别。

**操作步骤**：

1. 在 `tests/elastic/test_ep.py` 的 `test_dispatch_combine` 里，dispatch 被调用了两次——一次非 expand、一次 expand：

   [tests/elastic/test_ep.py:144-160](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L144-L160) —— `dispatch_args`（无 `do_expand`）与 `expanded_dispatch_args = dispatch_args | dict(do_expand=True, use_tma_aligned_col_major_sf=True)`。

2. 运行测试后，在断言处观察两个 handle 的张量形状。关键断言：

   [tests/elastic/test_ep.py:412-413](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L412-L413) —— `assert expanded_recv_topk_idx is None`（expand 不返回 topk_idx）。

3. 对照 `psum_num_recv_tokens_per_expert` 的两种用法：

   [tests/elastic/test_ep.py:454-462](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L454-L462) —— 非 expand：`count = psum[i+1] - psum[i]`，断言 `== align(ref_count, expert_alignment)`（对齐计数 \(c_e\)）；expand：`expanded_count = psum[i+1] - align(psum[i], expert_alignment)`，断言 `== ref_count`（未对齐实际计数 \(u_e\)）。

**需要观察的现象**：expand 模式下 `expanded_recv_x` 的行数 `num_expanded_tokens` ≥ 非 expand 的 `num_recv_tokens`（多了对齐 padding）；expand 的 `recv_topk_weights` 是 1D，非 expand 是 2D。

**为什么 expand 的还原公式是 `psum[i+1] - align(psum[i], a)`**：因为 expand 下 `psum[i]` 已被 epilogue 原子改写成 `base_i + u_i`，其中 `base_i = align(base_{i-1} + u_{i-1})` 恰好等于 `align(psum[i-1])`，于是 `psum[i+1] - align(psum[i], a) = (base_{i+1} + u_{i+1}) - base_{i+1} = u_{i+1}`（请对照 4.2.2 的数学推导自行验算一遍）。

**预期结果**：两条断言都通过，证明语义差别被代码正确实现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 expand 模式下 `recv_topk_idx` 返回 `None`？

**答**：expand 布局已经把同一专家的 token 物理上排在一起，`recv_x` 的某一段就唯一对应一个专家，专家身份由段位置（`psum_num_recv_tokens_per_expert`）隐含给出，不再需要逐 token 标注 `topk_idx`。而非 expand 模式 token 按到达顺序混排，必须用 `recv_topk_idx[i]` 显式标注每个 token 属于哪个专家。

**练习 2**：expand + cached 模式为什么不用 `atomicAdd`，而是直接从 `recv_src_metadata` 读 `dst_tensor_idx`？

**答**：cached 模式复用首次 dispatch 算好的布局，`dst_tensor_idx` 在首次 dispatch 时已写进 `recv_src_metadata[i, 2+lane]`。重放时直接读，既省掉原子操作，又保证两次 dispatch 的位序**完全一致**（这是确定性排序 / CUDA graph 复用的前提）。

---

### 4.3 expert_alignment 对齐与 do_zero_padding 零填充

#### 4.3.1 概念说明

现代 grouped GEMM（按专家分组的矩阵乘）为了高效分块，常要求**每个专家的 token 数是某个粒度（如 128）的整数倍**。DeepEP 用 `expert_alignment` 参数表达这个粒度（test 里取 128 或 1）。

对齐意味着：若专家 e 实际收到 \(u_e\) 个 token，则它在 `recv_x` 里**占用** \(c_e = \lceil u_e/a\rceil\cdot a\) 行，多出来的 \(c_e - u_e\) 行是**对齐缝隙（padding）**。这些缝隙如果不处理，里面是未初始化内存，会让 GEMM 算出垃圾。

`do_zero_padding=True` 就是让 epilogue 把这些缝隙**显式清零**（hidden、weights、SF 都清），这样 padding 行参与 GEMM 后贡献为 0，不影响有效结果。

host 侧有一条硬约束：

[csrc/elastic/buffer.hpp:731](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L731) —— `EP_HOST_ASSERT(not do_zero_padding or do_expand)`：零填充只在 expand 模式有意义，因为只有 expand 才有「每专家连续区域 + 对齐缝隙」的概念；非 expand 是按到达顺序排，没有对齐缝隙。

#### 4.3.2 核心流程

零填充分两步走：

1. **主 dispatch kernel** 写两个计数：
   - `psum_num_recv_tokens_per_expert`：对齐后的前缀和（用于定位专家区域）。
   - `num_unaligned_recv_tokens_per_expert`：**未对齐的实际计数** \(u_e\)（零填充用它确定缝隙位置）。

2. **epilogue 第二阶段**（仅当 `kDoZeroPadding and kDoExpand`）：
   - 先 `tma_store_wait` 等主循环最后的 TMA 写完成。
   - 用 `ptx::st_bulk<kNumHiddenBytes>` 把 smem 里 TMA 暂存区的 hidden 段**清零**——之后所有零填充都 TMA store 这段「全零」内容，避免每次重新清。
   - 并行读入所有专家的 \(u_e\)，用一个「波浪（wave）扫描」算法定位每个 padding 槽在 `recv_x` 里的全局行号 `dst_tensor_idx`。
   - 对每个 padding 槽：TMA store 全零 hidden、写 0 的 weight、写 0 的 SF。

「波浪扫描」是为了高效计算「第 k 个 padding 槽属于哪个专家的哪个位置」。设专家 e 的 padding 数为 \(p_e = c_e - u_e\)，则全局第 k 个 padding 落在第一个满足「前 i 个专家 padding 之和 > k」的专家 i 里。epilogue 用 warp 内的 inclusive/exclusive prefix sum 快速二分定位，避免逐专家串行。

#### 4.3.3 源码精读

**host 侧分配未对齐计数张量**：

[csrc/elastic/buffer.hpp:818-831](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L818-L831) —— `num_unaligned_recv_tokens_per_expert` 由主 dispatch kernel 的 notify warps 写入（详见 [u5-l1](./u5-l1-direct-dispatch.md)），cached 模式下复用旧张量。

**epilogue 零填充整段**：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:231-324](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L231-L324) —— 进入条件 `if constexpr (kDoZeroPadding and kDoExpand)`。

- [L234](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L234)：`tma_store_wait()` 等主循环最后一批 TMA 写完成，避免清零覆盖还没写好的有效数据。
- [L238-L239](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L238-L239)：`ptx::st_bulk<kNumHiddenBytes>(tma_buffer.get_hidden_ptr())` 把 smem 的 hidden 段整体清零，后续所有 padding 槽都 TMA store 这段零。
- [L242-L250](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L242-L250)：并行读入所有专家的 \(u_e\)（`num_unaligned_recv_tokens_per_expert`）。
- [L253-L323](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L253-L323)：单层 `while` 波浪扫描，对每个 padding 槽算出 `dst_tensor_idx`，然后 TMA store 零 hidden、清零 weight、清零 SF。

**清零 weight 与 SF**：

[deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:298-318](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L298-L318) —— weight 直接赋 `0.0f`；SF 用 `zero_sf_pack = {0}` 逐 pack 写零（FP8 的 scaling factor 为 0 表示该 token 不贡献）。

#### 4.3.4 代码实践

**实践目标**：验证 expand + `do_zero_padding` 下，每个专家尾部的对齐缝隙确实被清零。

**操作步骤**：

1. `test_ep.py` 用 cached expand + 零填充触发该路径：

   [tests/elastic/test_ep.py:172-177](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L172-L177) —— `cached_expanded_dispatch_args = ... | dict(do_expand=True, use_tma_aligned_col_major_sf=True, do_zero_padding=True, handle=expanded_handle)`。

2. 关注专门的零填充断言：

   [tests/elastic/test_ep.py:423-428](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L423-L428) —— 对每个本地专家，取其未对齐起始 `start = psum_num_recv_tokens_per_expert[expert]`、对齐结束 `end = align(start, expert_alignment)`，断言 `recv_x[start:end]` 与 `recv_topk_weights[start:end]` 全为 0。

**需要观察的现象**：对每个专家，`[start, end)` 这段（即 padding 缝隙）的 hidden 与 weight 全为 0；`end - start` 恰好是该专家的对齐余数 \((a - u_e \bmod a) \bmod a\)。

**预期结果**：所有专家的 padding 区断言通过。

**待本地验证**：若 `expert_alignment=1`，则 \(c_e = u_e\)，没有 padding，断言区间为空——这正是 `--expert-alignment 1` 时的退化情形。

#### 4.3.5 小练习与答案

**练习 1**：为什么 epilogue 先用 `st_bulk` 把 smem 的 hidden 段清零，再反复 TMA store 它，而不是每个 padding 槽都现清？

**答**：TMA store 要求源地址在 smem 且对齐。先把 smem 的一整段 hidden 区清零一次，之后所有 padding 槽都复用这段「全零源」做 TMA store，每个槽只发一条 TMA 指令即可，避免逐元素清零的多次访存。

**练习 2**：若用户设 `do_zero_padding=True` 但 `do_expand=False` 会怎样？

**答**：host 侧 [buffer.hpp:731](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L731) 的 `EP_HOST_ASSERT(not do_zero_padding or do_expand)` 会直接抛错。因为非 expand 没有「每专家对齐区域」的概念，零填充无从谈起。

---

## 5. 综合实践

把三个模块串起来：手工推演一个小例子，并在源码里逐行对照。

**设定**：单机 8 卡（`num_scaleout_ranks=1, num_scaleup_ranks=8`），`num_local_experts=32`，`expert_alignment=128`，BF16，`hidden=7168`。假设某 rank 的 32 个本地专家实际收到的未对齐 token 数 \(u_e\) 前 3 个为 `[100, 200, 50]`，其余为 0。

**任务**：

1. **算对齐计数**：写出 \(c_0, c_1, c_2\)（答：`[128, 256, 128]`）。
2. **算 expand 前缀和**：写出主 kernel 写入的 exclusive 前缀和前 4 项（答：`[0, 128, 384, 512]`）。
3. **定位 padding**：专家 0 的 padding 区在 `recv_x` 的哪几行？（答：`[100, 128)`，共 28 行被 `do_zero_padding` 清零。）
4. **代码对照**：在 [dispatch_copy_epilogue.cuh:231-324](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L231-L324) 里找到「计算每个 padding 槽 `dst_tensor_idx`」的波浪扫描代码，确认它能把第 0～27 号 padding 槽分别映射到 `recv_x` 的第 100～127 行。
5. **运行验证**：用 `--num-tokens` 较小、`--expert-alignment 128` 跑 `test_ep.py`，在 [test_ep.py:423-428](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L423-L428) 的断言处打印每个专家的 `[start, end)`，与你手算的 padding 区对照。

**预期结果**：手算与运行结果一致，padding 区全零。

## 6. 本讲小结

- dispatch 被拆成**主 kernel（省 SM 做通信）+ copy epilogue（满 SM 做拷贝/重排）**两个内核，靠 **PDL**（`cudaGridDependencySynchronize`）在 GPU 侧串接，无需 CPU 介入。
- epilogue 用两套 `BufferLayout`（smem 暂存区 + 全局 buffer）配合 TMA load/store，把打包 token 拷出为用户张量。
- **非 expand** 布局按到达顺序排列、保留 `recv_topk_idx`；**expand** 布局按专家分组连续排列、`recv_topk_idx` 返回 `None`，直接切片即可喂 per-expert GEMM。
- `dst_tensor_idx` 的计算是非 expand 取 `i`、expand 用 `atomicAdd(psum[expert], 1)` 抢槽、cached 直接读旧值。
- **同一个 `psum_num_recv_tokens_per_expert` 张量**在两种模式下语义不同：非 expand 切 inclusive 段返回对齐边界，expand 切 exclusive 段当原子计数器并被 in-place 改写。
- `expert_alignment` 让每专家 token 数对齐到粒度（如 128），`do_zero_padding` 把对齐缝隙清零，二者**只对 expand 生效**。

## 7. 下一步学习建议

- **下一讲 [u5-l4 CPU 同步、cached handle 与推理解码复用](./u5-l4-cpu-sync-cached-handle.md)**：本讲多次提到 cached 模式「读预存 `dst_tensor_idx`」，下一讲会讲清首次 dispatch 如何生成 handle、后续如何复用以跳过 CPU 同步与张量重算。
- **combine 方向 [u6-l1 Combine 主流程](./u6-l1-combine-main.md)**：epilogue 写进 `recv_src_metadata` 的源 token 全局 idx、源 rank、`dst_tensor_idx` 正是 combine 反向路由的「发货单」，建议对照阅读 `combine.cuh`。
- **底层原语 [u8-l1 PTX 原语](./u8-l1-ptx-tma-mbarrier.md)**：本讲用到的 `tma_load_1d`/`tma_store_1d`/`mbarrier_*`/`st_bulk`/`cudaGridDependencySynchronize`（PDL）都封装在 `deep_ep/common/ptx.cuh`，深入理解它们有助于你改写自定义内核。
- **若要动手改**：尝试在 epilogue 主循环里加一行「按专家统计拷出字节数」的 `atomicAdd`，用 `EP_BUFFER_DEBUG=1` 打印，验证你对 expand 区域边界的理解。
