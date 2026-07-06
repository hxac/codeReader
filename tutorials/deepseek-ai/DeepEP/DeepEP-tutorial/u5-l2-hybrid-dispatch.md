# Hybrid Dispatch：scaleout + scaleup 两级通信

## 1. 本讲目标

本讲承接 [u5-l1 直接模式 Dispatch](u5-l1-direct-dispatch.md)，把视角从「单节点纯 NVLink」扩展到「多节点混合网络」。

读完本讲你应该能够：

- 说清楚为什么多节点场景下不能只用一套 all-to-all，而要引入 **channel** 概念做两级（scaleout + scaleup）通信。
- 在 `hybrid_dispatch_impl` 内核中分清三类 warp（notify / scaleout / forward）各自的数据通路：谁负责计数、谁走 RDMA 跨节点、谁在节点内 NVLink 转发。
- 读懂 hybrid 模式下三个关键 cached handle 元数据张量——`dst_buffer_slot_idx`、`token_metadata_at_forward`、`channel_linked_list`——的形状与含义，并解释它们如何把 dispatch 与 combine 串起来。
- 理解 channel 数量 `num_channels_per_sm` 是如何由共享内存、combine 布局、`prefer_overlap_with_compute` 共同决定的。

本讲只讲 **dispatch 方向**；combine 方向（它如何「重放」dispatch 的转发路径）留待 [u6-l1](u6-l1-combine-main.md) 与 [u6-l2](u6-l2-combine-reduce-epilogue.md)。本讲也不展开 PTX/TMA/mbarrier 原语本身（见 [u8-l1](u8-l1-ptx-tma-mbarrier.md)）。

## 2. 前置知识

在进入源码前，先用一张「快递网」的比喻建立直觉。

### 2.1 为什么单级 all-to-all 在多节点上不够好

直接模式（u5-l1）假设所有 rank 之间都能用同一种链路（NVLink）互通。但真实集群是分层的：

- **节点内（intranode）**：8 张 GPU 之间用 NVLink 直连，带宽极高（数百 GB/s）。
- **节点间（internode）**：靠 RDMA 网卡（如 CX7），带宽远低于 NVLink（数十 GB/s），且要走 PCIe/NIC。

如果让「节点 0 的 GPU0」直接给「节点 7 的 GPU3」发数据，这条 RDMA 链路会成为瓶颈。**Hybrid 模式**的核心想法是：把一次 all-to-all 拆成两级——

1. **scaleout（RDMA）级**：每个节点先把自己要发往「其他节点」的 token 集中起来，按**目标节点**打包，走 RDMA 一次性发到对端节点的「收发区」。
2. **scaleup（NVLink）级**：对端节点收到包后，再在**节点内**用 NVLink 把 token 分发到该节点上正确的 GPU。

这就对应了 u3-l1 讲过的逻辑域划分：`num_scaleout_ranks = 节点数`，`num_scaleup_ranks = 每节点 GPU 数`，且恒有 `num_ranks = num_scaleout_ranks × num_scaleup_ranks`。`EP 8 x 2` 就是「每节点 8 GPU、共 2 节点」，即 `num_scaleout_ranks=2, num_scaleup_ranks=8`。

### 2.2 channel 是什么

把两级通信塞进**同一个 GPU kernel** 里同时跑，会遇到一个问题：RDMA 是异步的，发起 `put` 之后数据还在路上，节点内的 forward 不能无限等待。于是 DeepEP 引入 **channel（通道）** 概念：

- 把每个 SM 上的数据搬运工作切成若干条独立的「流水线」（channel）。
- 每条 channel 像一条独立的传送带：scaleout warp 往传送带上放货（RDMA 发），forward warp 从传送带上取货（NVLink 转发），两者用「已发送到第几个」这样的信号量（signaled tail）轻量同步，而不需要全局 barrier。
- channel 越多，RDMA 与 NVLink 之间的流水线重叠越充分；但每个 channel 在共享内存里要占一份 TMA 暂存区，所以 channel 数受共享内存约束。

源码里有一句注释直接点题——**「a warp is a channel」**：一条 channel 就由一对 warp（一个 scaleout warp + 一个 forward warp）驱动。

### 2.3 IBGDA 与 signaled tail

scaleout 发送用的是 `gin.put`（NCCL Gin 的 RDMA put，底层是 IBGDA——In-Band GPU Direct Async，让 GPU 自己发起 RDMA 而不经 CPU）。因为 RDMA 发送是异步的，发送方需要一种机制告诉接收方「我已经把第 N 个槽位写好了」，这就是 **signaled tail**：发送方维护一个单调递增的「尾指针」，周期性地用原子加（`red_add_rel`）把它推送给接收方；接收方轮询这个尾指针来知道现在可以处理到第几个 token。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/kernels/elastic/dispatch.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | host 侧启动器：根据 `num_scaleout_ranks` 选择实例化 `dispatch_impl` 还是 `hybrid_dispatch_impl`；计算 warp 数、channel 数 |
| [deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh) | hybrid dispatch 的 GPU 内核 `hybrid_dispatch_impl`，三类 warp 的全部逻辑都在这里 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer::dispatch` 的 host 实现：计算 channel、分配三个元数据张量、调用启动器 |
| [deep_ep/include/deep_ep/common/layout.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | `BufferLayout`、`TokenLayout`：缓冲区/token 的内存布局，含 `get_channel_buffer`、`get_linked_list_idx_ptr` |
| [deep_ep/include/deep_ep/common/comm.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh) | `get_qp_mode`（QP 与 channel 的映射）、`gpu_barrier`（两级同步原语）、`timeout_while`（超时轮询） |

调用方向回顾（u1-l2 已建立）：Python `buffer.dispatch()` → `ElasticBuffer::dispatch`（buffer.hpp）→ `launch_dispatch`（dispatch.hpp）→ JIT 实例化 `hybrid_dispatch_impl`（hybrid_dispatch.cuh）。本讲聚焦最后两跳里 **hybrid 分支** 的细节。

---

## 4. 核心概念与源码讲解

### 4.1 channel 模型：为什么需要 channel，数量怎么算

#### 4.1.1 概念说明

直接模式下，一个 CTA（线程块）内部把 warp 分成 notify + dispatch 两组，所有 token 共用一组数据通路。到了多节点，数据要先 RDMA 出去、再被对端节点内 NVLink 转发，这两件事耗时差异巨大（RDMA 慢得多）。如果只有一条通路，慢的 RDMA 会把快的 NVLink 拖死。

channel 的解法是**流水线化 + 多通道**：让一个 SM 同时跑多条独立流水线（每条 = 一个 scaleout warp + 一个 forward warp），scaleout 在发第 N+1 批的同时，forward 已经在处理第 N 批，从而把 RDMA 延迟「藏」到 NVLink 转发背后。同时，channel 也是 RDMA 负载在 QP（队列对）间分散的基本单位（见 4.2）。

#### 4.1.2 核心流程

channel 数量在 host 侧由 `ElasticBuffer::dispatch` 决定，关键约束有三条：

1. **共享内存约束**：每个 channel 要在 shared memory 里放一份 TMA 暂存区（一份给 scaleout、一份给 forward），所以 channel 数不能超过 `(smem - notify_smem) / token_bytes`。
2. **combine 布局约束**：dispatch 和 combine 分时复用同一块 buffer，channel 数还要被 combine 的 token 布局约束再压一次。
3. **两类 warp 对称**：scaleout warp 和 forward warp 各占一份暂存区，所以最终 channel 数要除以 2；同时封顶 `kNumMaxChannelsPerSM = 8`。

最终 `num_channels = num_sms * num_channels_per_sm`。

#### 4.1.3 源码精读

channel 上限常量定义在 buffer.hpp（注意 `kNumMaxChannels` 同时也是缓冲区尺寸计算里用作 per-channel padding 的上界）：

[DeepEP-tutorial 引用 / csrc/elastic/buffer.hpp:57-59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L57-L59) —— 定义 `kNumMaxChannelsPerSM=8`、`kNumMaxSMs=160`、`kNumMaxChannels=1280`，它们既是 channel 数的封顶，也用于估算 RDMA 接收区要预留多少 padding。

channel 数的真实推导在 `ElasticBuffer::dispatch` 里（hybrid 才算，直接模式恒为 1）：

[csrc/elastic/buffer.hpp:848-867](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L848-L867) —— 依次用 dispatch 布局、combine 布局、`/2`（两类 warp）、`kNumMaxChannelsPerSM` 取 `min`；若 `prefer_overlap_with_compute=false` 再压到 4 以内（多用 SM、不刻意省给计算流）。最后 `num_channels = num_sms * num_channels_per_sm`。

把这个 channel 数传给启动器后，dispatch.hpp 把它翻译成 warp 数：

[csrc/kernels/elastic/dispatch.hpp:191-196](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L191-L196) —— hybrid 分支里 `num_scaleout_warps = num_forward_warps = num_channels_per_sm`，于是 `num_threads = (notify + scaleout + forward) * 32`。**一个 channel = 一个 scaleout warp + 一个 forward warp** 的对应关系就在这里建立。

在设备侧，channel 数被编译期化为模板派生常量：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:22-27](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L22-L27) —— `kNumChannelsPerSM = kNumScaleoutWarps`、`kNumChannels = kNumScaleoutWarps * kNumSMs`、`kNumMaxTokensPerChannel = ceil_div(kNumMaxTokensPerRank, kNumChannels)`。`kScaleoutUpdateInterval = 6` 是「每攒 6 个 token 推送一次 signaled tail」的批次粒度（见 4.3）。

> 一句话：channel 是 host 用共享内存/combine 布局算出来的，下发给设备后即等于 scaleout/forward 的 warp 对数；它既是流水线深度，也是 RDMA 在 QP 间做负载均衡的粒度。

#### 4.1.4 代码实践

**实践目标**：观察 channel 数随配置变化。

1. 设置 `export EP_BUFFER_DEBUG=1`（这个开关会让 buffer.hpp:865-866 打印 `Elastic buffer uses %d channels per SM`）。
2. 在多节点环境（或构造时强制 `allow_hybrid_mode=True` 的单节点）跑 `tests/elastic/test_ep.py`，分别用 `--num-sm-scheduler-max`/`--hidden` 不同值跑两次。
3. **需要观察的现象**：终端输出 `Elastic buffer uses N channels per SM`；`hidden` 越大（token 字节越多），N 越小（共享内存先耗尽）。
4. **预期结果**：N ∈ {1,2,3,4}（被 `kNumMaxChannelsPerSM=8` 与 `/2` 共同约束）；若 `prefer_overlap_with_compute=False`，N 最多为 4。
5. 单节点（`num_scaleout_ranks==1`）下不会进入这段代码，N 不打印——**待本地验证**你能否在单机环境强行触发 hybrid 分支（可能需要多节点）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 channel 数计算里要做一次 `/2`？
**答**：因为每条 channel 在共享内存里需要两份 TMA 暂存区——一份给 scaleout warp（发）、一份给 forward warp（收转发），两者各自 `num_channels_per_sm` 个 warp。`/2` 把「warp 对数」换算回「channel 数」。

**练习 2**：`kNumMaxChannels = 1280` 这个常量除了封顶，还在哪里被用到？
**答**：在 buffer.hpp:608-609 的 hybrid 缓冲区尺寸计算里，scaleout 接收区的 token 数被写成 `num_max_tokens_per_rank + kNumMaxChannels`，多出的 `kNumMaxChannels` 是为每个 channel 的尾部对齐/边界预留的 padding。

---

### 4.2 三类 warp 的分工与 QP / channel 映射

#### 4.2.1 概念说明

`hybrid_dispatch_impl` 把一个 CTA 的 warp 分成三组，各司其职：

| warp 组 | 数量 | 职责 |
| --- | --- | --- |
| **notify warps** | `kNumNotifyWarps`（默认 4） | 统计每个 rank/专家收到多少 token，做全 grid + 跨 rank 归约，写计数给 host（CPU sync）和后续 epilogue |
| **scaleout warps** | `kNumChannelsPerSM` | 把 token 经 RDMA（`gin.put`）发到对端**节点**的接收区，并维护 signaled tail |
| **forward warps** | `kNumChannelsPerSM` | 轮询对端发来的 token，在**节点内**用 NVLink（`gin.get_sym_ptr` + TMA store）转发到目标 GPU |

注意 notify warps 与 u5-l1 直接模式的 notify 几乎是同一套逻辑（计数、`encode_decode_positive` 编码、前缀和），区别在于归约要分两级：先在节点内（scaleup）归约，再跨节点（scaleout）汇总。

#### 4.2.2 核心流程

每个数据 warp（scaleout/forward）启动时先通过 `get_qp_mode` 拿到自己的 QP 编号与共享模式，再进入对应的 `if/else if/else` 分支。整条 kernel 的骨架是：

```
gpu_barrier(tag0)          # 开局同步：确保所有 rank 的窗口/信号量就绪
if notify warp:    计数 + 两级归约 + 前缀和
elif scaleout warp: 遍历本 channel 的 token → TMA load → RDMA put → 更新 signaled tail
else (forward warp): 轮询 signaled tail → TMA load 收到的 token → NVLink 转发 → 写 linked list
gpu_barrier(tag1, scaleup) # 收尾同步：确保 NVLink 写入对 combine 可见
cudaTriggerProgrammaticLaunchCompletion()  # PDL：触发 epilogue
```

QP 映射决定了多条 channel 如何复用有限的 RDMA 队列对——这是 hybrid 模式比 direct 模式占用更多 QP（默认 65/129 vs 17）的根本原因。

#### 4.2.3 源码精读

三类 warp 的入口判断与「a warp is a channel」注释：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:56-79](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L56-L79) —— 第 57 行注释「a warp is a channel (different channels may share QPs)」；第 77-79 行 `get_qp_mode<...>(sm_idx, channel_in_sm, is_notify_warp)` 返回 `(qp_idx, sharing_mode)`，据此构造 `handle::NCCLGin`。

`get_qp_mode` 的分配策略（数据 channel 部分）：

[deep_ep/include/deep_ep/common/comm.cuh:70-86](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L70-L86) —— 若 `kNumSMs <= 可用 QP 数`，则每个 SM 独占若干 QP（`kSharingCTA`，CTA 内共享）；否则所有 SM 轮流复用所有 QP（`kSharingGrid`，grid 共享）。这就是为什么 hybrid 要分配较多 QP——给每个 channel 一条尽量独立的 RDMA 通道，减少队头阻塞。

开局与收尾的两级 barrier：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:82-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L82-L84) —— 开局 `gpu_barrier<..., kHybridDispatchTag0, ..., kSyncAtStart=true>`。
[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:663-668](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L663-L668) —— 收尾只做 **scaleup** barrier（注释解释：scaleout 的 token 已被 forward 消费完，不必再 scaleout barrier），随后 `cudaTriggerProgrammaticLaunchCompletion()` 用 PDL（Programmatic Dependent Launch）唤醒 epilogue 内核。

`gpu_barrier` 内部会按 `do_scaleout/do_scaleup` 选择并行做两级 barrier 还是单级：

[deep_ep/include/deep_ep/common/comm.cuh:232-259](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/comm.cuh#L232-L259) —— `do_scaleout &= num_scaleout_ranks>1`、`do_scaleup &= num_scaleup_ranks>1`；两级同时存在时，SM0 做 scaleup barrier，其余 SM 做 scaleout barrier，并行推进。

#### 4.2.4 代码实践

**实践目标**：源码阅读型——画出三类 warp 的时序。

1. 阅读上文第 56-79、82-84、663-668 三段代码。
2. 在纸上画三条横线（notify / scaleout / forward），标出：开局 barrier（三组都参与）、各组主体工作、收尾 scaleup barrier（三组都参与）、PDL 触发。
3. **需要观察的现象**：notify warps 是否参与数据搬运？（不参与，只计数）；forward warps 是否发起 RDMA？（不发起，只做 NVLink 转发）。
4. **预期结果**：得到一张「三泳道」时序图，能清楚指出 RDMA 只出现在 scaleout 泳道、NVLink TMA store 只出现在 forward 泳道。

#### 4.2.5 小练习与答案

**练习 1**：为什么收尾 barrier 只做 scaleup、不做 scaleout？
**答**：scaleout warp 发出的 token 已经被本节点的 forward warp 消费（读走）了；combine 阶段需要的是 forward 写入对端 GPU 的 NVLink 数据，因此只需保证 scaleup（NVLink）写入可见即可，scaleout 接收区已无用。

**练习 2**：notify warp 与数据 warp 的 QP 分配有何不同？
**答**：comm.cuh:67-68 显示 notify warp 固定用 QP 0、`kSharingCTA` 模式（它只用 1 个 SM 做归约收尾）；数据 channel 则按 channel/SM 数动态分配，可能独占也可能 grid 共享。

---

### 4.3 scaleout 链路：notify 两级计数 + RDMA 发送到对端节点

#### 4.3.1 概念说明

scaleout 链路包含两件事：**计数（notify warps）**和**发送（scaleout warps）**。

计数的目的和直接模式一样——告诉后续 epilogue「每个专家收到了几个 token」、告诉 host「每个 scaleup rank 收到了几个 token」（用于 CPU sync 与 combine 路由）。但多节点下计数要分两级归约：

- 先在**节点内**（scaleup 域）把所有 SM 的本地计数全 grid 归约；
- 由 SM0 把节点内汇总结果用 `gin.put` 跨 RDMA 发给**所有对端节点**；
- 每个节点收到所有对端的计数后，再次归约，写回本节点各 scaleup peer 的计数器。

发送则由 scaleout warps 完成：每个 channel 的 scaleout warp 以步长 `kNumChannels` 遍历 token，对每个 token 算出它要去的目标**节点**（`expert_idx / kNumExpertsPerScaleout`），把 token 拷到本节点的发送暂存区，再用 `gin.put` 经 RDMA 发到对端节点的接收区对应 channel 槽位。

#### 4.3.2 核心流程

**notify warps**（hybrid_dispatch.cuh:107-328）：

```
1. 共享内存里 atomicAdd 统计每个 rank/expert 的本地 token 数
2. red_add 写入 workspace，做全 grid 归约（SM 序号压高位、计数压低位）
3. SM0 等所有 SM 到齐 → 编码成正数 → gin.put 发给所有 scaleout 对端
4. SM0 轮询并归约所有 scaleout 对端发来的计数 → 写入本节点各 scaleup peer 的计数器（gin.put_value / red_add_rel）
5. 等本节点 scaleup 计数就绪 → 对齐 expert_alignment → 写 host workspace（CPU sync）/ unaligned 计数
6. warp0/warp1 分别做 inclusive / exclusive 前缀和 → 写 psum_num_recv_tokens_per_scaleup_rank / per_expert
```

**scaleout warps**（hybrid_dispatch.cuh:329-463）：

```
channel_idx = sm_idx * kNumChannelsPerSM + scaleout_warp_idx
preload_next_token(channel_idx)             # TMA 预取第一个 token
for token_idx in stride(channel_idx, kNumChannels):
    读 topk_idx → 算 dst_scaleout_rank = expert / kNumExpertsPerScaleout
    去重 → 领发送槽位 stored_dst_slot_idx
    更新 scaleout_tail（每向一个新 rank 发货就 +1）
    mbarrier wait → TMA store 到本节点发送暂存区（若有远端目标）
    若目标 == 本节点：直接 TMA store 进本节点接收区（local bypass，省 RDMA）
    gin.put → RDMA 发到对端节点接收区的对应 channel 槽
    每 kScaleoutUpdateInterval(=6) 个：update_scaleout_tail 把 tail 推给 forward warp
收尾：update_scaleout_tail(finish=true) 刷新剩余 tail
```

#### 4.3.3 源码精读

notify 的跨节点计数交换（SM0 内）：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:174-188](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L174-L188) —— 用 `gin.put<ncclTeamTagRail>` 把本节点的 rank/expert 计数广播给每个 scaleout 对端（`ncclTeamTagRail` = 节点间 team）。

scaleout warp 的 token 主循环与 RDMA 发送：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:389-456](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L389-L456) —— 第 396 行算 `dst_scaleout_rank_idx = expert_idx / kNumExpertsPerScaleout`；第 428-431 行若有远端目标则 TMA store 进发送暂存区；第 436-439 行「目标==本节点」时直接 TMA store 进接收区（local bypass）；第 448-455 行 `gin.put<ncclTeamTagRail>(... dst_scaleout_rank_idx, ncclGinOptFlagsAggregateRequests)` 真正发起 RDMA。

signaled tail 的周期性推送（让 forward warp 知道「已发到第几个」）：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:338-351](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L338-L351) —— `update_scaleout_tail`：每攒够 `kScaleoutUpdateInterval=6` 个（或 `finish` 时），用 `gin.red_add_rel<ncclTeamTagRail>` 把 `(finish_flag, tail)` 打包值原子加到对端 forward warp 会轮询的信号槽 `get_scaleout_channel_signaled_tail_ptr`。`release` 语义保证 RDMA 数据先于 tail 信号可见。

host 侧启动器把 `scaleout_rank_idx`、`scaleup_rank_idx` 注入内核（hybrid 分支比 direct 多传这两个）：

[csrc/kernels/elastic/dispatch.hpp:108-125](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L108-L125) —— 注意 hybrid 分支比 direct 分支多传了 `args.token_metadata_at_forward`、`args.scaleout_rank_idx`、`args.scaleup_rank_idx`（direct 只传 `scaleup_rank_idx`，因为 direct 没有 scaleout 概念）。

#### 4.3.4 代码实践

**实践目标**：理解 local bypass 与 signaled tail 的配合。

1. 阅读 hybrid_dispatch.cuh:428-456。
2. 回答：如果一个 token 的所有 top-k 专家都在**本节点**，scaleout warp 会发起 RDMA 吗？
3. **需要观察的现象**：`scaleout_rank_mask ^ (1 << scaleout_rank_idx)` 这个条件（第 428 行）控制是否 TMA store 进发送区——当掩码里只剩下本节点这一位时，异或结果为 0，跳过发送区写入；第 448 行 `stored_dst_scaleout_rank_idx != scaleout_rank_idx` 进一步保证不发 RDMA 给自己。
4. **预期结果**：纯节点内目标时，token 直接进本节点接收区（436-439 行），零 RDMA 流量——这正是单节点退化为直接模式的等价行为。
5. **待本地验证**：在 2 节点环境实际跑，用 `nvprof`/`nsys` 观察当所有 token 都路由到本节点专家时 RDMA put 次数是否趋近 0。

#### 4.3.5 小练习与答案

**练习 1**：`kScaleoutUpdateInterval = 6` 大一点或小一点各有什么影响？
**答**：调大→signaled tail 推送次数少、RDMA 元开销低，但 forward warp 能开始的转发批次粒度粗，流水线重叠变差；调小→重叠更好，但 `red_add_rel` 原子操作更频繁、且 forward 轮询更碎。6 是权衡值。

**练习 2**：notify 的计数为什么必须分两级（scaleup 内先归约、再 scaleout 跨节点）？
**答**：因为计数最终要落到「每个 scaleup rank 收到多少」（epilogue/combine 用）和「每个专家收到多少」（expand 用）。直接跨所有 `num_ranks` 做 allreduce 会绕过 NVLink 的高带宽优势；先节点内 NVLink 归约、再节点间 RDMA 汇总，恰好匹配物理拓扑的两级带宽。

---

### 4.4 forward 链路：节点内 NVLink 转发与 channel_linked_list

#### 4.4.1 概念说明

forward warps 是 hybrid 模式独有的「第二级」。它在本节点内消费 scaleout 收到的 token，再用 NVLink 转发到该 token 真正归属的 GPU。

由于 forward 与 scaleout 是异步流水线，forward 必须解决两个问题：

1. **怎么知道现在可以处理哪些 token？** → 轮询 scaleout warp 推送过来的 signaled tail，按 (channel, scaleout_rank) 轮询（round-robin）。
2. **combine 阶段怎么知道收到了哪些 token、它们来自哪、原 slot 在哪？** → 这就是 `channel_linked_list` 与 `token_metadata_at_forward` 两个元数据张量存在的理由。forward 每转发一个 token，就在链表里挂一个节点、在 metadata 里记一条「源 token 全局号 + 各 topk 的目标 scaleup rank + slot」。

#### 4.4.2 核心流程

**forward warps**（hybrid_dispatch.cuh:464-659）：

```
channel_idx = sm_idx * kNumChannelsPerSM + forward_warp_idx   # 与对应 scaleout warp 同 channel
while 还有未处理完的 scaleout rank:
    round-robin 选一个 recv_scaleout_rank_idx
    轮询 scaleout_channel_signaled_tail → 得到 (finish_flag, tail)
    处理一个 chunk（最多 kNumSlotsPerForwardChunk=6 个连续槽位）:
        for slot in [start, end):
            TMA load 收到的 token 进共享内存
            读 topk_idx → 算 dst_scaleup_rank = (expert - scaleout_offset) / kNumExpertsPerRank
            写 linked_list 节点索引（transform_linked_list_idx）
            去重 → atomicAdd 领对端 scaleup peer 的 slot
            TMA store（经 gin.get_sym_ptr 翻译到远端 GPU）→ NVLink 转发
            记录 token_metadata_at_forward 一条
    写链表尾指针 channel_scaleup_tail_ptr（gin.get_sym_ptr 跨 rank 写到对端 workspace）
    清理 signaled_tail 供下次使用
```

**链表如何把 token 串起来**：对每个 `(channel, scaleup 目标 peer)` 维护一条链。每转发一个 token，forward 在共享内存里维护一个「该 peer 已发几个」的计数器，把**上一个计数**作为链表节点索引写进 token 的元数据（随 token 一起 NVLink 发走）；最后把「该 peer 的最后一个节点索引」作为尾指针，跨 rank 写到对端 GPU 的 `channel_scaleup_tail_ptr`。combine 阶段从尾指针出发，沿着每个 token 自带的 `linked_list_idx` 一路回溯，就能枚举出该 peer 在该 channel 收到的全部 token 及其在接收 buffer 里的位置。

#### 4.4.3 源码精读

forward warp 入口与 channel 对齐：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:464-482](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L464-L482) —— `forward_warp_idx` 算出的 `channel_idx` 与 scaleout warp 的 `channel_idx` 一一对应（同一个 channel 的发与收）。`transform_linked_list_idx`（478-482）把 `(运行索引 idx, scaleup_rank)` 线性化为 `channel_linked_list` 张量里的偏移。

轮询 signaled tail + round-robin 选 rank：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:493-526](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L493-L526) —— `gather` 收集各 lane 的「有数据/已结束」状态，`ffs` 选下一个有效 rank；`ld_acquire_sys` 读 signaled_tail，`unpack2` 拆出 `(finish_flag, tail)`。

NVLink 转发（领 slot + 跨 rank TMA store）：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:579-599](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L579-L599) —— `kReuseSlotIndices`（cached 模式）时直接 `__ldg` 读回上次的 slot；否则 `atomicAdd(scaleup_atomic_sender_counter + peer, 1)` 领新 slot。随后 `gin.get_sym_ptr<ncclTeamTagLsa>(scaleup_buffer slot, peer)` 把本 rank 的 slot 指针翻译成对端 GPU 的对称指针，`tma_store_1d` 经 NVLink 写过去。

记录 `token_metadata_at_forward` 一条：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:612-630](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L612-L630) —— 第 0 维存源 token 全局号、第 1 维存「是否本 chunk 最后一个」、其后 `num_topk` 维存各 topk 的目标 scaleup rank、再 `num_topk` 维存对应 slot。这条记录是 combine 「重放 dispatch」的依据。

链表尾指针跨 rank 写到对端：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:636-658](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L636-L658) —— 先写 `-1` 作为 metadata 结束标记（636-638），再把每个 scaleup peer 的链表尾节点 `st_relaxed_sys` 写到对端 `channel_scaleup_tail_ptr`（642-652），最后清理 signaled_tail（656-657）。

链表节点的载体——每个 token 的 TMA 元数据里的 `linked_list_idx` 槽：

[deep_ep/include/deep_ep/common/layout.cuh:242-244](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L242-L244) —— `get_linked_list_idx_ptr()` 紧跟在 `src_token_global_idx` 之后，是 token 打包布局里专门留给链表节点索引的一段。

> 关于 `stored_scaleup_send_counters` 的 exchange 技巧（565-571 行）：它是每个 lane 用 `ptx::exchange` 维护的「本 lane 负责的那个 scaleup peer 的已发计数」寄存器，用于在不停下做全局归约的前提下，按 lane 分片地生成单调链表节点号。初读时不必深究其 shuffle 细节，把握「每转发一个 token 就在链表里挂一个节点、尾指针跨 rank 通知对端」即可。

#### 4.4.4 代码实践

**实践目标**：跟踪一个 token 从对端节点到本节点目标 GPU 的完整路径。

1. 假设节点 0 的 GPU0 有一个 token，它的某个 top-k 专家在「节点 1 的 GPU5」。在 hybrid_dispatch.cuh 里标注这个 token 经过的每一步：
   - 节点0 scaleout warp：TMA store → 发送暂存区 → `gin.put` 到节点1 接收区某 channel 槽（448-455 行）。
   - 节点1 forward warp：轮询 tail → TMA load → 算出 dst_scaleup_rank=5 → atomicAdd 领 slot → `tma_store_1d` 经 NVLink 到 GPU5（579-599 行）。
2. **需要观察的现象**：这个 token 在节点1 内部「换了一次载体」——从 RDMA 接收区搬到 NVLink 接收区（scaleup_buffer）。
3. **预期结果**：画出两级跳转图，确认 RDMA 只跨节点一次、NVLink 只跨 GPU 一次，token 不走「节点0 → 节点1所有GPU → GPU5」这种朴素全连接。
4. **待本地验证**：在多节点跑 `tests/elastic/test_ep.py`，对照 `deep_ep/utils/refs.py` 的 `ref_dispatch` 检查 `recv_x` 数值正确。

#### 4.4.5 小练习与答案

**练习 1**：为什么 forward warp 用 `atomicAdd` 领 slot，而不是像 scaleout 那样按 token 顺序算槽位？
**答**：因为多个 channel（多个 SM 上的 forward warp）会并发地向同一个 scaleup peer 发送，必须用原子操作保证每个 `(peer, slot)` 唯一；而 scaleout 是按 channel 内 token 顺序、且每个 channel 有自己独立的接收槽区，可以顺序分配。

**练习 2**：`channel_linked_list` 的第 0 号节点为什么是「起始项（head）」？
**答**：buffer.hpp:944 注释明确「Index 0 of the list means the starting item」。链表用尾指针标记结尾、用 0 号节点作为哨兵起点，combine 从尾指针回溯到 0 号即枚举完毕，避免单独存链表长度。

---

### 4.5 cached handle 的元数据张量布局

#### 4.5.1 概念说明

推理解码场景下，路由（topk_idx）每步都变，但若用 cached handle（见 [u5-l4](u5-l4-cpu-sync-cached-handle.md)）复用上一步的**接收布局**，就能跳过 CPU 同步与张量重算。hybrid 模式因为多了两级转发，cached handle 要多复用三个张量：`dst_buffer_slot_idx`、`token_metadata_at_forward`、`channel_linked_list`。理解它们的形状是本讲的核心交付物之一，也是本讲代码实践的重点。

#### 4.5.2 核心流程

`ElasticBuffer::dispatch` 在 hybrid 分支（`num_scaleout_ranks > 1`）会分配/校验这三个张量；`combine` 则反向读取它们。三者的维度都围绕 `num_channels`、`num_max_tokens_per_channel` 展开：

```
num_max_tokens_per_channel = ceil(num_max_tokens_per_rank / num_channels)
```

#### 4.5.3 源码精读

`dst_buffer_slot_idx`（hybrid 形状）：

[csrc/elastic/buffer.hpp:887-905](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L887-L905) —— 形状 `[num_channels, num_scaleout_ranks, num_max_tokens_per_channel, num_topk]`。注释（888-890 行）：「from channel i from scale-out peer k, the j-th token's index in the l-th rank buffer」。它记录的是 **forward 阶段**每个 token 被发到了对端哪个 scaleup rank 的哪个 slot——cached 模式下直接复用，省掉 atomicAdd 领 slot。

`token_metadata_at_forward`：

[csrc/elastic/buffer.hpp:907-929](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L907-L929) —— 形状 `[num_channels, num_max_forwarded_tokens, num_forward_metadata_dims]`，其中
`num_max_forwarded_tokens = num_scaleout_ranks * num_max_tokens_per_channel + 1`（+1 给结束标记 -1），
`num_forward_metadata_dims = 2 + num_topk * 2`。注释（908-913 行）说明每条记录含：源 scaleout rank + 源 token 号（0）、是否 chunk 末尾（1）、各 topk 的目标 scaleup rank（topk）、各 topk 的 slot（topk）。这正是 combine「重放 dispatch」所读的表（combine 侧 [buffer.hpp:1262-1281](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1262-L1281) 校验同样的形状）。

`channel_linked_list`：

[csrc/elastic/buffer.hpp:931-951](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L931-L951) —— 形状 `[num_channels, num_scaleout_ranks * num_max_tokens_per_channel + 1, num_scaleup_ranks]`。注释（931-932 行）：「from channel i from scaleup peer k, the j-th token's index in the combine's input」。第 1 维的 +1 同样是给 0 号哨兵 head。

hybrid 模式下 cached 必须额外提供这两个张量（否则 host 断言失败）：

[csrc/elastic/buffer.hpp:744-748](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L744-L748) —— cached 模式且 `num_scaleout_ranks > 1` 时，强制要求 `cached_token_metadata_at_forward` 与 `cached_channel_linked_list` 都 has_value，否则断言失败。direct 模式不需要它们。

设备侧对这些张量偏移的索引计算：

[deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh:470-482](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_dispatch.cuh#L470-L482) —— forward warp 一进来就把 `token_metadata_at_forward`、`dst_buffer_slot_idx` 按 `channel_idx` 偏移到本 channel 的起始位置，并定义 `transform_linked_list_idx` 完成 `(idx, scaleup_rank) → 线性偏移` 的映射。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：对照源码，口算/推演三个元数据张量的形状，并解释 `channel_linked_list` 如何串起 token 供 combine 使用。

1. **给定配置**（典型训练设置）：`num_max_tokens_per_rank = 4096`，`hidden = 7168`，`num_topk = 6`，`num_experts = 256`，`EP 8 x 2`（即 `num_scaleout_ranks = 2, num_scaleup_ranks = 8`），BF16，假设 `num_sms = 24`、`num_channels_per_sm = 2`。
2. **操作步骤**：
   - 算 `num_channels = 24 * 2 = 48`；
   - 算 `num_max_tokens_per_channel = ceil(4096 / 48) = 86`；
   - 推三个张量形状：
     - `dst_buffer_slot_idx` = `[48, 2, 86, 6]`
     - `token_metadata_at_forward` = `[48, 2*86+1=173, 2+6*2=14]`
     - `channel_linked_list` = `[48, 2*86+1=173, 8]`
   - 打开 buffer.hpp:887-951 与 hybrid_dispatch.cuh:470-482、612-658 逐项核对。
3. **解释 `channel_linked_list` 如何串 token**：
   - forward warp 每向某个 scaleup peer 转发一个 token，就在该 peer 的计数器上 +1，把**上一次的计数值**作为链表节点号写进 token 的 `linked_list_idx` 元数据（随 token NVLink 发走）；
   - 一个 channel 对一个 peer 的所有转发构成一条链，链尾节点号被 `st_relaxed_sys` 跨 rank 写到对端的 `channel_scaleup_tail_ptr`（hybrid_dispatch.cuh:643-652）；
   - combine 阶段（[hybrid_combine.cuh:150-166](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L150-L166)）从 `channel_scaleup_tail_ptr` 取尾节点，沿 `channel_linked_list[node]` 逐跳回溯，每跳得到一个 token 在接收 buffer 里的索引，直到回到 0 号 head——从而无歧义地枚举出每个 (channel, scaleup peer) 收到的全部 token。
4. **需要观察的现象**：三个张量的第 1 维都含 `+1`（`num_max_forwarded_tokens` 与链表第 1 维），这个 +1 同时服务于「结束标记 -1」与「0 号 head 哨兵」。
5. **预期结果**：能手写出一个 `(channel=0, scaleup_peer=3)` 的链表示意：`tail → node_5 → node_4 → ... → node_0(head)`，每个 node 指向 combine 输入 buffer 中的一个 token 槽。
6. **待本地验证**：在多节点用 `EP_BUFFER_DEBUG=1` 打印实际 `num_channels_per_sm`，替换上面的 2，重算形状。

#### 4.5.5 小练习与答案

**练习 1**：direct 模式（`num_scaleout_ranks==1`）的 `dst_buffer_slot_idx` 形状是什么？为什么与 hybrid 不同？
**答**：direct 模式是 `[num_tokens, num_topk]`（buffer.hpp:879-880），因为不存在 channel/scaleout 两级，每个 (token, topk) 直接对应一个对端 rank 的 slot；hybrid 则要按 (channel, scaleout_peer, token_in_channel, topk) 四维索引。

**练习 2**：cached 模式下，hybrid 为什么能省掉 forward 的 `atomicAdd` 领 slot？
**答**：cached 模式 `kReuseSlotIndices=true`，forward warp 直接 `__ldg(dst_slot_idx_ptr + lane_idx)` 读回上一次的 slot（hybrid_dispatch.cuh:582-584），跳过 atomicAdd；前提是路由布局（哪些 token 去哪个 peer）与上一步一致——这正是 cached handle 复用的语义保证。

**练习 3**：`token_metadata_at_forward` 里「是否 chunk 末尾」这个 flag（第 1 维）对 combine 有什么用？
**答**：combine 在「重放 dispatch」时按 chunk 批量处理（hybrid_combine.cuh 用 `kNumScaleoutUpdateInterval` 之类批次），「chunk 末尾」flag 让 combine 知道一个转发批次在哪里结束，从而对齐它的 scaleout tail 推送节奏。

---

## 5. 综合实践

把本讲的知识串起来，做一个**端到端路径标注 + 形状核对**的综合任务。

**场景**：`EP 8 x 2`（2 节点 × 8 GPU），`num_max_tokens_per_rank=4096`，`hidden=7168`，`num_topk=6`，`num_experts=256`，BF16。

**任务 A — 画拓扑与数据通路**：
1. 画 2×8 的 GPU 矩阵，标出 scaleout 边界（节点间 RDMA）与 scaleup 边界（节点内 NVLink）。
2. 选一个 token，其 top-k 中有一个专家在「另一节点的 GPU5」。在图上画出它经过的路径：本 GPU →（NVLink 汇聚到本 channel 的 scaleout warp 暂存区）→ RDMA 跨节点 → 对端节点接收区 channel 槽 →（forward warp）→ NVLink → 目标 GPU5。
3. 在路径每一段旁标注对应的源码行：TMA store 进发送区（dispatch.hpp / hybrid_dispatch.cuh:428-431）、`gin.put` RDMA（448-455）、signaled tail 推送（338-351）、forward TMA load（546-551）、NVLink `tma_store_1d`（593-599）。

**任务 B — 核对元数据张量形状**：
1. 设置 `EP_BUFFER_DEBUG=1`，跑 `tests/elastic/test_ep.py`（多节点），记录打印的 `channels per SM`。
2. 据此算出 `num_channels`、`num_max_tokens_per_channel`，写出三个元数据张量的完整形状。
3. 在 `csrc/elastic/buffer.hpp:887-951` 找到对应分配代码，逐维核对一致。

**任务 C — 解释链表（结合 combine）**：
1. 打开 `deep_ep/include/deep_ep/impls/hybrid_combine.cuh`，找到读取 `channel_linked_list` 与 `channel_scaleup_tail_ptr` 的循环（约 150-166 行）。
2. 用本讲 4.5 学到的链表结构，解释 combine 是如何「从尾指针出发、逐跳回溯到 head」枚举 token 的，并说明这与 dispatch forward 写尾指针（hybrid_dispatch.cuh:643-652）如何构成闭环。

**验收标准**：能脱稿讲清「一个跨节点 token 的两级跳转」「三个元数据张量形状的来源」「链表如何把零散到达的 token 串成 combine 可枚举的有序集合」这三件事。

> 注：任务 A/C 是源码阅读型实践，可在单机完成；任务 B 的实测部分需要多节点环境，单机无法触发 hybrid 分支（`num_scaleout_ranks==1`）。

## 6. 本讲小结

- **channel 是 hybrid 的核心抽象**：host 用共享内存/combine 布局算出 `num_channels_per_sm`，下发后等于 scaleout/forward 的 warp 对数；它既是 RDMA/NVLink 的流水线深度，也是 QP 负载均衡的粒度（「a warp is a channel」）。
- **三类 warp 分工**：notify 做两级计数归约（先 scaleup 内、再 scaleout 跨节点）；scaleout warp 走 RDMA 把 token 发到对端节点；forward warp 在节点内用 NVLink 把收到的 token 转发到目标 GPU。
- **scaleout 链路**靠 signaled tail 与 forward 异步流水线：每攒 6 个 token 用 `red_add_rel` 把尾指针推给 forward，local bypass 让纯节点内目标零 RDMA。
- **forward 链路**靠 `atomicAdd` 领 slot + `gin.get_sym_ptr` + TMA store 完成 NVLink 转发，并把每个 token 的转发信息记进 `token_metadata_at_forward`、链表节点随 token 发走、尾指针跨 rank 写到对端。
- **三个 cached 元数据张量**（`dst_buffer_slot_idx`、`token_metadata_at_forward`、`channel_linked_list`）形状都围绕 `num_channels`、`num_max_tokens_per_channel` 展开，是 cached handle 复用与 combine 反向路由的依据。
- hybrid 模式比 direct 多用 QP（默认 65/129 vs 17）、多一组 forward warp，但换来多节点下 RDMA 与 NVLink 的流水线重叠，这是它的根本收益。

## 7. 下一步学习建议

- **combine 方向**：本讲的 `token_metadata_at_forward` 和 `channel_linked_list` 是为 combine 准备的，下一步直接读 [u6-l1 Combine 主流程](u6-l1-combine-main.md) 看 combine 如何「重放 dispatch」，以及 [u6-l2 Combine reduce epilogue](u6-l2-combine-reduce-epilogue.md) 看多 rank 加权归约。
- **cached handle 全貌**：本讲只涉及 cached 的元数据布局，完整的「首次 dispatch → 后续复用」工作流见 [u5-l4 CPU 同步、cached handle 与推理解码复用](u5-l4-cpu-sync-cached-handle.md)。
- **底层原语**：若对 `tma_store_1d`、`mbarrier_wait_and_flip_phase`、`fence.proxy.async.shared::cta`、`ld_acquire_sys`/`st_relaxed_sys` 等内存序原语有疑问，读 [u8-l1 PTX 原语：TMA、mbarrier 与 fence.proxy](u8-l1-ptx-tma-mbarrier.md)。
- **QP 与拓扑**：想深入了解 `get_qp_mode`、`NCCL_GIN_CONNECTION_RAIL/FULL` 与多平面网络的关系，读 [u3-l1 物理域与逻辑域](u3-l1-topology-domains.md) 与 [u8-l2 NCCL communicator 复用与拓扑探测](u8-l2-nccl-comm-reuse.md)。
