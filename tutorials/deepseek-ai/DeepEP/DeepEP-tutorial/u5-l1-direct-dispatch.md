# 直接模式 Dispatch：notify 与 dispatch warps 的协作

## 1. 本讲目标

本讲深入 DeepEP V2 在**单节点（`num_scaleout_ranks == 1`）**场景下的 dispatch 内核 `dispatch_impl`，把它从「一个被 JIT 实例化的黑盒」拆成两组分工明确的 warp。读完本讲你应当能够：

- 说清直接模式 dispatch 内核中 **notify warps** 与 **dispatch warps** 各自的职责，以及它们如何用共享内存和 named barrier 协作。
- 推导 `num_dispatch_warps` 的计算公式，解释它为何同时被**共享内存容量**与**每 CTA 最多 32 个 warp** 两个上界约束。
- 说出 `dst_buffer_slot_idx`、`psum_num_recv_tokens_per_scaleup_rank`、`psum_num_recv_tokens_per_expert`、`num_unaligned_recv_tokens_per_expert` 这几个输出张量分别由**哪组 warp、在内核的哪个阶段**写入。
- 理解 dispatch warp 如何用 TMA 把 token 从本地共享内存搬到对端 rank 的对称缓冲区，以及 NVLink 可达与不可达两条路径的差异。

本讲只覆盖**直接模式**（`num_scaleout_ranks == 1`，纯 NVLink 节点内通信）。多节点的 hybrid 两级通信留待下一讲 u5-l2。

## 2. 前置知识

在进入内核之前，请确认你已经掌握下面这些前置概念（均来自依赖讲义 u3-l2 与 u4-l2）：

- **warp 与 CTA**：GPU 上 32 个线程组成一个 warp，一个线程块（CTA/block）可包含多个 warp。本讲里「CTA = 一个 SM 上跑的一份内核实例」，`__launch_bounds__(kNumThreads, 1)` 表示每个 SM 只放一个 block。
- **共享内存（shared memory, smem）**：每个 SM 私有的高速片上内存，本内核把它当「每个 dispatch warp 专属的 TMA 暂存区」+「notify warps 的计数小黑板」。
- **TMA（Tensor Memory Access）**：Hopper 引入的异步批量拷贝引擎，用一条指令搬运一大块连续字节，搬运完成由 mbarrier 通知。本内核里 dispatch warp 用 TMA 把 token 从全局内存搬进共享内存（load），再从共享内存搬到对端 rank 的全局内存（store）。
- **mbarrier**：共享内存里的异步屏障，配合 TMA 使用：`expect_tx` 声明期望收到的字节数，`try_wait` 等到这些字节到齐。
- **对称内存（symmetric memory）**：见 u3-l4。每个 rank 在 NCCL 窗口里有一块按相同偏移排布的缓冲区；本地指针减去本 rank 基址、加到对端 rank 基址，就得到对端等价位置——这是 NVLink 直接写入对端的关键。
- **JIT 模板实例化**：见 u4-l2。`launch_dispatch` 把 SM 数、rank 数、hidden 字节数等运行时整数填进 `dispatch_impl<...>` 的模板尖括号，让它们变成编译期常量。

如果你对 EP（专家并行）整体流程还陌生，请先读 u1-l1；如果你还不清楚 `ElasticBuffer.dispatch` 的 Python 接口与返回的 `EPHandle`，请先读 u2-l3。

## 3. 本讲源码地图

本讲聚焦两个核心文件，并引用四个辅助头文件：

| 文件 | 作用 |
| --- | --- |
| `csrc/kernels/elastic/dispatch.hpp` | **host 侧启动器**。`launch_dispatch` 决定 warp 数、shared memory、cluster 配置，再 `generate → build → launch`。 |
| `deep_ep/include/deep_ep/impls/dispatch.cuh` | **device 侧真内核** `dispatch_impl`。notify warps 与 dispatch warps 的全部逻辑都在这里。 |
| `deep_ep/include/deep_ep/common/layout.cuh` | `TokenLayout`（单个 token 的四段打包）与 `BufferLayout`（token 在 rank×tokens 两维展开）。 |
| `deep_ep/include/deep_ep/common/handle.cuh` | `NCCLGin::get_sym_ptr`——把本地对称指针翻译成对端 NVLink 可达指针。 |
| `deep_ep/include/deep_ep/common/ptx.cuh` | TMA / mbarrier / cp.async / named_barrier / deduplicate 等 PTX 原语封装。 |
| `csrc/elastic/buffer.hpp` | host 侧 `ElasticBuffer::dispatch`，准备张量、调用 `launch_dispatch`。 |

回顾一条调用链（来自 u1-l2）：`buffer.dispatch()`（Python）→ `ElasticBuffer::dispatch`（`buffer.hpp`）→ `launch_dispatch`（`dispatch.hpp`）→ JIT 编译并启动 `dispatch_impl`（`dispatch.cuh`）。本讲的主角是这条链的最后两环。

## 4. 核心概念与源码讲解

### 4.1 直接模式 dispatch 的整体角色与 warp 划分

#### 4.1.1 概念说明

dispatch 的任务是：本 rank 持有 `num_tokens` 个 token，每个 token 经门控后选出 `num_topk` 个目标专家；这些专家分布在所有 rank 上。dispatch 要把每个 token 的隐状态、scaling factor、路由元数据（top-k 索引、权重、来源 rank/token 索引）送到目标专家所在的 rank，落进那块所有 rank 共享的对称缓冲区里，供后续专家计算使用。

直接模式（`num_scaleout_ranks == 1`）特指**没有跨节点 RDMA 流量**的情形——所有目标 rank 都在本节点内，通过 NVLink 对称内存直达。于是内核里只走 NVLink 这一条物理路径，逻辑最简洁，是理解 dispatch 的最佳入口。

内核把一个 CTA（=一个 SM）里的 warp 分成两组：

- **notify warps**（前 `kNumNotifyWarps` 个 warp）：负责「先告诉每个 rank：你即将收到多少 token、每个专家收到多少」。它做的是**计数与全局归约**，产出供 combine 和 epilogue 使用的统计张量。
- **dispatch warps**（剩下的 warp）：负责「真正搬数据」。每个 dispatch warp 认领一组 token，把它们的隐状态经 TMA 写进对端 rank 的对称缓冲区。

为什么要先 notify 再 dispatch？因为对端 rank 必须先知道「我会收到几份、落在哪几个专家」，才能在 combine 阶段做反向路由、在 epilogue 阶段做 expand 布局与零填充。notify 用极少的 warp（默认 4 个）把这件事提前做完，dispatch warp 才能专心搬数据。

#### 4.1.2 核心流程

```text
进入 dispatch_impl(每个 SM 一个 CTA, 共 kNumSMs 个 CTA)
 │
 ├─ gpu_barrier(Tag0, 无 store flush, 无 prologue grid sync)   # 启动栅栏, 确保 workspace 就绪
 │
 ├─ if warp_idx < kNumNotifyWarps:   → notify 分支
 │     ├─ 清零共享内存里的 rank_count/expert_count
 │     ├─ 逐 token: atomicAdd 统计本 SM 内每个 expert/rank 收到几个
 │     ├─ 全 grid 归约到 workspace, 等所有 SM 到齐
 │     ├─ 把本 rank 的计数经 NVLink/RDMA 发给所有 peer rank
 │     ├─ 等待并收回所有 peer rank 发来的计数
 │     ├─ 汇总: 写 num_unaligned / 对齐 / cumulative stats
 │     └─ 前缀和: 写 psum_num_recv_tokens_per_scaleup_rank / per_expert
 │
 │   else:                            → dispatch 分支
 │     ├─ 在共享内存里开辟本 warp 专属的 TMA 暂存区
 │     ├─ for 每个认领的 token (步长 = num_dispatch_warps * num_sms):
 │     │     ├─ TMA load 隐状态 + cp.async SF 到共享内存
 │     │     ├─ 载入 top-k 索引/权重, 写来源元数据
 │     │     ├─ 槽位分配: atomicAdd 拿一个对端 slot (或 cached 复用)
 │     │     ├─ mbarrier 等 TMA load 到齐
 │     │     ├─ TMA store 到对端 rank 的对称缓冲区 (NVLink 直达)
 │     │     └─ 非可达 rank: 先 TMA store 到 send_buffer, 再 gin.put (RDMA)
 │     └─ gpu_barrier(Tag1, flush stores)   # 确保所有 token 都抵达对端
 │
 └─ 触发 epilogue + 清理 atomic 计数器
```

两组 warp 在同一个内核里**并行**跑（同一时刻 notify 在计数、dispatch 在搬数据），只在少数几个 `gpu_barrier` 与 `named_barrier` 处同步。

#### 4.1.3 源码精读

内核的模板签名与 launch_bounds，注意 `kNumNotifyWarps` 与 `kNumDispatchWarps` 都是编译期常量：

[deep_ep/include/deep_ep/impls/dispatch.cuh:17-31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17-L31) —— `dispatch_impl` 的模板参数列表与 `__launch_bounds__(kNumThreads, 1)`。`kNumThreads = kNumNotifyThreads + kNumDispatchThreads`，`( ,1)` 表示每 SM 只驻留一个 block，把整个 SM 的共享内存让给这一个 block。

warp 角色分派就一行 `if`：

[deep_ep/include/deep_ep/impls/dispatch.cuh:79](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L79) —— `if (warp_idx < kNumNotifyWarps)` 走 notify 分支，`else` 走 dispatch 分支。`warp_idx` 由 `ptx::get_warp_idx()` 取得。

注意每个 dispatch warp 还会被当成一个「channel」来分配 QP（队列对）：

[deep_ep/include/deep_ep/impls/dispatch.cuh:68-71](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L68-L71) —— 注释「We treat each warp as a channel」；`comm::get_qp_mode` 的第三个模板参数填的是 `kNumDispatchWarps`（扮演「每 SM 的 channel 数」），据此把 RDMA QP 分给各 warp。notify warp 因为 `kNumNotifyWarps > 0` 恒占用 QP 0。

#### 4.1.4 代码实践

**目标**：用一次「源码阅读 + 断点式标注」建立两组 warp 的全景图。

**步骤**：

1. 打开 `deep_ep/include/deep_ep/impls/dispatch.cuh`，定位到第 79 行的 `if`。
2. 在第 79 行旁标注 `// ===== notify warps 开始 =====`，在第 259 行的 `else` 旁标注 `// ===== dispatch warps 开始 =====`。
3. 浏览 notify 分支（79–258 行），数一下其中出现了几次 `ptx::named_barrier<kNumNotifyThreads>(...)`——这些是 notify warps 之间的局部同步点。
4. 浏览 dispatch 分支（259–395 行），找到唯一的 `for (int token_idx = ...)` 循环（第 280 行）。

**观察现象**：你会发现 notify 分支的同步密集（多次 named_barrier，因为多 warp 协作做归约），而 dispatch 分支几乎只有 `__syncwarp()`（单 warp 内同步），每个 dispatch warp 独立处理自己认领的 token，彼此几乎不通信。

**预期结果**：能在内核里画出「前 4 个 warp 跑 notify、其余 warp 跑 dispatch」的清晰分界，并理解这种分工让两组工作互不阻塞。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `__launch_bounds__` 的第二个参数设成 `1`（每 SM 只放一个 block）？如果设成 `2` 会怎样？

**答案**：因为每个 block 要独占整个 SM 的动态共享内存来容纳所有 dispatch warp 的 TMA 暂存区（见 4.2）。若每 SM 放 2 个 block，共享内存要一分为二，能容纳的 dispatch warp 数减半，吞吐下降；同时 dispatch 内核本就追求「占用尽量少的 SM」，并不需要靠提升 block 驻留数来掩盖延迟。

**练习 2**：notify warps 的数量 `kNumNotifyWarps = 4` 是随意选的吗？

**答案**：不是。`EP_STATIC_ASSERT(kNumNotifyWarps % 4 == 0)`（dispatch.cuh:48）要求它是 4 的倍数——因为前缀和阶段把 `kNumRanks` 维度的归约交给 warp 0、把 `kNumExpertsPerRank` 维度交给 warp 1（dispatch.cuh:251-256），且 host 侧 `EP_HOST_ASSERT(num_notify_warps % 4 == 0)`（dispatch.hpp:178）也校验过。选 4 而非更大，是为了把更多 warp 留给 dispatch。

### 4.2 共享内存占用计算与 `num_dispatch_warps` 推导

#### 4.2.1 概念说明

直接模式 dispatch 内核的共享内存同时承担两份职责，它们被前后拼接在同一块 `smem[]` 里：

1. **notify 计数区**（在前）：一块 `(num_ranks + num_experts)` 个 `int` 的数组，notify warps 用它做 block 内的 atomicAdd 统计。
2. **dispatch TMA 暂存区**（在后）：**每个 dispatch warp 拥有一份独立的、按 token 打包的暂存区**。dispatch warp 先把一个 token 的隐状态/SF/元数据 TMA load 进自己的暂存区，再从暂存区 TMA store 到对端。之所以「每 warp 一份」，是为了让多个 warp 能并行处理不同 token 而不争抢同一块暂存区。

于是 `num_dispatch_warps` 不是任意取的，而是由「共享内存还剩多少 / 每个 warp 暂存区多大」算出来的；同时它还要满足「整个 CTA 的 warp 数不超过 32」。这正是本讲的核心实践任务。

#### 4.2.2 核心流程

共享内存总容量 `num_smem_bytes`（由 `jit::device_runtime->get_num_smem_bytes()` 给出，Hopper SM90 在 `cudaFuncSetAttribute` 开启后可达约 228 KB，具体值**待本地验证**）被切成：

\[ \text{num\_smem\_bytes} = \text{notify\_smem} + \text{num\_dispatch\_warps} \times \text{per\_warp\_tma\_bytes} \]

其中：

\[ \text{notify\_smem} = \mathrm{align}(\text{num\_ranks} + \text{num\_experts},\ \text{kNumNotifyWarps} \times 32) \times \mathtt{sizeof(int)} \]

每个 dispatch warp 的 TMA 暂存区大小（`TokenLayout::get_num_bytes<true>`，`true` 表示含 mbarrier）：

\[ \text{per\_warp\_tma\_bytes} = \mathrm{align}(h_{\text{bytes}}, 32) + \mathrm{align}(\text{sf\_bytes}, 32) + \mathrm{align}(\text{meta\_bytes}, 32) + \mathrm{align}(\text{mbarrier}, 32) \]

而元数据段 \(\text{meta\_bytes} = \text{num\_topk}\cdot(\mathtt{sizeof(int)}+\mathtt{sizeof(float)}) + (1+\text{num\_topk})\cdot\mathtt{sizeof(int)}\)（前半是 top-k 索引+权重，后半是来源 rank/token 索引等）。

最终：

\[ \text{num\_dispatch\_warps} = \min\!\left(\left\lfloor\frac{\text{num\_smem\_bytes} - \text{notify\_smem}}{\text{per\_warp\_tma\_bytes}}\right\rfloor,\ \ 32 - \text{kNumNotifyWarps}\right) \]

第一个上界来自**共享内存**，第二个上界来自**每 CTA 最多 1024 线程（32 warp）**。

#### 4.2.3 源码精读

notify 共享内存大小由 host 辅助函数算出，注意它按 `kNumNotifyWarps * 32 = 128` 对齐：

[csrc/kernels/elastic/dispatch.hpp:130-134](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L130-L134) —— `kNumNotifyWarps = 4` 与 `get_num_notify_smem_bytes`。按 128 对齐是为了让 notify warps（共 128 线程）能用 `for (i = thread_idx; i < ...; i += kNumNotifyThreads)` 均匀地清零/读写。

核心公式本身——`num_dispatch_warps` 取两个上界的较小值：

[csrc/kernels/elastic/dispatch.hpp:186-190](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L186-L190) —— 直接模式分支：`(num_smem_bytes - num_notify_smem_bytes) / token_layout.get_num_bytes<true>()` 是共享内存上界，`32 - num_notify_warps` 是 warp 数上界；二者取 `min`。`num_threads = (num_notify_warps + num_dispatch_warps) * 32`。

每个 dispatch warp 的暂存区大小定义在 `TokenLayout::get_num_bytes`：

[deep_ep/include/deep_ep/common/layout.cuh:201-208](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L201-L208) —— 四段（hidden / sf / metadata / 可选 mbarrier）各自按 `kNumTMAAlignBytes = 32` 对齐后求和。`kWithMBarrier=true` 时多算一个对齐到 32 的 mbarrier（`sizeof(ptx::mbarrier) == 8`）。

元数据段长度的定义：

[deep_ep/include/deep_ep/common/layout.cuh:194-195](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L194-L195) —— `num_metadata_bytes = num_topk * (sizeof(int) + sizeof(float)) + (with_metadata ? (1 + num_topk) * sizeof(int) : 0)`。

device 侧用 `BufferLayout<true>` 把这块共享内存按「`kNumDispatchWarps` 份、每份 1 个 token」切给各 dispatch warp：

[deep_ep/include/deep_ep/impls/dispatch.cuh:262-265](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L262-L265) —— `tma_buffer = BufferLayout<true>(...).get_rank_buffer(dispatch_warp_idx).get_token_buffer(0)`：第 `dispatch_warp_idx` 个 dispatch warp 拿到共享内存里属于自己的那一份暂存区。起点是 `smem + kNumSmemBytesForNotify`，正好接在 notify 计数区之后。

#### 4.2.4 代码实践

**目标**：手算一个真实配置下 `num_dispatch_warps` 的值，验证它确实同时被共享内存与 warp 数两个上界约束。

**步骤**：

1. 选定配置：BF16、`hidden = 7168`、`num_topk = 6`、无 SF（`num_sf_packs = 0`）、`num_ranks = 8`、`num_experts = 64`（每 rank 8 个专家）。
2. 算每段大小（字节）：
   - `num_hidden_bytes = 7168 × 2 = 14336`。
   - `num_metadata_bytes = 6 × (4 + 4) + (1 + 6) × 4 = 48 + 28 = 76`。
   - 各段按 32 对齐：`hidden → 14336`（已对齐）、`sf → 0`、`metadata → 96`、`mbarrier → 32`（8 向上取整到 32）。
   - 故 `per_warp_tma_bytes = 14336 + 0 + 96 + 32 = 14464`。
3. 算 notify 共享内存：`align(8 + 64, 128) × 4 = 128 × 4 = 512` 字节。
4. 取 Hopper SM90 的最大动态共享内存为 `num_smem_bytes ≈ 233472`（228 KB，**待本地验证**）。共享内存上界：`(233472 − 512) / 14464 ≈ 16.1 → 16`。
5. warp 数上界：`32 − 4 = 28`。
6. 取 `min(16, 28) = 16`，所以 `num_dispatch_warps = 16`，`num_threads = (4 + 16) × 32 = 640`。

**需要观察的现象**：在上述配置下，共享内存上界（16）远小于 warp 数上界（28），说明「共享内存」才是瓶颈，而不是线程总数。换一个更小的 `hidden`（如 2048）重算，你会看到共享内存上界变大、可能逼近 28，那时 warp 数上界才成为约束。

**预期结果**：能够解释「为什么 DeepEP 在大 hidden 时 dispatch warp 数会明显变少」——因为每份 TMA 暂存区变大，固定容量的共享内存能容纳的份数减少。

**待本地验证**：`num_smem_bytes` 的确切值依赖设备与驱动。可在测试机设置 `EP_BUFFER_DEBUG=1`，观察 `csrc/elastic/buffer.hpp` 中 hybrid 路径打印的 `num_channels_per_sm`（直接模式的 `num_dispatch_warps` 不打印，需要自行加一行 `printf` 或在调试器中查看 `num_threads`）。

#### 4.2.5 小练习与答案

**练习 1**：把 `hidden` 从 7168 降到 2048（其余不变），`num_dispatch_warps` 会变成多少？

**答案**：`num_hidden_bytes = 4096`，`per_warp_tma_bytes = 4096 + 0 + 96 + 32 = 4224`；共享内存上界 `(233472 − 512) / 4224 ≈ 55.1 → 55`，被 warp 数上界 `28` 截断，故 `num_dispatch_warps = 28`。此时瓶颈从共享内存切换到 warp 数。

**练习 2**：为什么把 notify 计数区放在共享内存最前、dispatch 暂存区放在后面，而不是反过来？

**答案**：因为 device 侧用 `extern __shared__ int8_t smem[]` 拿到的是整块起点，notify 区长度 `kNumSmemBytesForNotify` 在编译期已知（`math::constexpr_align(...)`），dispatch 暂存区的起点 `smem + kNumSmemBytesForNotify` 也就编译期确定，便于 `BufferLayout` 用 `constexpr` 计算偏移。把定长的 notify 区放前面、变长的 dispatch 区放后面，符合「定长在前、变长在后」的内存布局惯例。

### 4.3 notify warps：逐 token 计数、跨 rank 归约与前缀和

#### 4.3.1 概念说明

notify warps 的产出是「**本 rank 即将从每个 peer rank 收到多少 token、每个本地专家收到多少 token**」。这件事需要两步归约：

1. **block 内 + 跨 SM 归约**：每个 SM（CTA）的 notify warps 各自统计「本 SM 处理的 token 里，要发给各 rank/专家的个数」，然后所有 SM 把局部计数汇总成一个全 grid 的总数。
2. **跨 rank 归约**：把「我会发给你几个」告诉每个 peer rank，再等 peer rank 把「我会发给你几个」回报回来，从而得到「我会收到几个」。

这里有个关键技术点：跨 rank 的计数用 `red_add`（原子加）写进 workspace 的固定槽位，并用 `encode_decode_positive` 把计数编码成「正数表示有效、0 表示空」的形式——因为 workspace 是所有 rank 共享的，0 被当作「未写入」哨兵，所以真实计数必须避开 0。

#### 4.3.2 核心流程

```text
notify warps (每个 SM 上前 kNumNotifyWarps 个 warp):
 1. 清零 smem 里的 rank_count[num_ranks] + expert_count[num_experts]
 2. for 本 SM 认领的 token (步长 = num_notify_warps * num_sms):
        读 topk_idx[token, :]
        expert_choice 不去重: atomicAdd(expert_count[expert_idx])
        rank_choice 按 warp 去重 (deduplicate): atomicAdd(rank_count[rank_idx])
 3. 全 grid 归约: red_add 到 workspace 的 notify_reduction 区
                  把「(本 SM 序号 << 32) | 计数」累加
 4. SM 0 轮询 workspace, 等所有 num_sms 个 SM 到齐:
        status >> 32 == kNumSMs 时, 取出总计数, encode_decode_positive 编码
        (非 NVLink 直达时) 同步写进 scaleup_rank_expert_count 的发送区
 5. SM 0 把本 rank 的 rank_count/expert_count 发给所有 peer:
        NVLink 直达: put_value 逐元素 (或 TMA 复制 expert_count)
        否则:        gin.put 批量 RDMA
 6. SM 0 轮询收回所有 peer 发来的计数, 解码后写回 smem
 7. 汇总专家计数: 写 num_unaligned / 对齐到 expert_alignment / atomicAdd cumulative stats
 8. (可选) 把计数镜像到 host workspace (供 do_cpu_sync 轮询)
 9. 前缀和:
        warp 0: inclusive psum → psum_num_recv_tokens_per_scaleup_rank[num_scaleup_ranks]
        warp 1: exclusive psum → psum_num_recv_tokens_per_expert[num_local_experts + 1]
```

#### 4.3.3 源码精读

清零 + 逐 token atomicAdd 统计（block 内局部计数）：

[deep_ep/include/deep_ep/impls/dispatch.cuh:93-107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L93-L107) —— 对每个 token 的 `num_topk` 个选择：专家选择不去重，直接 `atomicAdd_block(expert_count + dst_expert_idx, 1)`；rank 选择用 `ptx::deduplicate`（同一个 warp 内若多个 lane 选了同一 rank，只算一次）再 `atomicAdd_block`。`EP_STATIC_ASSERT(kNumTopk <= 32)` 保证 top-k 用一个 warp 的 32 个 lane 装得下。

全 grid 归约——把 `(1<<32) | count` 高位「打包 SM 序号」、低位放计数，原子加到 workspace：

[deep_ep/include/deep_ep/impls/dispatch.cuh:111-115](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L111-L115) —— 每个进来的 SM 贡献一个高 32 位的 `1`（累加后即「到齐的 SM 数」），低 32 位累加计数。SM 0 据此判断「所有 SM 是否到齐」。

SM 0 等所有 SM 到齐、编码、写发送区：

[deep_ep/include/deep_ep/impls/dispatch.cuh:118-148](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L118-L148) —— `status >> 32 == kNumSMs` 即所有 SM 到齐；用 `math::encode_decode_positive` 把计数编码（避开 0）；`not kIsScaleupNVLink` 时还要把它写进发送区供 RDMA 批量复制。`comm::timeout_while` 提供超时保护（超时打印诊断后 `trap`）。

把本 rank 计数发给 peer、再收回 peer 计数：

[deep_ep/include/deep_ep/impls/dispatch.cuh:152-178](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L152-L178) —— NVLink 直达分支（`kIsScaleupNVLink`）用 `gin.put_value` 逐元素写 expert_count；非直达分支用 `gin.put` 批量 RDMA 复制。

[deep_ep/include/deep_ep/impls/dispatch.cuh:184-201](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L184-L201) —— 轮询 `scaleup_rank_expert_count` 的接收区，`math::is_decoded_positive_ready` 判断计数到齐，解码写回 smem。

汇总专家计数、写统计张量：

[deep_ep/include/deep_ep/impls/dispatch.cuh:205-220](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L205-L220) —— 这里**写入两个关键输出张量**：`num_unaligned_recv_tokens_per_expert[i] = sum`（未对齐的真实接收数，供 epilogue 零填充用）和 `atomicAdd(cumulative_local_expert_recv_stats + i, sum)`（跨多次 dispatch 的累计统计）；随后把 `sum` 对齐到 `kExpertAlignment`。

前缀和——产出另两个关键输出张量：

[deep_ep/include/deep_ep/impls/dispatch.cuh:251-257](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L251-L257) —— `warp 0` 做**包含**前缀和写入 `psum_num_recv_tokens_per_scaleup_rank`（combine 反向路由要用）；`warp 1` 做**不包含**前缀和写入 `psum_num_recv_tokens_per_expert`（expand 布局要用）。两者都由 notify warps 在内核**最末尾**写入。

#### 4.3.4 代码实践

**目标**：追踪 `psum_num_recv_tokens_per_scaleup_rank` 与 `num_unaligned_recv_tokens_per_expert` 的写入点，理解它们「为何在 notify 阶段写、而非 dispatch 阶段」。

**步骤**：

1. 在 `csrc/elastic/buffer.hpp` 的 `dispatch` 函数里（约 833–844 行与 818–831 行），找到 `psum_num_recv_tokens_per_scaleup_rank` 与 `num_unaligned_recv_tokens_per_expert` 这两个张量的分配：它们都是新分配的 `torch::empty(...)`，`dtype=torch::kInt`，形状分别是 `[num_scaleup_ranks]` 与 `[num_local_experts]`。
2. 顺着 `launch_dispatch(...)` 的参数列表（buffer.hpp:980-1003），确认这两个指针被分别传给 `dispatch_impl` 的第 7、第 9 个参数。
3. 在 `dispatch.cuh` 里 grep 这两个形参名（`psum_num_recv_tokens_per_scaleup_rank`、`num_unaligned_recv_tokens_per_expert`），定位写入点。
4. 注意它们都出现在 `if (warp_idx < kNumNotifyWarps)` 分支内（dispatch.cuh:213 与 253、256）。

**需要观察的现象**：这两个张量在 dispatch warps 还在搬数据时就已经被 notify warps 写好了——因为 notify 的计数归约（跨 SM + 跨 rank）比 dispatch 的逐 token 搬运快得多。这就是为什么 host 侧 `do_cpu_sync` 模式可以在 dispatch 内核结束前就轮询到 host workspace 的计数（dispatch.cuh:224-229）。

**预期结果**：能复述「notify 提前算好统计 → dispatch 才慢慢搬数据 → host 可在 CPU 同步模式下提前拿到计数」这条时序链。

#### 4.3.5 小练习与答案

**练习 1**：为什么跨 rank 的计数要经过 `encode_decode_positive` 编码，而 block 内的 atomicAdd 不需要？

**答案**：block 内 atomicAdd 在共享内存上、只在本 SM 可见，初值已被本 SM 清零，无需区分「未写入」。而跨 rank 的计数写进 workspace 的接收区，该区被所有 rank 共享、会被多个 peer 同时写；用 `encode_decode_positive` 把真实计数映射到正数（避开 0），让接收方可以用「0 = 还没到 / 正数 = 已到」来轮询判断完成（`is_decoded_positive_ready`），巧妙地复用了 0 作哨兵。

**练习 2**：第 3 步全 grid 归约时，为什么把 SM 计数放在高 32 位、token 计数放在低 32 位？

**答案**：因为「到齐的 SM 数」与「计数总和」要被原子加到同一个 64 位槽位。把 SM 数放高位，SM 0 只需检查 `status >> 32 == kNumSMs` 就知道所有 SM 是否到齐，而低位在到齐时正好是所有 SM 计数的总和——一次原子加同时完成「到达计数」和「数值归约」两件事。

### 4.4 dispatch warps：TMA 暂存、槽位分配与 NVLink 对称写入

#### 4.4.1 概念说明

dispatch warps 是真正搬数据的 warp。每个 dispatch warp 处理一组 token（步长 = `num_dispatch_warps × num_sms`，跨 SM 与跨 warp 双重均分）。对每个 token，它要做四件事：

1. **搬进来**：用 TMA 把 token 的隐状态、SF 从全局内存 load 进本 warp 的共享内存暂存区，等 mbarrier 通知到齐。
2. **填元数据 + 分槽位**：把 top-k 索引、权重、来源 rank/token 索引写进暂存区的 metadata 段；并通过原子加从对端 rank 的「发送方计数器」领一个槽位号 `stored_dst_slot_idx`，决定这一份 token 落在对端缓冲区的哪个位置。同时把槽位号回写到 `dst_buffer_slot_idx` 供 combine 反查。
3. **搬出去（NVLink 直达）**：若目标 rank 在 NVLink 域内，用 `gin.get_sym_ptr` 把本地指针翻译成对端指针，直接 `tma_store_1d` 把整个暂存区写到对端缓冲区的对应槽位。
4. **搬出去（RDMA 中转）**：若目标 rank 不在 NVLink 域内（直接模式下其实不会发生，但模板要兼容多节点），先把暂存区 TMA store 到本地 send_buffer，再用 `gin.put`（RDMA）发到对端。

> 说明：直接模式下 `kIsScaleupNVLink` 通常为真（见 u3-l1），所有目标 rank 都 NVLink 可达，因此第 4 步的 RDMA 分支基本不触发；但内核保留这条分支是为了与 hybrid 共用同一份模板。本讲主题里提到的「LDG.128/256」更准确地说：**数据搬运由 TMA（`cp.async.bulk`，一次一整块）完成**，而 top-k 索引/权重这类标量是用 `__ldg`（带缓存的普通全局读）载入的——大块用 TMA、零散标量用 `__ldg`，两者配合。

#### 4.4.2 核心流程

```text
dispatch warp (dispatch_warp_idx = warp_idx - kNumNotifyWarps):
  recv_buffer = 本 rank 在对称窗口里的接收区
  send_buffer = recv_buffer 之后紧跟的发送暂存区 (仅 RDMA 路径用)
  tma_buffer  = 本 warp 在共享内存里的暂存区

  init mbarrier (tma_buffer 末尾)
  for token_idx = dispatch_warp_idx*num_sms + sm_idx; token_idx < num_tokens; token_idx += num_dispatch_warps*num_sms:
      ① tma_store_wait()                       # 等上一轮 store 完成, 暂存区可复用
      ② elect_one: tma_load_1d(hidden)         # 异步搬隐状态进暂存区
      ③ cp.async.ca(SF) + cp_async_mbarrier_arrive   # 异步搬 SF, 通知 mbarrier
      ④ __ldg 载入 top-k idx/weights, 写进暂存区 metadata
         同时 (可选) copied_topk_idx[token] = 原始 idx
      ⑤ elect_one: 写 src_token_global_idx = rank_idx*num_max_tokens_per_rank + token_idx
         tma_store_fence()                     # 共享内存写完, 给 TMA store 建立顺序
      ⑥ 槽位分配:
           cached 复用: 从 dst_buffer_slot_idx 读旧 slot
           否则:        atomicAdd(scaleup_atomic_sender_counter[dst_rank]) 领新 slot
                       回写 dst_buffer_slot_idx[token, lane] = global_slot
      ⑦ elect_one: mbarrier_arrive_and_set_tx(hidden_bytes) + mbarrier_wait_flip   # 等数据到齐
      ⑧ (非 NVLink 直达) tma_store_1d → send_buffer + commit    # RDMA 中转暂存
      ⑨ dst_ptr = gin.get_sym_ptr(recv_buffer.slot, dst_rank)   # 翻译成对端指针
         if dst_ptr: tma_store_1d(dst_ptr, tma_buffer) + commit # NVLink 直达写入
      ⑩ (非 NVLink 直达 且 不可达) tma_store_wait<1>; gin.put   # RDMA 发送
  gpu_barrier(Tag1, flush stores)               # 确保所有 token 抵达对端
```

#### 4.4.3 源码精读

缓冲区布局——recv/send/tma 三块的关系：

[deep_ep/include/deep_ep/impls/dispatch.cuh:262-268](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L262-L268) —— `recv_buffer` 是本 rank 的接收区（`get_rank_buffer(rank_idx)`），`send_buffer` 紧跟在 `recv_buffer.get_buffer_end_ptr()` 之后（仅 RDMA 路径用作中转）。`tma_buffer` 在共享内存里。

token 主循环的起点与步长——跨 SM 与跨 warp 双重均分：

[deep_ep/include/deep_ep/impls/dispatch.cuh:278-280](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L278-L280) —— `token_start = dispatch_warp_idx * kNumSMs + sm_idx`，`token_stride = kNumDispatchWarps * kNumSMs`。所有 SM 上的所有 dispatch warp 联合，恰好不重不漏地覆盖全部 token。

TMA 搬隐状态 + cp.async 搬 SF（异步载入）：

[deep_ep/include/deep_ep/impls/dispatch.cuh:288-312](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L288-L312) —— `ptx::tma_load_1d(tma_buffer.get_hidden_ptr(), x + token*kNumHiddenBytes, mbarrier_ptr, kNumHiddenBytes)` 把整段隐状态一次搬进共享内存；SF 用 `cp_async_ca` 逐 4/8/16 字节搬，搬完 `cp_async_mbarrier_arrive` 通知 mbarrier「这些字节也算数」。`elect_one_sync` 保证只由一个线程发起 TMA。

载入 top-k 索引/权重、写来源元数据：

[deep_ep/include/deep_ep/impls/dispatch.cuh:315-333](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L315-L333) —— top-k 索引/权重用 `__ldg`（标量缓存读）载入并写进暂存区；`src_token_global_idx = rank_idx * kNumMaxTokensPerRank + token_idx`，让对端收到后能知道这份 token 来自哪个 rank 的哪个槽（combine 反向路由要用）。随后的 `tma_store_fence()`（即 `fence.proxy.async.shared::cta`）保证「先写完共享内存、再发起 TMA store」的顺序——这正是近期 commit `d4f41e4` 修复的那道 fence。

槽位分配——这是 `dst_buffer_slot_idx` 的写入点：

[deep_ep/include/deep_ep/impls/dispatch.cuh:337-352](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L337-L352) —— 非 cached 模式：`ptx::deduplicate` 后由一个 lane `atomicAdd(workspace.get_scaleup_atomic_sender_counter() + dst_rank, 1)` 领一个槽位号，再把 `rank_idx*num_max + slot` 回写到 `dst_buffer_slot_idx[token_idx*kNumTopk + lane]`（这一份 token 的第 `lane` 个 top-k 选择落在了哪个全局槽）。cached 模式（`kReuseSlotIndices`）：直接从 `dst_buffer_slot_idx` 读回上次的槽位，跳过 atomicAdd。

等数据到齐：

[deep_ep/include/deep_ep/impls/dispatch.cuh:356-359](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L356-L359) —— `mbarrier_arrive_and_set_tx(kNumHiddenBytes)` 声明期望字节数（注意只声明 hidden 那段，SF 已用 `cp_async_mbarrier_arrive` 单独通知），`mbarrier_wait_and_flip_phase` 阻塞到 load 完全到齐。

NVLink 对称写入——核心两步：翻译指针 + TMA store：

[deep_ep/include/deep_ep/impls/dispatch.cuh:372-379](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L372-L379) —— `dst_ptr = gin.get_sym_ptr<team_t>(recv_buffer.get_token_buffer(slot).get_base_ptr(), stored_dst_rank_idx)` 把本地接收槽的对称指针翻译成对端 rank 的 NVLink 可达指针；若可达（`dst_ptr != nullptr`）则 `tma_store_1d(dst_ptr, tma_buffer, num_bytes)` 把整块暂存区一次性发到对端。

`get_sym_ptr` 的内部逻辑——决定走 NVLink 还是返回 `nullptr`（交由 RDMA）：

[deep_ep/include/deep_ep/common/handle.cuh:64-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/handle.cuh#L64-L92) —— 若对端 rank NVLink 不可达（`is_nvlink_accessible` 为假），直接返回 `nullptr`；否则用 `ncclGetLsaPointer` 把偏移加到对端基址上，得到对端等价指针。这就是「对称内存跨 rank 寻址」的落点。

RDMA 中转分支（直接模式下基本不触发，但模板保留）：

[deep_ep/include/deep_ep/impls/dispatch.cuh:363-393](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L363-L393) —— 不可达 rank：先把暂存区 `tma_store_1d` 到本地 `send_buffer`，`tma_store_wait<1>()` 等 store 落盘，再 `gin.put` 用 RDMA 发到对端的同一槽位。

内核末尾的到达栅栏：

[deep_ep/include/deep_ep/impls/dispatch.cuh:398-400](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L398-L400) —— `gpu_barrier<..., kDispatchTag1, true, true, false>`：`kFlushStores=true` 会先 `tma_store_commit + tma_store_wait`（comm.cuh:219-223）确保本 grid 所有 TMA store 都已离开本 SM，再跨 rank 做屏障，保证「所有 token 已抵达各对端缓冲区」——epilogue 内核才能放心地把它们拷出去。

#### 4.4.4 代码实践

**目标**：跟踪一个 token 从「本地全局内存」到「对端 rank 接收槽」的完整路径，标注每一步用的是 TMA 还是 `__ldg`。

**步骤**：

1. 在 `dispatch.cuh` 第 280 行的 `for` 循环内，逐句标注数据来源/去向：
   - 第 289 行 `tma_load_1d(...)`：标注 `# 全局 → 共享内存 (TMA bulk)`。
   - 第 303–308 行 `cp_async_ca(...)`：标注 `# 全局 SF → 共享内存 (cp.async)`。
   - 第 318 / 323 行 `__ldg(topk_idx ...)` / `__ldg(topk_weights ...)`：标注 `# 全局标量 → 寄存器 (__ldg)`。
   - 第 332 行 `*get_src_token_global_idx_ptr() = ...`：标注 `# 寄存器 → 共享内存 metadata (普通写)`。
   - 第 377 行 `tma_store_1d(dst_ptr, tma_buffer, ...)`：标注 `# 共享内存 → 对端全局 (TMA bulk, NVLink)`。
2. 对照 [ptx.cuh:115-127](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L115-L127)（`tma_load_1d`）与 [ptx.cuh:129-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L129-L139)（`tma_store_1d`），确认它们底层的 PTX 是 `cp.async.bulk....global.mbarrier::complete_tx` 与 `cp.async.bulk.global.shared::cta`。

**需要观察的现象**：整份 token 的搬运几乎全部由 TMA（`cp.async.bulk`）完成，CPU/标量读只用于 top-k 索引、权重这类少量路由信息。这正是 dispatch 内核能用很少的线程/SM 就吃满 NVLink 带宽的原因——TMA 是硬件引擎，不需要线程轮询。

**预期结果**：能画出一条「global →(TMA)→ smem →(填 metadata + 领 slot)→(TMA)→ 对端 global」的数据通路，并指出每段对应的 PTX 指令族。

#### 4.4.5 小练习与答案

**练习 1**：`stored_dst_slot_idx` 是怎么算出来的？为什么需要它？

**答案**：非 cached 模式下，dispatch warp 对去重后的目标 rank 调 `atomicAdd(workspace.get_scaleup_atomic_sender_counter() + dst_rank, 1)`，返回值就是「本 rank 要发给该对端 rank 的第几个槽」。需要它是因为：本 rank 会把多个不同 token 发给同一个对端 rank，必须给每一份分配一个不冲突的落点，对端才能在 combine/epilogue 时按槽位回查。`dst_buffer_slot_idx` 把这个全局槽位回写，正是 combine 反向路由的依据。

**练习 2**：为什么在第 333 行写完 metadata 后要插一句 `ptx::tma_store_fence()`？

**答案**：因为接下来要用 TMA 把整块暂存区（含刚写入的 metadata）store 到对端。TMA store 由异步代理（proxy）执行，而前面的普通共享内存写在「计算代理」视角下。`fence.proxy.async.shared::cta`（即 `tma_store_fence`）建立一条「计算代理写 → 异步代理读」的顺序保证，确保 TMA 引擎读到的暂存区里 metadata 已经写完。缺失它可能导致 TMA 读到旧值（这正是 commit `d4f41e4` 在 mbarrier wait 与 TMA load 之间补 fence 要解决的同类问题）。

## 5. 综合实践

把本讲两个核心模块串起来——**warp 划分（4.1）+ 共享内存推导（4.2）+ 输出张量写入时机（4.3/4.4）**——完成下面这个端到端追踪任务。

**任务**：给定一组真实配置，预测 `num_dispatch_warps` 与 `num_threads`，并验证它们能让内核正确跑通。

**操作步骤**：

1. 在单机 8 卡环境跑通 `tests/elastic/test_ep.py`（参考 u1-l4）。选用 BF16、`--num-experts 64`、`--num-topk 6`、`--hidden 7168`（若脚本支持命令行传参；否则在脚本里改常量）。
2. 在 `csrc/kernels/elastic/dispatch.hpp` 的 `launch_dispatch` 中、第 190 行 `num_threads = ...` 之后，临时加一行调试打印：

   ```cpp
   // 示例代码（仅用于调试，验证后请删除）
   if (getenv("EP_BUFFER_DEBUG"))
       printf("[direct-dispatch] num_sms=%d num_notify_warps=%d num_dispatch_warps=%d num_threads=%d notify_smem=%d per_warp=%d\n",
              num_sms, num_notify_warps, num_dispatch_warps, num_threads,
              num_notify_smem_bytes,
              get_dispatch_token_layout(hidden, elem_size, num_sf_packs, num_topk).get_num_bytes<true>());
   ```

   > 注意：这是**示例代码**，不是项目原有代码。验证完后务必删除，不要提交。
3. 重新编译（`pip install -e .`），用 `EP_BUFFER_DEBUG=1` 跑测试，记录打印出的 `num_dispatch_warps` 与 `num_threads`。
4. 用 4.2.4 的公式手算 `num_dispatch_warps`，与打印值对比。

**需要观察的现象**：

- 打印的 `num_dispatch_warps` 应等于 `min((num_smem_bytes - notify_smem) / per_warp, 28)`。
- 若你手算时假设的 `num_smem_bytes` 与设备实际值不同，二者会不一致——这正是「待本地验证」的部分；用打印值反推设备的 `num_smem_bytes`，与 `cudaDeviceProp::sharedMemPerBlockOptin` 核对。
- 多次 dispatch（不同 token 数）下，`num_dispatch_warps` 应**保持不变**（它只依赖 hidden/topk/ranks/experts 与 smem，不依赖 `num_tokens`），印证「配置即模板」的设计。

**预期结果**：能解释 `num_dispatch_warps` 如何同时被共享内存与 warp 数约束，并说出当前配置下哪一个约束是瓶颈。

## 6. 本讲小结

- 直接模式 dispatch 内核把一个 CTA 的 warp 分成 **notify warps（默认 4 个，负责计数与跨 rank 归约）** 与 **dispatch warps（负责搬数据）** 两组，前者提前产出统计、后者专心传输，互不阻塞。
- `num_dispatch_warps = min((num_smem_bytes - notify_smem) / per_warp_tma_bytes, 32 - num_notify_warps)`：同时受**共享内存容量**与**每 CTA 最多 32 warp** 两道上界约束；大 hidden 时共享内存是瓶颈。
- 每个 dispatch warp 在共享内存里有一份独立的 **TMA 暂存区**，大小为 `TokenLayout::get_num_bytes<true>`（hidden + SF + metadata + mbarrier，各段按 32 对齐）。
- notify warps 在内核末尾用前缀和写出 `psum_num_recv_tokens_per_scaleup_rank`（inclusive，warp 0）与 `psum_num_recv_tokens_per_expert`（exclusive，warp 1），并在汇总阶段写 `num_unaligned_recv_tokens_per_expert` 与 `cumulative_local_expert_recv_stats`。
- dispatch warps 在槽位分配阶段写 `dst_buffer_slot_idx`（cached 模式下改为读），通过 `atomicAdd` 向对端 rank 的发送方计数器领槽；token 的搬运几乎全由 TMA（`cp.async.bulk`）完成，标量路由信息用 `__ldg`。
- NVLink 可达的对端用 `gin.get_sym_ptr` 翻译指针后 `tma_store_1d` 直达；不可达对端经 send_buffer 中转 + `gin.put` 走 RDMA（直接模式下基本不触发）。

## 7. 下一步学习建议

- 本讲只覆盖了**单节点直接模式**。当你需要理解多节点场景下 scaleout（RDMA）+ scaleup（NVLink）的两级转发、channel 模型与 `token_metadata_at_forward`/`channel_linked_list` 的布局时，请进入 **u5-l2 Hybrid Dispatch：scaleout + scaleup 两级通信**。
- dispatch 主内核只把 token 写进对称缓冲区；真正把它们拷成 `recv_x`/`recv_topk_idx` 并做 expand 布局与零填充的是独立的 epilogue 内核，见 **u5-l3 Dispatch copy epilogue 与 expand 布局**。
- 想深入理解本讲反复出现的 TMA、mbarrier、`fence.proxy.async.shared::cta` 等 PTX 原语的底层语义，见 **u8-l1 PTX 原语：TMA、mbarrier 与 fence.proxy**。
- 想理解 `do_cpu_sync` 如何在 dispatch 内核仍在跑时就轮询到 notify 写出的计数、以及 cached handle 如何跳过统计重算，见 **u5-l4 CPU 同步、cached handle 与推理解码复用**。
