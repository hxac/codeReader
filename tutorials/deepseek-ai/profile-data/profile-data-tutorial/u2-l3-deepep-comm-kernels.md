# 训练/预填充轨迹中的 DeepEP 通信内核

## 1. 本讲目标

读完本讲，你应该能够：

1. 在轨迹中一眼认出 DeepEP 的四类「内节点（internode）」通信内核：`dispatch`、`combine`、`notify_dispatch`、`cached_notify`，以及布局内核 `dpsk::ep::cuda::get_dispatch_layout`。
2. 说清楚 dispatch 与 combine 分别是 MoE all-to-all 通信的两个方向：把 token 发往专家所在的 GPU，再把专家结果按原路聚合回来。
3. 用脚本统计通信类内核的次数与总时长，并计算它们占全部 GPU kernel 时间的比例（本讲实测：train.json 中约 46.5%）。
4. 在时间线上观察到通信内核与 attn/mlp 计算内核的交错重叠，并理解为什么通信内核只占用极少的 SM。
5. 掌握一个重要教训：**注解名 ≠ 内核名的字面对应**——必须用上一讲（u2-l1）的 correlation 外键反查，才能知道某段 CPU 注解真正启动了哪些 GPU 内核。

## 2. 前置知识

本讲建立在 u1 系列（事件格式、可视化）与 u2-l1/u2-l2（CPU-GPU 关联、1F1B 注解）之上。先用通俗语言补齐通信侧的概念。

- **MoE 与 Top-K 路由**：混合专家模型（Mixture of Experts）的每一层不是一个大网络，而是很多个「专家」小网络 \( E_1,\dots,E_N \)。每个 token \( x_i \) 由路由器选出 Top-K 个专家并给出权重，输出为

  \[ y_i=\sum_{e\in\operatorname{TopK}(i)} g_{i,e}\cdot E_e(x_i) \]

  也就是说，一个 token 的计算结果需要它选中的**所有**专家算完再加权求和。
- **专家并行（EP）**：把不同专家放到不同 GPU 上。EP64 = 64 个 GPU 参与专家并行（train 轨迹的配置，见 README）。这样每个 token 几乎必然要「离开」本地 GPU 去找它的专家。
- **all-to-all / dispatch / combine**：EP 的通信模式是 all-to-all——每个 GPU 都要发给其他所有 GPU。DeepSeek 的开源通信库 **DeepEP** 把它拆成两个方向：
  - **dispatch（分发）**：把每个 token（的激活）按路由结果发往专家所在的 GPU；
  - **combine（聚合）**：专家算完后，把结果按原路发回 token 的来源 GPU，并按路由权重 \( g_{i,e} \) 合并。
  - `dpsk` 是这些 DeepSeek 自研内核的命名空间前缀；`internode` 表示「跨节点」版本，走 RDMA 网络，用于训练与预填充（解码用的低延迟版本 `dispatch_ll/combine_ll` 是 u2-l5 的内容）。
- **握手（notify）**：发数据之前，双方要先交换「我要发给你多少」这类规模信息，接收方才能分配缓冲区、发起接收。这类交换在轨迹里体现为 notify 系内核。
- **RDMA**：远程直接内存访问，网卡直接读写对端显存/内存，绕过操作系统内核，是跨节点 all-to-all 的物理载体。
- **CUDA stream 与 SM**：stream 是 GPU 上的任务队列，同一 stream 内的内核严格串行，不同 stream 可并行；SM（流式多处理器）是 GPU 的计算单元，H800 有 132 个 SM（u1-l3 读过的 `deviceProperties`）。u1-l4 已确认 train.json 中计算内核集中在 stream 7（另有少量在 stream 23），通信内核全部在 stream 27。
- **复习两件事**：GPU `kernel` 事件通过 `args.correlation` 与 CPU 侧 `cuda_runtime` 事件一一对应（u2-l1）；`user_annotation`（如 `dispatch(F)`、`combine(B)`）是框架在 CPU 上打的调度注解，其 `dur` 是 CPU 墙钟时间，不是 GPU 时长（u2-l2）。
- **解读前提**（README 反复强调）：采集时模拟了**绝对均衡**的 MoE 路由，见 [README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `train.json` | 本讲主对象。EP64 训练轨迹，963 个 kernel 事件，其中 40 个落在通信流 stream 27 上，本讲逐个精读 |
| `README.md` | 场景配置的权威说明：训练 EP64/TP1/4K 序列、PP 通信未计入；预填充双微批；解码不占 SM |
| `prefill.json` | 延伸对照。同样的 DeepEP 内核族在 EP32 预填充中出现 580 次（本讲只做统计对比，详细内核图谱留给 u2-l4） |

## 4. 核心概念与源码讲解

### 4.1 dispatch 与 combine：MoE 的 all-toall 两个方向

#### 4.1.1 概念说明

EP 把专家分散在几十上百张 GPU 上，而每个 token 只需要其中 Top-K 个专家。于是每个 MoE 层的前向必须完成一次「全局重排」：

- **dispatch**：本地把 token 按「目标专家所在的 GPU（rank）」分组，用 RDMA 写到对端预留的接收缓冲区。dispatch 之后，每张 GPU 收到的恰好是「驻留在本地的那些专家」要处理的 token。
- **combine**：本 GPU 的专家算完后，输出张量要按 dispatch 的**逆过程**发回各 token 的来源 GPU，来源端再按路由权重把 K 份部分结果加权求和。

Dispatch 内核签名里的 `dpsk::ep::internode::SourceMeta`（来源元数据）正体现了这一点：发送时记录每个 token 从哪来，combine 时才能原路返回。

反向传播同样要做这两件事，只不过是针对梯度张量：把 \( \partial L/\partial y \) 发给专家端，聚合回 \( \partial L/\partial x \)。这解释了 u2-l2 看到的注解为什么有 `dispatch(F)/dispatch(B)/combine(F)/combine(B)` 四种——每个 MoE 层前向一次、反向一次，共两次 dispatch、两次 combine。

#### 4.1.2 核心流程

一个 EP 下的 MoE 层（伪代码）：

```text
topk_idx, topk_w = router(x)                # 每个 token 选 K 个专家
layout = get_dispatch_layout(topk_idx)      # 统计每对 (src_rank, dst_rank) 的 token 数
recv = dispatch(x, topk_idx, layout)        # all-to-all：token → 专家所在 GPU
y_local = experts_local(recv)               # 本地专家计算（grouped GEMM 等）
out = combine(y_local, topk_w)              # all-to-all：结果 → 来源 GPU + 加权求和
```

在轨迹层面（train.json 实测，详见 4.4）：每个 MoE 层在通信流 stream 27 上留下一组固定节奏的内核——两次 dispatch、两次 combine，外加握手与布局内核，共 9 个 DeepEP 内核；4 个 MoE 层（一个 chunk）共 36 个 `dpsk::ep::` 内核。

#### 4.1.3 源码精读

dispatch 内核事件（第一层前向附近的一次发送）：

[train.json:L31323-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31323-L31338)

这一段是一个完整的 `cat: "kernel"` 完成事件（`ph: "X"`）：内核名 `void dpsk::ep::internode::dispatch<false, 8, true, 8>(...)`，运行在 `pid 0 / tid 27`（GPU 0 的通信流）；`ts/dur` 表明它执行了 3320 微秒；`args` 里 `stream: 27`、`correlation: 44508` 是回查 CPU 调用的外键；`grid [20,1,1] × block [512,1,1]` 说明它只启动了 20 个 block——H800 有 132 个 SM，通信内核最多只在约 15% 的 SM 上落了线程，把大部分计算单元留给并行的计算内核。

combine 内核事件：

[train.json:L35711-L35726](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35711-L35726)

这是第一层的第一次 combine：模板参数里出现 `__nv_bfloat16`（聚合结果以 BF16 写回），`dur 6549µs` 是全文件最长的通信内核之一；同样 `grid [20,1,1]`（block 加宽到 800 线程），估计占用率仅 6%。它的 `correlation: 46454` 将在 4.4.3 中用来演示错位配对。

对照配置说明：

[README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)

README 说明训练轨迹是 DualPipe 中「一对前向/反向 chunk」的重叠策略展示，每 chunk 含 4 个 MoE 层，配置 EP64、TP1、4K 序列，且 **PP 通信未计入轨迹**——所以轨迹里的通信内核只有 DeepEP 的 EP all-toall，这正好让我们能干净地只研究 dispatch/combine。

#### 4.1.4 代码实践

实践目标：亲手数出 train.json 中 dispatch/combine 内核的次数与时长分布。

操作步骤（示例代码，jq 一行版）：

```bash
jq -r '[.traceEvents[] | select(.ph=="X" and .cat=="kernel" and .tid==27)]
       | sort_by(.ts)[] | "\(.dur)us  \(.name|split("(")[0])"' train.json
```

需要观察的现象：输出共 40 行（40 个通信流内核）；其中名字以 `dispatch<` 开头的 8 个（两个模板实例各 4 个）、`combine<` 开头的 8 个。

预期结果（笔者已验证）：

| 内核族 | 次数 | 总时长 | 单次最大 |
| --- | --- | --- | --- |
| `dpsk::ep::internode::dispatch<false, 8, true, 8>` | 4 | 13621µs | 3613µs |
| `dpsk::ep::internode::dispatch<false, 8, false, 8>` | 4 | 17383µs | 4604µs |
| `dpsk::ep::internode::combine<false, 8, __nv_bfloat16, ...>` | 8 | 49448µs | 6549µs |

combine 单次时长普遍长于 dispatch（约 5.8~6.5ms 对 3.1~4.6ms）——聚合方向要把 BF16 结果写回所有来源端，数据量与同步开销都更大。

#### 4.1.5 小练习与答案

1. **练习**：为什么 dispatch 与 combine 在一个 MoE 层里各出现 2 次而不是 1 次？
   **答案**：一次前向、一次反向。反向传播时梯度同样要发给专家端（dispatch）、再聚合回输入端（combine），对应注解 `dispatch(F)/dispatch(B)/combine(F)/combine(B)` 各 4 次（4 个 MoE 层），内核侧 8+8 次，计数严格对上。
2. **练习**：dispatch 内核名里的 `SourceMeta` 起什么作用？
   **答案**：它是「来源元数据」，记录每个收到的 token 来自哪个 GPU/哪个位置，combine 阶段据此把专家输出原路发回，是两个方向能配对的关键。
3. **练习**：combine 的模板参数里有 `__nv_bfloat16`，这暗示什么？
   **答案**：聚合传输的数据以 BF16 精度进行（DeepSeek-V3 的激活精度），而 dispatch 方向配合 FP8 通信流上的 `per_token_cast_to_fp8` 量化内核（见 4.4）。

### 4.2 notify 家族：数据传输前的握手内核

#### 4.2.1 概念说明

可靠的全互联通信不能「闷头发数据」：接收方必须提前知道将从每个对端收到多少 token，才能划分接收缓冲、初始化计数器；RDMA 写也依赖双方协商好的地址与规模。这类「先打招呼」的步骤在 DeepEP 里由 notify 系内核完成，轨迹里能看到两种：

- `dpsk::ep::internode::notify_dispatch<false, 8>`：随 dispatch 走的握手内核；
- `dpsk::ep::internode::cached_notify<false>`：「复用缓存」的握手内核。

从名字与出现时机可以推断：当这一轮通信的规模信息可以复用（例如同一层反向沿用前向的路由布局）时走 `cached_notify`，需要重新交换规模时走 `notify_dispatch`。两者模板参数与语义的准确对应需查阅 DeepEP 源码确认（待确认），但「每个主通信内核之前都紧邻一次握手内核」这一节奏在数据里是稳定可验证的。

#### 4.2.2 核心流程

实测的配对节奏（train.json，按执行时间排序）：

```text
cached_notify  →  dispatch<..., true, 8>          # 复用握手的一次发送
get_dispatch_layout → notify_dispatch → dispatch<..., false, 8>   # 重新布局+握手的发送
cached_notify  →  combine                          # 聚合前的握手
cached_notify  →  combine                          # 第二次聚合前的握手
```

数量对账：16 个主通信内核（8 dispatch + 8 combine）恰好对应 4 次 `notify_dispatch` + 12 次 `cached_notify` = 16 次握手，一对一。

#### 4.2.3 源码精读

cached_notify 内核事件：

[train.json:L31291-L31306](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31291-L31306)

这是全文件第一个 DeepEP 内核：`cached_notify` 执行 1008µs，`grid [20,1,1] × block [320,1,1]`，`blocks per SM 0.15`、估计占用率 2%——握手内核只做计数与信令交换，几乎不消耗计算资源。它结束仅 2µs 后，同流的 dispatch 内核（L31323）立刻开跑，印证「先握手、后传数据」的串行节奏。

#### 4.2.4 代码实践

实践目标：验证「每个 dispatch/combine 内核前面都紧贴一个 notify 系内核」。

操作步骤（示例代码）：

```bash
jq -r '
[.traceEvents[] | select(.ph=="X" and .cat=="kernel" and .tid==27
                         and (.name|contains("dpsk::ep::")))]
| sort_by(.ts) | .[1:][] | select((.name|contains("dispatch<")) or (.name|contains("combine<")))
| "\(.name|split("(")[0]|.[0:44]) 在 ts=\(.ts) 启动"' train.json
```

再与 4.1.4 的完整时序列表对照，逐行检查每个 dispatch/combine 的前一行是否为 `cached_notify` 或 `notify_dispatch`。

需要观察的现象：8 个 dispatch/combine 主内核中，每一个的前一个同流内核都是 notify 系内核，且时间上首尾相接（间隙在微秒级）。

预期结果：16/16 全部满足。（笔者已验证；唯一插在中间的是每次循环开头的 FP8 量化内核，它出现在 cached_notify 之前，不打断握手→传输的衔接。）

#### 4.2.5 小练习与答案

1. **练习**：为什么握手与数据传输要拆成两个内核，而不是合并成一个大内核？
   **答案**：握手本质是与所有对端交换控制信息，必须等最慢的对端响应（内核时长里大量是这种等待）；数据传输则是纯粹的拷贝。拆开后各自流水、失败域也更清晰。从数据看，cached_notify 单次可达 3.8ms，主要就是等待。
2. **练习**：`notify_dispatch` 4 次、`cached_notify` 12 次的差异暗示了什么？
   **答案**：16 次主通信中只有 4 次（每层 1 次）需要完整握手，其余 12 次复用了缓存的规模信息——均衡路由下同一层的收发规模确定性更强，缓存命中率天然高。
3. **练习**：握手内核的 `est. achieved occupancy %` 只有 2%，这正常吗？
   **答案**：正常。这类内核受限于等待与同步，而不是计算吞吐，低占用率是设计使然；也正因如此它占用的 SM 极少。

### 4.3 get_dispatch_layout：路由布局计算内核

#### 4.3.1 概念说明

dispatch 之前必须回答一个纯本地的问题：「我手上的这些 token，按路由结果要发多少给每个目标 GPU？」`dpsk::ep::cuda::get_dispatch_layout` 就是干这个的：输入 token→专家的 Top-K 索引，输出每对源/目的的计数、偏移和合法性掩码，供握手与发送使用。它是通信的「排货单」。

#### 4.3.2 核心流程

```text
输入: topk_idx (long 数组, 每 token K 个专家编号)
输出: 各通道计数 / 累计偏移 / token 排列索引 / 越界掩码 (bool)
性质: 纯本地计算, 不产生网络流量
调用时机: 紧贴 notify_dispatch 之前
```

train.json 中每层 1 次、共 4 次；prefill.json 中每次 dispatch 前都出现，共 116 次（真实在线路由每步变化，布局不可复用；train 是模拟的绝对均衡路由，复用条件更好）。

#### 4.3.3 源码精读

[train.json:L35379-L35394](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35379-L35394)

从签名 `get_dispatch_layout<256, 32, 8>(long const*, int*, int*, int*, bool*, int, int, int, int)` 可以读出：第一个参数是 `long const*`（Top-K 索引数组），随后 4 个输出指针（3 个 `int*` 计数/偏移/索引 + 1 个 `bool*` 掩码）——这是从参数类型做的合理推断。值得注意的硬件细节：`shared memory: 41984`（每 block 约 41KB 共享内存，用于多专家并行计数）与 `grid [16,1,1]`，`dur` 仅 74~142µs——相对动辄几毫秒的传输本身，布局计算几乎免费。

#### 4.3.4 代码实践

实践目标：对比该内核在两份轨迹中的出现密度。

操作步骤（示例代码）：

```bash
for f in train.json prefill.json; do
  jq -r --arg f "$f" '[.traceEvents[] | select(.ph=="X" and .cat=="kernel"
      and (.name|contains("get_dispatch_layout")))] | "\($f): n=\(length)"' "$f"
done
```

需要观察的现象：train 中 4 次、prefill 中 116 次；同时对比 dispatch 内核总数（train 8 次、prefill 116 次）。

预期结果：train 中「布局计算 : dispatch」= 4:8（一半的 dispatch 复用了旧布局）；prefill 中为 116:116（每次都重算）。（笔者已验证。）

#### 4.3.5 小练习与答案

1. **练习**：为什么 prefill 每次都要重算布局，而 train 可以复用？
   **答案**：train 采集时模拟绝对均衡路由（README 前提），前后两轮的收发规模高度可预测；prefill 是真实在线流量，每个批的路由都不同，布局必须重算。
2. **练习**：这个内核会产生网络流量吗？
   **答案**：不会。它只读本地路由索引、写本地统计量，是通信前的纯本地预处理。

### 4.4 通信与计算的交错：通信流上的十步循环与重叠实证

#### 4.4.1 概念说明

通信内核单独看只是「耗时」，有价值的是它与计算的关系。README 对训练轨迹的说明（DualPipe 用一对前向/反向 chunk 互相填充空泡）与对预填充的说明（双微批重叠计算与 all-to-all，[README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)）最终都落在 GPU 时间线上「通信流与计算流同时忙碌」这一可观测事实上。本节用数据把这个事实钉死。

#### 4.4.2 核心流程

train.json 通信流（stream 27）上，每个 MoE 层重复一个**十步循环**（第一层实测值）：

| # | 内核 | 时长 | 说明 |
| --- | --- | --- | --- |
| 1 | `cuda::per_token_cast_to_fp8_with_channels` | 52µs | 发送前 FP8 量化（被排入通信流保序） |
| 2 | `dpsk::ep::internode::cached_notify` | 1008µs | 握手 |
| 3 | `dpsk::ep::internode::dispatch<..., true, 8>` | 3320µs | 发送（复用布局） |
| 4 | `dpsk::ep::cuda::get_dispatch_layout` | 74µs | 重算布局 |
| 5 | `dpsk::ep::internode::notify_dispatch` | 1381µs | 完整握手 |
| 6 | `dpsk::ep::internode::dispatch<..., false, 8>` | 4604µs | 发送（新布局） |
| 7 | `dpsk::ep::internode::cached_notify` | 753µs | 握手 |
| 8 | `dpsk::ep::internode::combine` | 6549µs | 聚合 ① |
| 9 | `dpsk::ep::internode::cached_notify` | 1323µs | 握手 |
| 10 | `dpsk::ep::internode::combine` | 6002µs | 聚合 ② |

四层各跑一轮，共 40 个内核、busy 时长逐层略增（25.1 → 27.1 → 30.1 → 32.3ms），合计 114.5ms——与 112.2ms 的 1F1B 注解窗口相当，即**通信流几乎全程忙碌**。同期计算流 stream 7 上排满 907 个计算内核（118.2ms）。

定量汇总（train.json 全部 963 个 kernel 事件、总 kernel 时长 244.0ms）：

| 流 | 内核数 | 总时长 | 占比 |
| --- | --- | --- | --- |
| stream 7（计算主流） | 907 | 118165µs | 48.4% |
| stream 23（计算，部分反向 grouped GEMM） | 16 | 11355µs | 4.7% |
| stream 27（通信） | 40 | 114521µs | **46.9%** |

其中 `dpsk::ep::` 内核 36 个共 113440µs，占全部 GPU kernel 时间的 **46.5%**；另外 4 个是 FP8 量化内核。

#### 4.4.3 源码精读

**(a) 重叠的直接证据**。取第一层的 combine ① 内核（[train.json:L35711-L35726](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35711-L35726)，执行区间 6549µs），检查同区间计算流上的内核，能找到 **17 个计算内核**（合计 8125µs）：

```text
tid=7/23  cutlass fp8_dptp128c_acc grouped GEMM ×12   (专家 GEMM，部分在 stream 23)
tid=7     per_channel_cast_and_transpose              (FP8 scale 处理)
tid=7     clean_and_count_expert / get_fused_mapping  (MoE 路由统计)
tid=7     expand_to_fused_with_scales_impl            (按 scale 展开)
tid=7     batched_transpose / elementwise             (布局与逐元素)
```

这个 6.5ms（6549µs）的通信窗口内，stream 7 忙了 5280µs（80.6%）、stream 23 忙了 2845µs（43.4%）——**combine 在网上收发的同时，SM 几乎满负荷地在算 MoE**。这就是 README 截图里通信条与计算条并排铺满的微观解释。

**(b) 错位配对：注解名 ≠ 内核名**。用 u2-l1 的方法把 36 个 DeepEP 内核按 `correlation` 反查回启动它们的 CPU 注解，得到稳定的错位模式（每层）：

| CPU 注解（启动点） | 该区间内启动的 GPU 内核 |
| --- | --- |
| `combine(B)` | `cached_notify` + `dispatch<true>` |
| `dispatch(F)` | `get_dispatch_layout` + `notify_dispatch` + `dispatch<false>` |
| `dispatch(B)` | `cached_notify` + `combine ①` |
| `combine(F)` | `cached_notify` + `combine ②` |

具体证据链：combine ① 的 `correlation` 为 46454，对应的 CPU 侧启动事件在 [train.json:L35731-L35738](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35731-L35738)（`cudaLaunchKernelExC`，ts=…478346），它落在 `dispatch(B)` 注解（[train.json:L17685-L17691](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L17685-L17691)，区间 …478267~…478414）之内——**名为 dispatch(B) 的 CPU 区间里启动的是 combine 内核**。合理推断是 DeepEP 把一次调用的握手与数据传输拆成异步两段、由框架在不同调度阶段分别触发，加上 CPU/GPU 异步，注解与内核呈稳定的错一位对应；确切机制需结合 DeepEP/DualPipe 源码（待确认）。对读者的教训：**不要按名字对号入座，要按 correlation 对账**。

另一个细节：该 launch（ts …478346）与内核实际执行（ts …483541）相隔 5.2ms，原因不是 GPU 空闲，而是 stream 27 内串行排队——前面的 `dispatch<false>` 直到 …482743 才结束，`cached_notify` 接力到 …483505，combine 随即开跑。同一 stream 内核首尾相接，是「流内串行、流间并行」的教科书式样本。

**(c) CPU 注解区间很短，别在它里面找重叠**。`combine(B)` 注解本身只有 147~626µs（CPU 墙钟，见 [train.json:L14430-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14430-L14436)），实测四个 `combine(B)` 区间内计算流上只有 0~2 个内核擦边而过；真正体现重叠的是 GPU 通信内核自己 6.5ms 的执行区间（见 (a)）。

#### 4.4.4 代码实践（本讲主实践）

实践目标：编写脚本统计 train.json 中名称含 `dpsk::ep::` 的 kernel 事件，按内核名聚合计数与总时长，计算占全部 GPU kernel 时间的比例；再取一个 `combine(B)` 注解区间，列出其中穿插的计算内核。

操作步骤（示例代码，保存为 `analyze_comm.py`）：

```python
import json, sys
from collections import defaultdict

trace = json.load(open(sys.argv[1]))
evs = trace["traceEvents"]
kernels = [e for e in evs if e.get("ph") == "X" and e.get("cat") == "kernel"]
total_dur = sum(e["dur"] for e in kernels)

agg = defaultdict(lambda: [0, 0])          # 简名 -> [次数, 总时长]
for e in kernels:
    if "dpsk::ep::" in e["name"]:           # 注意 4.4.5 练习 3 的陷阱
        key = e["name"].split("(")[0]
        agg[key][0] += 1; agg[key][1] += e["dur"]

comm = sum(v[1] for v in agg.values())
print(f"kernel 总数 {len(kernels)}, 总时长 {total_dur}us")
for k, (n, d) in sorted(agg.items(), key=lambda x: -x[1][1]):
    print(f"{n:3d} 次  {d:7d}us  {k}")
print(f"dpsk::ep:: 合计 {comm}us = {100*comm/total_dur:.1f}%")

# combine(B) 注解区间 → 其启动的通信内核 → 通信内核执行区间内的计算内核
ann = [e for e in evs if e.get("cat") == "user_annotation" and e.get("name") == "combine(B)"]
apis = [e for e in evs if e.get("cat") in ("cuda_runtime", "cuda_driver")]
a = sorted(ann, key=lambda e: e["ts"])[0]
lo, hi = a["ts"], a["ts"] + a["dur"]
launched = [k for k in kernels
            if any(p.get("args", {}).get("correlation") == k["args"]["correlation"]
                   and lo <= p["ts"] <= hi for p in apis)]
print(f"combine(B) [{lo},{hi}] 启动了:",
      *[k["name"].split("(")[0].split("::")[-1] for k in launched], sep="\n  ")
for k in launched:                          # 用内核自己的执行区间找重叠的计算内核
    klo, khi = k["ts"], k["ts"] + k["dur"]
    overlap = [c for c in kernels if c["tid"] != k["tid"]
               and c["ts"] < khi and c["ts"] + c["dur"] > klo]
    print(f"内核 {k['name'].split('(')[0].split('::')[-1]} 执行 {k['dur']}us 期间，"
          f"计算流上运行 {len(overlap)} 个内核，合计 {sum(c['dur'] for c in overlap)}us")
```

运行 `python3 analyze_comm.py train.json`。

需要观察的现象：聚合表的行数与数值；`combine(B)` 区间里启动的内核名是否与注解名一致；通信内核执行区间内计算内核的数量。

预期结果（笔者已验证）：`dpsk::ep::` 共 36 个、113440µs，占 963 个内核总时长 244041µs 的 **46.5%**；各内核族数值与 4.1.4/4.2 的表格一致。第一个 `combine(B)` 区间启动的是 `cached_notify` 与 `dispatch`（错位！）；第一个 dispatch/combine 内核执行期间计算流几乎不断流（combine ① 期间 17 个计算内核、8125µs）。

#### 4.4.5 小练习与答案

1. **练习**：通信流 busy 114.5ms，而 1F1B 注解窗口只有 112.2ms，通信怎么会「超过」窗口？
   **答案**：注解是 CPU 墙钟且窗口两端有启动/收尾的错位；GPU 侧尾部的通信内核晚于 CPU 注解结束才执行完，属异步执行的正常现象。
2. **练习**：通信内核只占约 15% 的 SM，这是不是说明它「不重要」？
   **答案**：恰恰相反。占用少是刻意设计——`grid` 只有 16~20 个 block，让通信与计算在同一 GPU 上共存（对照：解码阶段连这 15% 也省掉，见 [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)）；但它高达 46.5% 的 kernel 时长占比说明它是流水线的关键路径资源。
3. **练习（陷阱题）**：把脚本里的过滤条件换成子串匹配 `"dpsk::ep::" in name` 后直接跑 prefill.json，结果会混入什么？
   **答案**：prefill 中有 16 个 CUB 扫描内核（如 `at_cuda_detail::cdpsk::ep::DeviceScanKernel`，合计仅 23µs），其修饰名里恰好含 `cdpsk::ep::` 子串，会被误判成通信内核。更稳的过滤是 `name.startswith("void dpsk::ep::")` 或同时校验所在流（prefill 中真正的 DeepEP 内核共 580 个，全部在 stream 16）。

## 5. 综合实践

把本讲所有零散查询沉淀成一个可复用的小工具，并对两份轨迹各跑一次：

1. 扩展 4.4.4 的 `analyze_comm.py`：自动识别通信流（`dpsk::ep::` 内核所在的 tid），输出该流的内核数、busy 时长、占全部 kernel 时长的比例，以及五类内核族（dispatch / combine / notify_dispatch / cached_notify / get_dispatch_layout）的次数与时长表。
2. 对 `train.json` 与 `prefill.json` 各运行一次，制作对比表：通信流编号（train=27，prefill=16）、五类内核族计数 dispatch/combine/notify_dispatch/cached_notify/get_dispatch_layout（train 为 8/8/4/12/4，其中 dispatch 含两个模板实例各 4 次；prefill 为 116/116/116/116/116）、通信占比（train ≈46.5%，prefill ≈48.0%，笔者已验证）。
3. 用 3~5 句话总结你的发现。参考方向：两代模板参数的差异（train `<..., 8, ...>` 对 prefill `<..., 4, ...>`）、dispatch 与 combine 谁更耗时、握手开销占比，以及「训练里一半的 dispatch 复用布局、prefill 全部重算」（4.3）如何与路由均衡性联系起来。

## 6. 本讲小结

- DeepEP 的 internode 通信内核共四类：`dispatch`（token 发往专家）、`combine`（结果按原路聚合）、`notify_dispatch`/`cached_notify`（传输前的两种握手），外加本地布局内核 `get_dispatch_layout`；`dpsk` 是 DeepSeek 内核的命名空间前缀。
- train.json 中它们共 36 个、113.4ms，占全部 GPU kernel 时长的 **46.5%**，全部落在通信流 stream 27；每个 MoE 层留下一个「量化 → 握手 → 发送 → 布局 → 握手 → 发送 → 握手 → 聚合 → 握手 → 聚合」的十步循环，4 层重复 4 轮。
- 通信与计算真正并行：单个 6.5ms 的 combine 执行区间内，计算流上跑了 17 个内核（8.1ms），stream 7 利用率 80.6%；通信内核只用 16~20 个 block（约 15% SM），把计算单元让给同期的 GEMM。
- 注解名与内核名**错一位**对应（如 dispatch(B) 区间启动 combine 内核），必须用 `args.correlation` 反查；CPU 注解区间极短（百微秒级），重叠分析要基于 GPU 内核自己的执行区间。
- prefill.json 中同一内核族出现 580 次、占 48.0%，位于 stream 16；prefill 每次都重算布局（116 次 get_dispatch_layout 对 train 的 4 次），对应真实路由 vs 模拟均衡路由的差别。

## 7. 下一步学习建议

- 下一讲 **u2-l4（prefill.json 内核图谱）**：把本讲的通信内核放回预填充的完整计算图景中——DeepGEMM 的 `fp8_gemm_kernel`、grouped GEMM 家族、`clean_and_count_expert → get_fused_mapping → expand_to_fused → reduce_fused` 路由链与 MLA 注意力内核，理解双微批如何与 stream 16 上的这 580 个通信内核交错。
- 若想弄清 4.4.3 中错位配对的确切机制，建议阅读 [DeepEP](https://github.com/deepseek-ai/DeepEP) 的 internode dispatch/combine 实现与其异步握手接口，以及 [DualPipe](https://github.com/deepseek-ai/DualPipe) 的调度源码。
- 数量化兴趣的读者可提前翻到 **u3-l2（通信-计算重叠率与气泡量化）**：本讲的「17 个内核、80.6% 利用率」只是单点观测，u3-l2 会把它变成系统化的区间交/差运算。
