# POD 混合批处理注意力

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚在一个 continuous batching（持续批处理）的推理迭代里，**prefill 请求**与 **decode 请求**为什么会同时出现，以及为什么把它们拆成两次 kernel launch 会浪费 GPU。
- 理解 POD（Prefill-On-Decode）的核心思想：**用一次 kernel launch 把 prefill 和 decode 的工作塞进同一个 grid**，靠一个「SM 感知的线程块调度器」把每一块（CTA）动态分配给 prefill 或 decode，从而把 GPU 打满。
- 读懂 FlashInfer 提供的两个 POD wrapper——`PODWithPagedKVCacheWrapper`（单 prefill + 批 decode）与 `BatchPODWithPagedKVCacheWrapper`（批 prefill + 批 decode）——的 `plan`/`run` 两段式用法。
- 能把 POD 的输出与「分别调用 prefill/decode」的正确性对上，并理解融合带来的延迟收益来源。

本讲是进阶注意力变体单元的一篇，承接 u3-l4（BatchPrefill 的 plan/run）和 u3-l3（BatchDecode 的 plan/run）。你需要先理解 plan/run 两段式 API、页表三件套、以及 prefill 与 decode 在调度上的差异。

## 2. 前置知识

在进入 POD 之前，先用三句话复习几个关键概念：

- **continuous batching（持续批处理）**：现代 LLM 服务（如 vLLM、SGLang）不会等一个批次里所有请求都生成完才换批，而是**每一步都可以插入新请求**。于是同一个 forward 迭代里，往往同时存在两类工作：
  - **prefill 请求**：刚进来的新请求，要把一整段 prompt（可能几千 token）的注意力一次性算完。它有大量 query 行，是 **compute-bound（计算受限）** 的，能把 SM（流式多处理器）打满。
  - **decode 请求**：已经生成了一段时间的老请求，每步只新增 1 个 token，query 只有 1 行。它是 **memory-bound（访存受限）** 的——算力用不满，瓶颈在把很长的 KV cache 从显存搬进来。
- **SM（Streaming Multiprocessor，流式多处理器）**：GPU 的计算单元。一张卡有几十到上百个 SM（如 A100 有 108 个，H100 有 132 个）。kernel launch 时，GPU 会把 grid 里的线程块（thread block，FlashInfer 代码里叫 CTA）往 SM 上派发，每个 SM 能同时跑若干个 CTA。
- **kernel launch 开销与 SM 占用**：每一次「启动一个 CUDA kernel」都有固定的 launch 延迟；而一个 memory-bound 的 decode kernel 往往**占不满所有 SM**——decode 在跑时大量 SM 是闲着的。

POD 想解决的就是这最后一点：当 decode 把 GPU 闲下来时，正好可以让那些 SM 去算 prefill。与其先 launch 一个 decode kernel、再 launch 一个 prefill kernel，不如**一次 launch 一个「既包含 prefill 块、又包含 decode 块」的混合 kernel**，让调度器自己去把每块派给 prefill 或 decode。

> 术语小贴士：POD 论文原文见 <https://arxiv.org/abs/2410.18038>（POD-Attention）。FlashInfer 的 `docs/api/pod.rst` 把它概括为「把 single-request prefill kernel 与 batch-decode kernel 在**同一次 launch 里并发执行**，适用于把 chunked prefill 与正在进行的 decode 重叠起来的服务栈」。

## 3. 本讲源码地图

本讲涉及的关键文件，以及它们各自在「Python → TVM-FFI 绑定 → CUDA 模板」分层中的位置：

| 文件 | 层 | 作用 |
|------|----|------|
| `flashinfer/pod.py` | Python wrapper | 两个 wrapper 类 `PODWithPagedKVCacheWrapper`、`BatchPODWithPagedKVCacheWrapper`，提供 `plan`/`run`，管理双侧 workspace 与页表缓冲。 |
| `flashinfer/jit/attention/modules.py` | JIT 代码生成 | `gen_pod_module` / `gen_batch_pod_module` 与 `get_pod_uri`：按编译期参数（dtype、head_dim、posenc、滑窗等）生成类型特化的 POD 模块。 |
| `csrc/pod.cu` | TVM-FFI 绑定（单路径） | 导出 `pod_with_kv_cache_tensor`：把 Python 张量拆成 prefill/decode 两套 `Params`，调用 `PODWithKVCacheTensorDispatched`。 |
| `csrc/batch_pod.cu` | TVM-FFI 绑定（批路径） | 导出 `batch_pod_with_kv_cache_tensor`：双侧 paged 参数，调用 `BatchPODWithKVCacheTensorDispatched`，并多带一个 `sm_aware_sched` 调度缓冲。 |
| `include/flashinfer/attention/pod.cuh` | header-only kernel | **SM 感知调度器核心** `PODWithKVCacheTensorKernel`：一个 kernel grid 里同时含 prefill 与 decode 的 CTA，运行期决定每块干哪种活。 |
| `include/flashinfer/attention/batch_pod.cuh` | header-only kernel | 批路径对应的 `BatchPODWithKVCacheTensorKernel`，调度逻辑与单路径同构。 |
| `tests/utils/test_pod_kernels.py` | 测试 | 把 POD 的输出与「分别用 `single_prefill` + `BatchDecode` 得到的参考结果」逐元素对比，验证融合不改结果。 |
| `benchmarks/bench_mixed_attention.py` | 基准 | 把 BatchPOD 与 `BatchPrefill`、`BatchAttention` 在同一混合批次上比耗时。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**混合批次场景与动机**、**POD 融合与 SM 感知调度**、**prefill+decode 双侧 API（两个 wrapper 的 plan/run）**。

### 4.1 混合批次：prefill 与 decode 为什么并存

#### 4.1.1 概念说明

回想 u3-l1 讲过的注意力三阶段：prefill（长 query 序列，compute-bound）、decode（单 query，memory-bound）、append（写 KV）。在一个持续批处理的推理服务里，**同一次 forward 迭代**常常需要同时处理若干个 prefill 请求和若干个 decode 请求——因为服务端不会把这两类工作排队分开跑，否则 decode 的低延迟优势就没了。

朴素的实现是「两次 launch」：

1. 先 launch 一个 batch prefill kernel 处理所有 prefill 请求；
2. 再 launch 一个 batch decode kernel 处理所有 decode 请求。

问题在于资源利用的失衡：

- **decode kernel 跑的时候，SM 大量空闲**。decode 每个请求只有 1 个 query 行，每个 head 分到的工作量很小，单靠 split-kv 切分也未必能填满上百个 SM。这些闲着的 SM 在「等显存」。
- **prefill kernel 跑的时候 SM 是满的**，但前一步 decode 白白空转的时间收不回来。
- 两次 launch 之间还有固定的 kernel launch 开销和流水线气泡。

POD 的洞察是：**decode 的「SM 闲」和 prefill 的「SM 紧」正好互补**。如果能把两类 CTA 放进同一个 grid 一次 launch 出去，让硬件调度器在 decode 块等显存的间隙把 prefill 块派上去，就能把整张卡的吞吐吃满。这就是 POD 名字里 "On-Decode" 的含义——把 prefill 「搭」在 decode 的空闲算力上。

#### 4.1.2 核心流程

混合批次的组织方式（理解 POD 的前提）：

```text
一次 forward 迭代的输入被拆成两堆：
  prefill 组：q_p  形状 [Σ qo_len_p,  num_qo_heads, head_dim]   （变长，前缀和 qo_indptr_p）
              k_p/v_p 或 paged_kv_cache_p                          （KV 可为 ragged 或 paged）
  decode  组：q_d  形状 [Σ 1,           num_qo_heads, head_dim]   （每请求 1 行）
              paged_kv_cache_d                                      （KV 存于页池）

朴素做法：两次 kernel launch
  launch1: batch_prefill(q_p, kv_p)   → o_p
  launch2: batch_decode (q_d, kv_d)   → o_d

POD 做法：一次 kernel launch，grid 里混着 prefill 块与 decode 块
  launch : POD_kernel(q_p, kv_p, q_d, kv_d, sm_aware_sched) → (o_p, o_d)
```

注意一个关键事实（后面会反复用到）：**POD 的「decode」一侧，在 kernel 内部其实是用一个 batch-prefill kernel 模板实现的**，只是把 query 的 CTA tile 设得很小（`CTA_TILE_Q_D = 16`）。这一点在 `csrc/pod.cu` 和 `csrc/batch_pod.cu` 的注释里都直接写明——「Decode setup (TensorView decode = batched prefill)」。这样做的好处是：prefill 与 decode 两侧可以共用同一套 `KernelTraits` 模板基础设施，从而能塞进同一个 kernel 实例里。

#### 4.1.3 源码精读

先看 wrapper 类的导出，确认 POD 是公开 API 的一部分：

[flashinfer/__init__.py:166-167](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__init__.py#L166-L167) 把两个 wrapper 从 `flashinfer.pod` 重新导出到顶层命名空间，所以你可以直接 `flashinfer.BatchPODWithPagedKVCacheWrapper(...)`。

再看官方文档对 POD 的一句话定位：

[docs/api/pod.rst:6-8](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/docs/api/pod.rst#L6-L8) 明确说 POD「把 single-request prefill kernel 与 batch-decode kernel 在同一次 launch 里并发执行」，并点出它的服务场景是「把 chunked prefill 与正在进行的 decode 重叠起来」。

「decode 即 batched-prefill」的注释出现在两个绑定文件里：

[csrc/pod.cu:88](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/pod.cu#L88) 注释 `// Decode setup (TensorView decode = batched prefill)`，紧接着用 `PrefillPlanInfo`（而不是 decode 专属的计划信息）来描述 decode 一侧。

[csrc/batch_pod.cu:106](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_pod.cu#L106) 同样标注 `// Decode setup (TensorView decode = batched prefill)`，批路径也用 `PrefillPlanInfo plan_info_d` 描述 decode 一侧。

这条「decode 复用 prefill 模板」的线索是理解整篇 POD 的钥匙——它解释了为什么 prefill 和 decode 能塞进同一个 kernel：**它们本来就是同一个模板的两个实例，只是 query tile 大小不同**。

#### 4.1.4 代码实践

**实践目标**：用一段文字 + 草图，把「为什么混合批次需要融合」讲清楚，确认你理解了 compute-bound 与 memory-bound 的互补关系。

**操作步骤**：

1. 阅读本节内容，然后在不看答案的情况下，画一张时间轴：
   - 上方画「朴素两次 launch」：一段 decode（标注「SM 占用低」）+ 一段 prefill（标注「SM 占用满」），中间画一个 launch 气泡。
   - 下方画「POD 一次 launch」：一个融合块，里面 decode 与 prefill 的 CTA 交错。
2. 回答：假如一张卡有 132 个 SM，decode 只能用满约 40 个 SM，prefill 需要 120 个 SM 的算力。朴素两次 launch 会出现多少「SM 空转·步」？POD 把它们合并后，理论上能把空闲的多少 SM 拿去算 prefill？

**需要观察的现象 / 预期结果**：

- 朴素做法里，decode 阶段约有 92 个 SM 空转；这些算力在 POD 里可以贡献给 prefill。
- POD 的总时长应接近 `max(decode 时长, prefill 时长) + 调度开销`，而不是两者之和。注意这是直觉层面的预期，精确数字待本地实测（见 4.3.4 的 benchmark 实践）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 POD 对 decode 收益明显，而对「全是 prefill」或「全是 decode」的批次收益不大？

> 参考答案：全是 prefill 时 SM 本来就满，没有空闲算力可借；全是 decode 时没有 prefill 工作可填，且 decode 之间彼此都是 memory-bound，互相抢带宽反而可能变慢。POD 收益来自两类工作在资源占用上的**互补**，缺一不可。

**练习 2**：POD 的「decode 一侧」在 kernel 内部实际调用了哪个 device 函数？为什么这样设计？

> 参考答案：调用的是 `BatchPrefillWithPagedKVCacheDevice`（见 4.2.3 的 [pod.cuh:167](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/pod.cuh#L167)），而不是某个 decode 专属 kernel。设计目的是让 prefill 与 decode 共用同一套 `KernelTraits` 模板，从而能编译进同一个 kernel 实例、共享 shared memory 布局与 warp 编排，只需用很小的 query tile（`CTA_TILE_Q_D=16`）来模拟 decode 的「每请求 1 行」。

### 4.2 POD 融合与 SM 感知调度

这是 POD 的技术核心：一个 grid 里既有 prefill 块又有 decode 块，怎么决定每个块干哪种活？

#### 4.2.1 概念说明

CUDA 的线程块（CTA）一旦被 launch，会被硬件的 block scheduler 派到各个 SM 上。普通 kernel 里每个 CTA 干的活都一样（由 `blockIdx` 决定读哪段数据）。POD 的特殊之处是：**同一个 kernel 里，不同 CTA 干的活不同**——有的算 prefill、有的算 decode。

FlashInfer 采用「SM 感知的软件调度器」来做这个分配。核心思路：

1. 每个 CTA 在入口处先读一条 PTX 指令 `mov.u32 %smid`，知道自己被派到了**哪一个 SM**（编号 `linear_bid` ∈ [0, num_SMs)）。
2. 维护一个全局原子计数数组（代码里叫 `tbAssign`/`sm_aware_sched`），布局是 `[num_SMs + 2]`：
   - 前 `num_SMs` 个槽：每个 SM 一个计数器，用来给该 SM 上的 CTA 轮流派发 prefill/decode「名额」。
   - 后 2 个槽：分别是 prefill、decode 的**全局 blockId 计数器**，用来领取「下一个要算的 prefill/decode 工作块编号」。
3. 根据 prefill 工作量与 decode 工作量的**比例**，给每个 SM 设计一个交替节奏（例如「每 3 块 decode 配 1 块 prefill」），让两种活在 SM 上交错填充。
4. 某种活的 blockId 领完了，就把这个 SM 上的剩余 CTA 切到另一种活，避免尾部长尾空转。

这套机制让 GPU **不需要等 decode 块跑完才开始 prefill**，而是让两者在同一拨 SM 上交错推进。

#### 4.2.2 核心流程

调度器的判定逻辑（取自 [batch_pod.cuh:77-98](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L77-L98)，单路径 [pod.cuh:80-101](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/pod.cuh#L80-L101) 同构）：

```text
prefill_slots = ceil(prefill_blocks / blk_factor_p)   # prefill 工作量
decode_slots  = ceil(decode_blocks  / blk_factor_d)   # decode  工作量

if prefill_slots <= decode_slots:        # prefill 少、decode 多
    total_tags = decode_slots / prefill_slots + 1
    # 例如 decode 是 prefill 的 4 倍 → total_tags = 5：每 5 块里 1 块 prefill、4 块 decode
    op = (atomicAdd(per_sm_counter[my_sm], 1) % total_tags)
    op = (op > 0) ? DECODE : PREFILL
else:                                    # decode 少、prefill 多
    pref_tags = prefill_slots / decode_slots
    op = (atomicAdd(per_sm_counter[my_sm], 1) % (pref_tags + 1))
    op = (op < pref_tags) ? PREFILL : DECODE

# 领一个该 op 的全局 blockId
linear_bid = atomicAdd(global_counter[op], 1)
# 若该 op 的工作已领完，切到另一个 op
if op == PREFILL && linear_bid >= prefill_slots: op = DECODE; linear_bid = ...
if op == DECODE  && linear_bid >= decode_slots : op = PREFILL; linear_bid = ...
```

可用一个简单的「交错比」来理解：当 prefill 工作量是 decode 的 \(r\) 倍时，调度器让每个 SM 上 prefill 与 decode 的出现频率比约为 \(r:1\)。形式上，若 `prefill_slots = r · decode_slots`，则 `pref_tags = r`，每 \(r+1\) 个名额里有 \(r\) 个 prefill、1 个 decode。这正是把 GPU 按两类工作的真实比例「搅拌」分配的体现。

#### 4.2.3 源码精读

先看枚举与 kernel 签名。POD 用一个 `Operation` 枚举区分两种活：

[include/flashinfer/attention/pod.cuh:32-35](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/pod.cuh#L32-L35) 定义 `enum Operation { PREFILL = 0, DECODE = 1 }`。

[include/flashinfer/attention/pod.cuh:37-46](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/pod.cuh#L37-L46) 是 kernel 签名 `PODWithKVCacheTensorKernel`。注意它的 `__launch_bounds__` 取的是「prefill 与 decode 两种 kernel trait 所需线程数的最大值」——因为同一个 kernel 必须能容纳两种活里更宽的那一种。第四个参数 `int* tbAssign` 就是调度缓冲（单路径用 `__grid_constant__` 之外的动态参数传入；批路径改名为 `sm_aware_sched`）。

接着是调度器的精髓——读 `%smid`：

[include/flashinfer/attention/batch_pod.cuh:72-73](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L72-L73) 用内联 PTX `mov.u32 %0, %nsmid`（读总 SM 数）与 `mov.u32 %0, %smid`（读当前 SM 编号）。源码里有一行重要警告 `WARNING: nsmid has only been tested on A100/H100`——这说明该机制依赖硬件的 SM-ID 寄存器语义，**可移植性有限**，这也是 POD 目前偏实验性、面向特定架构的原因之一。

然后是按比例分配 op 的逻辑：

[include/flashinfer/attention/batch_pod.cuh:77-98](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L77-L98) 实现了 4.2.2 里描述的「交错比」判定。`atomicAdd(&sm_aware_sched[linear_bid], 1)` 是「本 SM 第几次被调度」的计数，对 `total_tags`（或 `pref_tags+1`）取模决定 op。

领取 blockId 并处理尾部切换：

[include/flashinfer/attention/batch_pod.cuh:100-109](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L100-L109) 从 `sm_aware_sched[num_SMs + op]` 领取该 op 的下一个全局 blockId；若超出该 op 的工作上限就切到另一个 op。`num_SMs + 0` 是 prefill 全局计数器、`num_SMs + 1` 是 decode 全局计数器——这正是缓冲区大小必须是 `num_sm + 2` 的由来。

分配结果通过 shared memory 广播给 CTA 内所有线程：

[include/flashinfer/attention/batch_pod.cuh:110-120](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L110-L120) 把 `linear_bid` 和 `op` 写进 shared memory，`__syncthreads()` 后让整块线程取到一致的分配结果。

最后按 op 分发到真正干活的 device 函数：

[include/flashinfer/attention/batch_pod.cuh:122-139](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L122-L139) 当 `op == PREFILL` 时，把 shared memory 重解释成 prefill 的 `SharedStorage`，调用 `BatchPrefillWithPagedKVCacheDevice<KTraits_P>(...)`。

[include/flashinfer/attention/batch_pod.cuh:140-158](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L140-L158) 当 `op == DECODE` 时，重解释成 decode 的 `SharedStorage`，调用 `BatchPrefillWithPagedKVCacheDevice<KTraits_D>(...)`——**注意两边调用的都是 `BatchPrefillWithPagedKVCacheDevice`**，只是用了不同的 kernel trait（`KTraits_D` 的 query tile 更小）。这就是 4.1 里「decode 即 batched-prefill」在源码上的最终落点。

单路径的对应分发在 [pod.cuh:125-169](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/pod.cuh#L125-L169)：prefill 分支调用 `SinglePrefillWithKVCacheDevice`（单请求、非分页 KV），decode 分支同样调用 `BatchPrefillWithPagedKVCacheDevice`。

调度缓冲的大小约束在绑定层有断言：

[csrc/batch_pod.cu:331-336](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_pod.cu#L331-L336) 断言 `sm_aware_sched.size(0) == num_sm + 2`，注释解释「num_sm 个每-SM 计数器 + 2 个分别跟踪 prefill/decode 的 blockId」。

#### 4.2.4 代码实践

**实践目标**：用纸笔复现调度器对一组给定工作量的分配，确认你读懂了「交错比」与「尾部切换」。

**操作步骤**：

1. 假设 `num_SMs = 8`，`prefill_blocks = 4`，`decode_blocks = 16`，`blk_factor_* = 1`。因此 `prefill_slots = 4`，`decode_slots = 16`，落在 `prefill_slots <= decode_slots` 分支，`total_tags = 16/4 + 1 = 5`。
2. 模拟前 5 次某个 SM 被调度时的 `op` 取值：`op = counter % 5`，取值序列是 `0,1,2,3,4,0,1,...`；按 `op>0 ? DECODE : PREFILL` 规则，得到 `PREFILL, DECODE, DECODE, DECODE, DECODE, PREFILL, ...`，即 1:4 的 prefill:decode 比例。
3. 再模拟「prefill 的 4 个 blockId 被领完之后」会发生什么：根据 [batch_pod.cuh:103-105](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L103-L105)，下一个本该算 prefill 的 CTA 会切到 decode。

**需要观察的现象 / 预期结果**：

- 调度比例自动等于工作量比例（4:16 = 1:4）。
- 某种活提前做完时不会卡住，剩余 CTA 自动切到另一种活。

**预期结果**：手工推演的 op 序列与「1 个 prefill 配 4 个 decode」一致；这正是调度器把 GPU「按比例搅拌」的效果。

> 待本地验证：可在 GPU 上用 `nvprof`/`Nsight Compute` 抓 POD kernel 的 SM Occupancy 与 warp stall 原因，看 decode 块的「Long Scoreboard（等显存）」stall 是否被 prefill 块的计算掩盖。本机若无 A100/H100，`%smid` 调度路径可能不生效，仅供参考。

#### 4.2.5 小练习与答案

**练习 1**：调度缓冲为什么是 `num_sm + 2` 而不是 `num_sm`？

> 参考答案：前 `num_sm` 个槽是「每 SM 一个计数器」，用来给该 SM 轮流派发 op 名额；后 2 个槽分别是 prefill 与 decode 的**全局 blockId 计数器**，所有 SM 共享，用来领取「下一个要算的工作块」。少任何一个都会丢功能。

**练习 2**：源码里有一句 `WARNING: nsmid has only been tested on A100/H100`。这对 POD 的可移植性意味着什么？

> 参考答案：调度器依赖 `%smid`/`%nsmid` 这两条 PTX 寄存器读出「当前 SM 编号」与「总 SM 数」。这些寄存器的语义在不同架构上未必一致，因此该软件调度路径目前只在 A100/H100 验证过。在其他 GPU 上运行 POD 可能退化或行为异常，使用前需实测。

### 4.3 prefill+decode 双侧 API：两个 wrapper 的 plan/run

POD 有两个 wrapper，对应两种服务侧用法。

#### 4.3.1 概念说明

- **`PODWithPagedKVCacheWrapper`（「单 POD」）**：prefill 一侧是**单个请求**（`q_p` 是一段连续 query，`k_p`/`v_p` 是普通的非分页张量），decode 一侧是**一批请求**（`q_d` 形如 `[batch_size, num_qo_heads, head_dim]`，KV 存于页池）。它适合「一次只 prefill 一个新请求，同时 decode 一批老请求」的迭代。它的 `plan` **只规划 decode 一侧**（因为单请求 prefill 不需要 split-kv 规划），`run` 同时吃两侧输入。
- **`BatchPODWithPagedKVCacheWrapper`（「批 POD」）**：prefill 与 decode 两侧都是**一批请求**，KV 两侧都存于页池。它的 `plan` 要分别规划 prefill 侧与 decode 侧，`run` 把两侧融合到一次 launch。这是更通用的形态，benchmark 里也用它做对比。

两个 wrapper 都遵循 u3 系列建立的 plan/run 两段式约定：`plan` 做仅依赖批次结构的调度决策（split-kv 划分、kernel 选择），结果（`PrefillPlanInfo`）缓存起来供同一 forward 的多层 Transformer 复用；`run` 只携带每层数据并真正启动融合 kernel。

一个值得注意的设计细节：批 POD 的 `plan` 会先规划 decode 侧、再规划 prefill 侧，并从 decode 的计划里取出一个 `num_colocated_ctas`（「同驻 CTA 数」）参数传给 prefill 侧的规划——但在 prefill 工作量较大时会把它清零，以避免 prefill 与 decode 在显存带宽上互相争抢。

#### 4.3.2 核心流程

以更通用的 **批 POD** 为例，端到端流程：

```text
__init__(float_workspace_buffer, kv_layout):
    - 把 float workspace 对半切成 _p / _d 两份
    - 各自分配 int workspace(8MB) + pin_memory int workspace(8MB)
    - 查询设备属性，分配 _sm_aware_sched（大小 = multi_processor_count + 2）

plan(qo_indptr_p, kv_indptr_p, kv_indices_p, last_page_len_p,   # prefill 侧页表
     qo_indptr_d, kv_indptr_d, kv_indices_d, last_page_len_d,   # decode  侧页表
     num_qo_heads, num_kv_heads, head_dim, page_size, ...):
    - 复用 get_batch_prefill_module("fa2", ...) 拿到 JIT 模块（两侧共用同一份 fa2 prefill 模块）
    - 用同一模块对 decode 侧调一次 plan(...)  → _plan_info_d
    - 从 _plan_info_d[0] 取 num_colocated_ctas
    - 若 prefill 总行数 > 1536：num_colocated_ctas = 0   # 避免带宽争抢
    - 用同一模块对 prefill 侧调一次 plan(..., num_colocated_ctas) → _plan_info_p

run(q_p, paged_kv_cache_p, q_d, paged_kv_cache_d, causal_p=False, ...):
    - 校验 dtype、解包两侧 paged KV、补全 sm_scale/rope 等默认值
    - get_batch_pod_module(...) 按 dtype/head_dim/posenc 等编译期参数取（或 JIT 编译）POD 模块
    - module.run_tensor( prefill 侧参数..., decode 侧参数..., sm_aware_sched )
      → 融合 kernel 一次 launch 同时产出 (out_p, out_d)
    - 可选 return_lse：返回 ((out_p, lse_p), (out_d, lse_d))
```

#### 4.3.3 源码精读

先看 Python 端的两个 `@functools.cache` 模块加载器（接续 u2-l5 的两级缓存机制）：

[flashinfer/pod.py:49-58](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L49-L58) 定义 `get_pod_module` 与 `get_batch_pod_module`，分别 `gen_pod_module(...).build_and_load()` 与 `gen_batch_pod_module(...).build_and_load()`，并用 `SimpleNamespace` 暴露出 `run_tensor` 符号。键是编译期参数元组（dtype、head_dim、posenc、滑窗、logits_cap、fp16_qk_reduction、indptr dtype）。

批 POD 的 `__init__` 里双侧 workspace 与调度缓冲的分配：

[flashinfer/pod.py:861-890](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L861-L890) 把 `float_workspace_buffer` 用 `torch.chunk(..., 2)` 对半切成 prefill/decode 两份；各自分配 8MB 的 int workspace 与 8MB 的 pin_memory int workspace；并用 `torch.cuda.get_device_properties(...).multi_processor_count + 2` 分配 `_sm_aware_sched`——注意这里的 `+ 2` 与 [csrc/batch_pod.cu:331](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_pod.cu#L331) 的断言完全对应。

批 POD 的 `plan` 两侧规划与「同驻 CTA」启发式：

[flashinfer/pod.py:1066-1112](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L1066-L1112) 先对 decode 侧调一次 `self._cached_module.plan(...)` 得到 `_plan_info_d`；接着取 `num_colocated_ctas = self._plan_info_d[0]`，并在 `total_num_rows_p > 1536` 时把它置 0（注释 `Splitting small prefill causes unecessary bandwidth contention`），最后用这个值对 prefill 侧再调一次 `plan(...)` 得到 `_plan_info_p`。两侧复用同一个 `get_batch_prefill_module("fa2", ...)` 编译出来的模块。

批 POD 的 `run` 把两侧参数一次性喂给融合 kernel：

[flashinfer/pod.py:1124-1196](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L1124-L1196) 是 `run` 的签名与文档，强调「single-shot fused attention，runs batched paged prefill 与 batched paged decode in the same kernel launch」，且所有形状/策略参数都取自 `plan` 缓存的值。

[flashinfer/pod.py:1281-1351](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L1281-L1351) 是真正的调用点：`get_batch_pod_module(...)` 取得模块后，`module_getter.run_tensor(...)` 把「prefill 侧参数（含 `_plan_info_p`、`_qo_indptr_buf_p`、`_kv_indices_buf_p` 等）+ decode 侧参数（含 `_plan_info_d` 等）+ `_sm_aware_sched`」整批传下去，并在末尾按 `return_lse` 决定返回 `(out_p, out_d)` 还是带 LSE 的嵌套元组。

单 POD 的 `run` 签名（注意 prefill 一侧是非分页的 `k_p`/`v_p`）：

[flashinfer/pod.py:451-488](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L451-L488) 文档明确：`q_p` 是 `[qo_len, num_qo_heads, head_dim]`，`k_p`/`v_p` 是普通 prefill KV（布局由 `kv_layout_p` 决定），`q_d` 是 `[batch_size, num_qo_heads, head_dim]`，`paged_kv_cache_d` 才是分页 decode 缓存。它还诚实地标注了一些「currently ignored」（如 decode 侧的 `kv_layout_d`/`sm_scale_d` 等会被 `plan` 缓存值覆盖）和「LSE 已分配但暂不返回」的已知限制——读 wrapper 时留意这些注释，能帮你避开坑。

JIT 代码生成侧，URI 把 prefill/decode 两套编译期开关都编码进名字（接续 u2-l3 的 `gen_*_module` 五步模式）：

[flashinfer/jit/attention/modules.py:343-370](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L343-L370) 的 `get_pod_uri` 同时编码 `posenc_p`/`use_swa_p`/`use_logits_cap_p` 与 `posenc_d`/`use_swa_d`/`use_logits_cap_d`，即两侧的位置编码/滑窗/soft-cap 开关各算一份。批路径的 URI 只是多加前缀 `"batch_"`：

[flashinfer/jit/attention/modules.py:664](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L664) `uri = "batch_" + get_pod_uri(...)`。

最后看测试如何保证 POD 与「分别调用」结果一致：

[tests/utils/test_pod_kernels.py:121-127](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_pod_kernels.py#L121-L127) 用 `single_prefill_with_kv_cache` 算 prefill 参考输出。

[tests/utils/test_pod_kernels.py:172-187](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_pod_kernels.py#L172-L187) 用 `BatchDecodeWithPagedKVCacheWrapper` 算 decode 参考输出。

[tests/utils/test_pod_kernels.py:207-223](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_pod_kernels.py#L207-L223) 调单 POD wrapper 的 `run(q_p, k_p, v_p, q_d, kv_data, ...)`，再用 `torch.testing.assert_close` 分别比对 `o_p` 与 prefill 参考、`o_d` 与 decode 参考（`rtol=1e-3, atol=1e-3`）。这正是「融合不改结果」的回归保障。

#### 4.3.4 代码实践

**实践目标**：跑通一个最小的「混合批次」批 POD 调用，验证它的输出与「分别跑 prefill + decode」一致，并体会它相对两次 launch 的优势。

**操作步骤**（示例代码，基于 `flashinfer/pod.py` 类 docstring 与 `benchmarks/bench_mixed_attention.py` 改写）：

```python
import torch
import flashinfer

num_qo_heads, num_kv_heads, head_dim = 32, 8, 128
page_size = 1            # 批 POD benchmark 固定用 page_size=1
device = "cuda:0"

# —— 构造一个混合批次：2 个 prefill 请求 + 8 个 decode 请求 ——
p_qo_lens = [1024, 1024]                 # prefill 侧 query 长度
d_qo_lens = [1] * 8                      # decode  侧每请求 1 个 query
p_kv_lens = [1024, 1024]
d_kv_lens = [2048] * 8

def make_indptr(lens):
    return torch.cat([torch.tensor([0]), torch.cumsum(torch.tensor(lens), 0)]).int()

p_q_indptr  = make_indptr(p_qo_lens).to(device)
p_kv_indptr = make_indptr(p_kv_lens).to(device)
d_q_indptr  = make_indptr(d_qo_lens).to(device)
d_kv_indptr = make_indptr(d_kv_lens).to(device)

kv_indices_p = torch.arange(p_kv_indptr[-1].item(), device=device, dtype=torch.int32)
kv_indices_d = torch.arange(d_kv_indptr[-1].item(), device=device, dtype=torch.int32)
last_page_len_p = torch.full((len(p_qo_lens),), page_size, device=device, dtype=torch.int32)
last_page_len_d = torch.full((len(d_qo_lens),), page_size, device=device, dtype=torch.int32)

q_p = torch.randn(p_q_indptr[-1].item(), num_qo_heads, head_dim, device=device, dtype=torch.bfloat16)
kv_p = torch.randn(p_kv_indptr[-1].item(), 2, page_size, num_kv_heads, head_dim, device=device, dtype=torch.bfloat16)
q_d = torch.randn(d_q_indptr[-1].item(), num_qo_heads, head_dim, device=device, dtype=torch.bfloat16)
kv_d = torch.randn(d_kv_indptr[-1].item(), 2, page_size, num_kv_heads, head_dim, device=device, dtype=torch.bfloat16)

workspace = torch.empty(128 * 1024 * 1024, dtype=torch.uint8, device=device)
wrapper = flashinfer.BatchPODWithPagedKVCacheWrapper(workspace, kv_layout="NHD")
wrapper.plan(
    # prefill 侧
    p_q_indptr, p_kv_indptr, kv_indices_p, last_page_len_p,
    # decode  侧
    d_q_indptr, d_kv_indptr, kv_indices_d, last_page_len_d,
    num_qo_heads=num_qo_heads, num_kv_heads=num_kv_heads,
    head_dim=head_dim, page_size=page_size,
    q_data_type=torch.bfloat16, kv_data_type=torch.bfloat16,
)

o_p, o_d = wrapper.run(q_p, kv_p, q_d, kv_d, causal_p=True)
print(o_p.shape, o_d.shape)   # 预期: ([2048,32,128], [8,32,128])
```

**需要观察的现象 / 预期结果**：

1. 首次 `wrapper.run(...)` 会触发 JIT 编译（ninja + nvcc），第二次调用显著变快——这承接 u2-l5 的两级缓存行为。
2. `o_p` 形状是 `[Σ p_qo_lens, num_qo_heads, head_dim]`，`o_d` 形状是 `[Σ d_qo_lens, num_qo_heads, head_dim]`，与 docstring（[pod.py:825-826](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L825-L826)）一致。
3. 想验证正确性，可参照 [tests/utils/test_pod_kernels.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_pod_kernels.py)：分别用 `BatchPrefillWithPagedKVCacheWrapper` 与 `BatchDecodeWithPagedKVCacheWrapper` 各跑一遍，再与 `o_p`/`o_d` 做 `torch.testing.assert_close(rtol=4e-3, atol=4e-3)`。

**说明 POD 相对「分别调用」的优势**：

- **一次 launch 取代两次 launch**：省掉一次 kernel launch 开销与两次 launch 之间的流水线气泡。
- **SM 占用更满**：decode 块等显存的间隙，被同一 grid 里的 prefill 块的计算填补（见 4.2 的 SM 感知调度）。理论上混合批次的 POD 总时长更接近 `max(prefill, decode)` 而非 `prefill + decode`。

> 待本地验证：精确的加速比取决于硬件（POD 的 `%smid` 调度目前只在 A100/H100 验证过）、批次里 prefill/decode 的比例与各自的序列长度。`benchmarks/bench_mixed_attention.py` 的 `run_bench` 已经把 `BatchPOD` 与 `BatchPrefill`、`BatchAttention` 放在同一个混合批次上用 `bench_gpu_time` 比较中位数耗时，可据此在你的卡上得到真实数字。

#### 4.3.5 小练习与答案

**练习 1**：批 POD 的 `plan` 为什么要先规划 decode 侧、再规划 prefill 侧，并且把一个 `num_colocated_ctas` 从前者传给后者？

> 参考答案：decode 侧的 split-kv 规划会估算出一个「同驻 CTA 数」(`num_colocated_ctas`)，反映 decode 工作能容忍多少 CTA 同驻而不互相抢带宽。把这个数传给 prefill 侧的规划，可以让 prefill 的 split 决策与 decode 协调，便于两者在同一 grid 里共驻。但当 prefill 工作量较大（`total_num_rows_p > 1536`）时，继续同驻会引发带宽争抢，所以代码会把它清零。见 [pod.py:1088-1091](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L1088-L1091)。

**练习 2**：单 POD wrapper 的 `run` 里，prefill 一侧的 `k_p`/`v_p` 与 decode 一侧的 `paged_kv_cache_d` 在存储方式上有什么本质区别？为什么单 POD 适合「一次只 prefill 一个请求」？

> 参考答案：prefill 一侧的 `k_p`/`v_p` 是**普通、非分页**的张量（形状按 `kv_layout_p` 解释，如 NHD 下 `[kv_len, num_kv_heads, head_dim]`），对应单请求 prefill 走 `SinglePrefillWithKVCacheDevice`；decode 一侧的 KV 存于**页池**，靠页表三件套寻址。单 POD 适合「一次一个 prefill」是因为它的 prefill 路径是 single-prefill kernel（非 batched），更适合恰好只有一个新请求进来的迭代；若同时有多个新请求，应改用批 POD。

**练习 3**：读 [flashinfer/pod.py:1281-1296](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/pod.py#L1281-L1296)，`get_batch_pod_module` 的缓存键由哪些编译期参数组成？如果只改 decode 的 `qo_len`（运行期形状）会触发重新编译吗？

> 参考答案：缓存键含 `q_p.dtype`、`k_cache_p.dtype`、`q_p.dtype`(输出)、`q_p.shape[-1]`(head_dim)、prefill 与 decode 各自的 `pos_encoding_mode`、`use_sliding_window`、`use_logits_soft_cap`、`use_fp16_qk_reduction`、以及 `indptr_type`。`qo_len` 是运行期参数，不进缓存键，所以只改它**不会**触发重新编译——这与 u2-l5 讲的「编译期 vs 运行期参数」一致。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个小任务：

**任务**：实现一个最小化的「混合批次推理迭代」对照实验。

1. 选定 `num_qo_heads=32, num_kv_heads=8, head_dim=128, page_size=1`。
2. 构造三组混合批次（保持总 token 数大致相近，但 prefill:decode 比例不同）：
   - A：4 个 prefill(512) + 4 个 decode(2048)
   - B：1 个 prefill(2048) + 16 个 decode(2048)
   - C：8 个 prefill(512) + 0 个 decode（纯 prefill 对照组）
3. 对每组，分别用两种方式跑：
   - **朴素**：`BatchPrefillWithPagedKVCacheWrapper` + `BatchDecodeWithPagedKVCacheWrapper`，两次 launch；
   - **POD**：`BatchPODWithPagedKVCacheWrapper`，一次 launch。
4. 用 `flashinfer.testing.bench_gpu_time`（见 u10-l3）测量两种方式的中位数耗时；同时用 `assert_close` 验证 POD 输出与朴素方式一致。
5. 回答：
   - 哪一组 POD 相对朴素的加速最明显？为什么？
   - 对照组 C（纯 prefill）POD 是否还有优势？这是否印证了 4.1.5 练习 1 的结论？

**预期收获**：你会直观看到 POD 的收益与「prefill/decode 比例」强相关——两者都存在且 decode 占用 SM 较少时收益最大，纯 prefill 时收益消失。同时你会熟练掌握批 POD 的 plan/run 用法，并能用测试里的对照法验证融合的正确性。

> 注意：若你的 GPU 不是 A100/H100，`%smid` 软件调度路径可能不生效，加速比可能不及预期甚至无加速——这本身也是一个有价值的观察，对应 [batch_pod.cuh:70-71](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/batch_pod.cuh#L70-L71) 的可移植性警告。

## 6. 本讲小结

- POD（Prefill-On-Decode）针对**持续批处理迭代中 prefill 与 decode 并存**的场景：prefill 是 compute-bound、decode 是 memory-bound，朴素两次 launch 会让 decode 期间的空闲 SM 白白浪费。
- 核心机制是**一次 kernel launch 把 prefill 块与 decode 块塞进同一个 grid**，再用一个 **SM 感知的软件线程块调度器**（靠 PTX `%smid`/`%nsmid` + 一个 `num_sm+2` 大小的原子计数数组）按工作量比例把每个 CTA 动态分配给 prefill 或 decode。
- 关键工程巧思：**decode 一侧在 kernel 内部实际复用 batched-prefill 模板**（`BatchPrefillWithPagedKVCacheDevice`，`CTA_TILE_Q_D=16`），使两侧共用同一套 `KernelTraits`，从而能编译进同一个 kernel 实例。
- 两个 wrapper：`PODWithPagedKVCacheWrapper`（单 prefill + 批 decode，prefill 为非分页）与 `BatchPODWithPagedKVCacheWrapper`（批 prefill + 批 decode，两侧都分页）；二者都遵循 plan/run 两段式，`plan` 复用 `get_batch_prefill_module("fa2", ...)` 编出来的模块分别规划两侧。
- 批 POD 的 `plan` 用 `num_colocated_ctas` 启发式协调两侧同驻，并在 prefill 工作量大时清零以避免带宽争抢；JIT 的 URI 同时编码 prefill/decode 两套编译期开关。
- POD 的正确性由 `tests/utils/test_pod_kernels.py` 保证——把融合输出与「分别用 single_prefill + BatchDecode」的参考逐元素比对；可移植性目前受限于 `%smid` 只在 A100/H100 验证过。

## 7. 下一步学习建议

- **对比 BatchAttention**：u4-l5 会讲 `BatchAttention` 这个「统一混合批处理 wrapper」，它按 `qo_indptr` 区间对每个请求分别派发 paged-prefill 或 paged-decode，但**仍是分别调度**。建议读完 u4-l5 后回到 `benchmarks/bench_mixed_attention.py`，把 BatchPOD 与 BatchAttention 放在同一批次上对比，理解「融合到单 kernel」（POD）与「统一入口但分别调度」（BatchAttention）的取舍。
- **深入调度器与 split-kv**：本讲的 SM 感知调度建立在 u3-l4 讲过的 `PrefillPlanInfo`、split-kv 划分之上。若想理解 `_plan_info_d[0]`（`num_colocated_ctas`）是如何由 plan 算出来的，可继续阅读 `include/flashinfer/attention/scheduler.cuh` 中的 `PrefillSplitQOKVIndptr`。
- **MLA 与 cascade 的合并视角**：POD 的「同一 grid 多种活」思想，与 u4-l1（MLA 的 split-k + LSE 合并）、u4-l2（cascade 的 logsumexp 合并）在「先用部分结果再合并」这一点上相通，可对照阅读 `include/flashinfer/attention/cascade.cuh` 体会 FlashInfer 统一的「split → merge」设计哲学。
- **可移植性与实验性算子**：POD 的 `%smid` 调度属于较实验性的优化。如果你关注这类「依赖特定硬件特性」的新算子，u7-l6 会介绍 GDN/KDA 等更新的实验性子系统，可作为下一站。
