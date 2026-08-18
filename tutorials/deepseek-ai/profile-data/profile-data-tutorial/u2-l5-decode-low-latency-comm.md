# u2-l5 decode.json 解析：低延迟 dispatch_ll/combine_ll

## 1. 本讲目标

学完本讲，你应该能够：

1. 在轨迹中一眼认出 DeepEP 低延迟内核 `dpsk::ep::internode::dispatch_ll` 与 `combine_ll` 的完整签名，并说出它们的次数、典型时长与启动配置。
2. 用 `tid` / `args.stream` 字段验证内核所在的流，并解释一个**反直觉的实测事实**：decode 轨迹中通信内核与计算内核同在 stream 7，而不是像 train（stream 27）、prefill（stream 16）那样占用独立通信流。
3. 用「发送 → 计算填隙 → 等待」三段式时间线，解释 README 中「RDMA 消息发出后所有 SM 被释放、等计算结束再等通信完成」这句话在轨迹上长什么样。
4. 对比 normal（`dispatch`/`combine`）与 low-latency（`dispatch_ll`/`combine_ll`）两代内核在次数、时长、流布局、时间占比上的形态差异。
5. 顺带掌握一个解码轨迹特有的分析障碍：整条前向被一次 CUDA Graph 回放执行，3437 个内核共享同一个 correlation，u2-l1 的「反查 CPU 算子」方法在这里会失效。

## 2. 前置知识

本讲默认你已读过前置讲义 u2-l1（CPU-GPU 关联机制）与 u2-l3（DeepEP normal 通信内核）。相关概念快速回顾，并补充三个新概念。

**已学概念回顾：**

- **dispatch / combine**：MoE 专家并行的 all-to-all 两个方向。dispatch 把本 GPU 的 token 按路由结果发给各专家所在的 GPU；combine 把专家算完的结果原路收回并按路由权重加权求和（u2-l3）。
- **stream（流）**：GPU 上的任务队列，同一流内的内核严格串行，不同流可并行。PyTorch 轨迹里 kernel 事件的 `tid` 就是流号，`thread_name` 元数据把它显示为 "stream N"（u1-l4）。
- **correlation**：CUPTI 分配的外键，把 CPU 侧 CUDA API 调用与 GPU 侧内核一对一配对（u2-l1）。
- **RDMA**：远程直接内存访问，网卡不经对方 CPU 直接读写远端显存/内存，「数据在飞」的时间不消耗任何一方的算力。

**本讲新概念：**

| 术语 | 通俗解释 |
|---|---|
| SM | GPU 的流式多处理器（Streaming Multiprocessor），GPU 的"CPU 核"。H800 有 132 个 SM（u1-l3 已从 deviceProperties 读出）。内核占用的 block 数决定它用掉几个 SM。 |
| low-latency 内核 | DeepEP 面向解码（小 batch、极低延迟）设计的 dispatch/combine 实现，内核名以 `_ll` 结尾。与 normal 版相对。 |
| CUDA Graph（图捕获/回放） | 把一整段内核启动序列录成一张图，之后一次 `cudaGraphLaunch` 原样重放。解码每步做的计算几乎相同，非常适合图化。**副作用**：图内所有内核共享同一次 API 调用的 correlation。 |

一个需要建立的直觉：**「通信时间」不等于「通信内核时间」**。RDMA 网卡发出消息后，数据在网络上飞的这段时间里 GPU 可以完全不去管它——这正是「不占 SM」的含义。轨迹能记录的只有内核执行区间，网上的飞行时间只会表现为「两个通信内核之间的空隙」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) | 本讲主角。EP128 解码轨迹，19417 个事件。注意它是**单行压缩 JSON**（`wc -l` 为 0 行），所有永久链接只能锚定到 L1，必须用脚本解析。 |
| [README.md:L24-L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L24-L30) | Decoding 一节：EP128、TP1、4K prompt、每 GPU 128 请求、双微批重叠，以及「不占 SM」的关键描述。 |
| [assets/decode.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/decode.jpg) | 官方截图：GPU 0 的主流（stream 7）被密集内核几乎填满，另外两条 GPU 流（13/16）近乎空置。本讲脚本会量化这个画面。 |
| [train.json:L31324](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324) | 对照组。train.json 是多行格式，可精确锚定：L31324 是 normal 版 `dispatch<false, 8, true, 8>` 内核，[L35712](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712) 是 normal 版 `combine` 内核。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 低延迟内核本体**、**4.2 流布局的实测真相**、**4.3 释放 SM 的调度语义**。

### 4.1 模块一：dispatch_ll / combine_ll 低延迟内核

#### 4.1.1 概念说明

解码阶段的 MoE all-to-all 有两个特点：每次传输的数据量很小（每 GPU 只有 128 个请求、每请求 1 个新 token），但对延迟极其敏感。normal 版内核（u2-l3 精读过的 `dispatch`/`combine`/`notify_dispatch`）是为预填充/训练的大批量设计的：一个内核从头到尾负责「握手 + 发送 + 收齐」，一跑就是几毫秒，期间占着 SM 不放。

低延迟版把这件事拆开：**发送方把 RDMA 消息直接扔给网卡就退出，数据到没到，留给以后再等**。README 对此的原话（[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)）：

> the all-to-all communication during decoding does not occupy GPU SMs: after RDMA messages are issued, all GPU SMs are freed, and the system waits for the all-to-all communication to complete after the computation has finished.

在 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 中，这两个内核的完整签名是（截取自真实事件）：

```
void dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>(void*, float*, int*, long*, ...)
void dpsk::ep::internode::combine_ll<3, 10, 7168, 9>(void*, void*, int*, void*, ...)
```

识别要点：

- 命名空间 `dpsk::ep::internode::`（DeepSeek / Expert Parallelism / 跨节点），与 normal 版同族；
- 名字以 `_ll` 结尾（low-latency）；
- 模板参数里都有 `7168`——与 DeepSeek-V3 公开的隐藏维度一致，提示这是按 V3 形状实例化的内核；其余模板参数（`3`、`10`、`9`）的确切含义属于 DeepEP 实现细节，**待确认**，不建议凭猜测下结论。

另外注意一个「负结果」：decode.json 里**没有任何** normal 版 DeepEP 内核（没有 `dispatch<`、`combine<`、`notify_dispatch`、`cached_notify`、`get_dispatch_layout`）。解码整份轨迹只认 `_ll` 这一代。

#### 4.1.2 核心流程

一次 MoE 层的 dispatch 调用在 GPU 时间线上呈「一短一长」两个 `dispatch_ll` 内核（下标为 ts 后 6 位，便于对表）：

```text
──▶ dispatch_ll  dur≈17µs   ──▶  （此处插入约 100µs 的计算内核） ──▶ dispatch_ll  dur≈60~80µs ──▶ 消费收到的 token 的专家 GEMM
     打包 + 发出 RDMA              数据在网络上飞，GPU 不参与            等待各 rank 数据到齐
     发完即退出，SM 释放                                              （此时计算已经做完，才轮到等）
```

combine 方向同理，但两段都短（16~23µs）：把结果按源端发回去、再做收尾。

关于「一短一长」的分工（短的对应发出 RDMA、长的对应等待接收），是从时长双峰分布和「长内核紧邻消费数据的专家 GEMM」这一时序关系**推断**的；内核内部各阶段的确切命名请以 [DeepEP 仓库](https://github.com/deepseek-ai/DeepEP) 源码为准。

#### 4.1.3 源码精读

以下数字全部可以用 4.1.4 的脚本在 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 上复现。

**总量与时长分布：**

| 内核 | 次数 | 总时长 | 典型时长 | 离群值 |
|---|---|---|---|---|
| `dispatch_ll` | 235 | 11921 µs | 双峰：117 个落在 17~19 µs；115 个落在 37~86 µs | 134、202、**1603** µs |
| `combine_ll` | 235 | 5138 µs | 232 个落在 16~23 µs | 154、356、362 µs |

三个值得注意的细节：

1. **双峰即分工**。`dispatch_ll` 的时长分成清晰的两群（~17µs 与 ~60-80µs），对应 4.1.2 的「发送 / 等待」两段；`combine_ll` 两段都短，分布单峰。
2. **首个 `dispatch_ll` 长达 1603 µs**，是全轨迹最长的 ll 内核——出现在图回放的最开始，符合「第一次通信要付冷启动代价（连接/缓存未热）」的直觉；它之后的所有发送段都回到 17µs。
3. **235 与 235 严格相等**。图回放内各有 232 个，按「每层每微批一次 dispatch、一次 combine」折算是 232 = 2 微批 × 58 层的调用数，每两次调用对应 4 个内核——次数即结构指纹的又一例（层/微批的完整推导见下一讲 u2-l6）。

**启动配置（从 args 读出，两个内核完全相同）：**

```json
"registers per thread": 64, "shared memory": 36,
"blocks per SM": 0.6515151, "grid": [86, 1, 1], "block": [960, 1, 1],
"est. achieved occupancy %": 31
```

86 个 block 摊在 132 个 SM 上（86/132 ≈ 0.65），占用率约 31%——通信内核本来就不是算力大户，它只负责搬运与等待。DeepEP 公开接口允许为低延迟内核配置参与通信的 SM 数量，这个 grid 就是该配置落地的样子（86 这一具体数值的来历**待确认**）。

#### 4.1.4 代码实践

**实践目标**：亲手从 decode.json 里把 ll 内核挖出来，复现上表的每个数字。

**操作步骤**：

最省事的方式是 `jq`（decode.json 单行压缩，jq 可直接处理）：

```bash
# 1. dispatch_ll 的次数 / 总时长 / 最大值
jq -r '[.traceEvents[] | select(.cat=="kernel" and (.name|contains("dispatch_ll"))) | .dur]
       | "n=\(length) sum=\(add) max=\(max)"' decode.json

# 2. 全部 ll 内核的时长直方图（升序去重计数）
jq -r '.traceEvents[] | select(.cat=="kernel" and (.name|contains("_ll<"))) | .dur' decode.json \
  | sort -n | uniq -c
```

也可以用等价的 Python（本讲作者的环境只验证了 jq 版本，Python 脚本请本地运行复现）：

```python
import json, collections
trace = json.load(open("decode.json"))
events = trace["traceEvents"]

ll = [e for e in events if e.get("cat") == "kernel" and "_ll<" in e["name"]]
print("ll 内核总数:", len(ll))                              # 预期 470
print("按流分布:", collections.Counter(e["tid"] for e in ll))  # 预期 {7: 468, 16: 2}

for key in ("dispatch_ll", "combine_ll"):
    d = sorted(e["dur"] for e in ll if key in e["name"])
    print(f"{key}: n={len(d)} 中位={d[len(d)//2]}µs 最大={d[-1]}µs")
```

**需要观察的现象**：`dispatch_ll` 的直方图在 17µs 与 60~80µs 两处明显扎堆；`combine_ll` 几乎全部挤在 16~23µs。

**预期结果**：`n=235 sum=11921 max=1603`（dispatch_ll）；`n=235 sum=5138 max=362`（combine_ll）；ll 内核 470 个里 468 个在 tid=7。若你的数字一致，说明提取口径正确。

#### 4.1.5 小练习与答案

**练习 1**：不看本讲，如何用一条命令确认 decode.json 里是否存在 normal 版 DeepEP 内核？

答案：`jq -r '.traceEvents[] | select(.cat=="kernel") | .name' decode.json | grep -c "dpsk::ep::internode::dispatch<"`，结果为 0；把 `_ll` 去掉再数 `dispatch_ll` 则为 235。也可以直接数 `dpsk::ep::` 前缀总数 = 470，全部是 `_ll` 家族。

**练习 2**：`dispatch_ll` 为什么会长出 1603 µs 这样的离群值，而且只出现一次？

答案：它发生在图回放内的第一次 dispatch，冷启动路径（连接状态、路由缓存等未热）需要额外开销；此后同签名内核都回到 17µs 量级。「只出现一次 + 位置在最前」是判断冷启动的典型证据。

**练习 3**：`combine_ll` 总时长（5138 µs）明显小于 `dispatch_ll`（11921 µs），结合 4.1.2 的两段式模型猜一下原因。

答案：dispatch 的「等待段」要等齐所有对端 rank 发来的 token（受最慢者制约，60~80µs），而 combine 方向的两段都只做发出与收尾（16~23µs），没有等价的长等待段；所以 dispatch 侧总时长更大。

### 4.2 模块二：通信流与计算流的分离——decode 给出的真实答案

#### 4.2.1 概念说明

前两讲建立了一个印象：**通信内核住在独立通信流里**——train.json 的 DeepEP 内核全在 stream 27，prefill.json 的在 stream 16，与计算流（stream 7）分开，这样两边的内核才能在时间上并行（u2-l3、u2-l4）。

顺着这个印象，很容易对 decode 做出同样的预期：ll 内核应该在某条独立通信流上。**但实测不是**。这是本讲最重要的一个教训：预期要拿数据验证。decode 的通信内核和计算内核同在 stream 7，分离的不是「流」，而是「内核执行」与「数据在飞」这两件事——后者根本不占 GPU。

为什么 decode 敢把通信放回主流？因为低延迟内核发送段只有 ~17µs、等待段被刻意排在计算之后（4.3 详述），即使与计算同流串行，也几乎不挡路。而 normal 内核一跑几毫秒，必须挪去独立流与计算并行。

#### 4.2.2 核心流程

流布局的实测全景（3647 个内核）：

```text
stream 7   ████████████████████████████████████████  3642 个内核（计算 + 全部 ll 通信）
stream 13  ▏                                         3 个（ncclDevKernel_AllReduce ×3）
stream 16  ▏                                         2 个（收尾阶段的 2 个 eager ll 内核）
```

再叠加 CUDA Graph 这一层：

1. 轨迹里恰好有 **1 次** `cudaGraphLaunch`（ts=1742522672417026，API 本身耗时 4071 µs，含首次上传图的开销）。
2. 它启动了 **3437 个内核（占 94.3%）**，correlation 全部等于 7077，全部在 stream 7，从 ts=1742522672417085 持续到最后一个图内内核（ts=1742522672505548），约 88.5 ms——这就是一个完整解码步的前向。
3. 剩下 **210 个 eager 内核**分两段：图开始前 8 个（含 stream 13 上前 2 个 NCCL AllReduce 热身），图结束后约 5.6 ms 内 202 个（下一个解码步的开端，被采集窗口截断；其中含 6 个 eager ll 内核，2 个落在 stream 16 上——这就是 stream 16 那两个内核的来历）。

对分析的直接影响：u2-l1 的方法是用 correlation 把 GPU 内核反查回 CPU 侧 `cuda_runtime` 事件再套 `cpu_op`。在 decode.json 里这条链对图内内核会**全部坍缩到同一个 `cudaGraphLaunch`**，拿不到逐内核的算子层级。所以解码轨迹要像 u2-l4 一样直接从 GPU 内核序列读结构。

#### 4.2.3 源码精读

- 元数据事件把轨道命名清楚（可从 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 解出）：`process_labels` 把 pid 0 标为 `GPU 0`；`thread_name` 在 pid 0 下只登记了三条轨道——`stream 7`、`stream 13`、`stream 16`（注意名字尾随空格，脚本匹配要 strip，u1-l4 踩过的坑）。CPU 进程是 pid 494。
- 对照组 train.json 是多行格式，可以精确锚定 normal 内核的流号：[train.json:L31324](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324) 的 `dispatch<false, 8, true, 8>` 与 [train.json:L35712](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712) 的 `combine<false, 8, __nv_bfloat16, ...>`，两者的 `"tid": 27` —— 36 个 DeepEP 内核无一例外。
- decode.json 的 `user_annotation` 只有 3 条 `nccl:all_reduce`（对应 stream 13 的 3 个 NCCL 内核：热身 2 个 + 收尾 1 个），没有 `ProfilerStep`、`1F1B` 之类的框架调度注解——解码结构只能从 GPU 侧读。

#### 4.2.4 代码实践

**实践目标**：用两条命令验证「ll 内核在 stream 7」与「一次图回放占了 94% 的内核」。

**操作步骤**：

```bash
# 1. 每条流上的内核数
jq -r '.traceEvents[] | select(.cat=="kernel") | .tid' decode.json | sort -n | uniq -c
# 预期：3642 个 tid=7；3 个 tid=13；2 个 tid=16

# 2. correlation 分布（图内内核共享同一 id）
jq -r '.traceEvents[] | select(.cat=="kernel") | .args.correlation' decode.json \
  | sort -n | uniq -c | sort -rn | head -3
# 预期：7077 出现 3437 次，其余每个 id 至多 1~2 次

# 3. 找到那次图启动 API 本身
jq -r '.traceEvents[] | select(.cat=="cuda_runtime" and .name=="cudaGraphLaunch")' decode.json
# 预期：恰好 1 条，ts=1742522672417026，dur=4071
```

**需要观察的现象**：ll 内核的 tid 与 fp8_gemm 等计算内核的 tid 完全相同（都是 7）；`cudaGraphLaunch` 只有 1 条。

**预期结果**：与上面三条注释一致。若你想进一步确认「流分离」在 train.json 里确实存在，把第 1 条命令里的文件换成 train.json，会看到 DeepEP 内核集中在 tid=27——两份轨迹并排一跑，4.2.1 的对比就落地了。

#### 4.2.5 小练习与答案

**练习 1**：为什么图回放内 3437 个内核共享 correlation 7077？

答案：correlation 由 CUPTI 在**每次 CUDA API 调用**时分配，用来配对「这次 API 调用」与「它启动的 GPU 活动」。图回放只有一次 `cudaGraphLaunch`，图内全部内核都由这一次调用产生，因此共享同一个 correlation。

**练习 2**：既然 ll 内核与计算内核同流串行，「通信与计算重叠」在 decode 里还成立吗？

答案：成立，但重叠的对象变了。重叠的不是两条流上的内核，而是「RDMA 数据在网络上飞的时间」与「同流上后续计算内核的执行时间」。发送内核退出后，流继续跑计算；等计算做完，排在后面的等待内核才去收数据。可见的证据就是 4.3 要量的「发送与等待之间的计算填隙」。

**练习 3**：stream 13 上恰好 3 个 NCCL AllReduce 内核，它们是 DeepEP 家族吗？

答案：不是。名字是 `ncclDevKernel_AllReduce_Sum_u64/u32_TREE_LL(...)`，属于 NCCL 集合通信库（这里 `_LL` 指 NCCL 的 low-latency 协议算法，与 DeepEP 的 `_ll` 内核只是撞了缩写）。它们只有 3 个，位于热身与收尾，不参与 MoE all-to-all 主链路。

### 4.3 模块三：RDMA 发出后释放 SM 的调度语义

#### 4.3.1 概念说明

「不占 SM」容易被误读成「通信内核不占用 SM」。更准确的说法分三段：

1. **发送段**（~17µs）：`dispatch_ll` 打包数据、向网卡发出 RDMA 写请求，然后退出。此后到数据送达为止，**没有任何内核在替通信工作**，全部 132 个 SM 都可以跑计算——这就是 README 说的 "all GPU SMs are freed"。
2. **飞行段**（约 100µs 量级）：数据在网络上。GPU 这边同一条流接着执行**另一个微批**的计算内核。解码和预填充一样用两个微批（[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)），微批 B 的计算正是用来填微批 A 的通信缝隙的。
3. **等待段**（~60-80µs）：计算做完，排在后面的第二个 `dispatch_ll` 内核去等各 rank 数据到齐，等齐后紧跟着的专家 GEMM 立刻消费——"waits for the all-to-all communication to complete after the computation has finished"。

#### 4.3.2 核心流程

从 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 提取的一段真实窗口（ts 后 6 位 423753 → 423942，全部在 stream 7）：

```text
ts=423753  dispatch_ll      dur=17   ← 发送：RDMA 消息发出，内核即刻退出
ts=423771  per_token_cast_to_fp8     ┐
ts=423774  fp8_gemm 4096×7168       │
ts=423792  swiglu                   │
ts=423796  fp8_gemm 7168×2048       │ 11 个计算内核
ts=423807  _layer_norm              │ （约 105µs，
ts=423811  fp8_gemm 2112×7168       │  与数据飞行重叠）
ts=423825  _layer_norm              │
ts=423827  per_token_cast_to_fp8    │
ts=423830  fp8_gemm 24576×1536      │
ts=423849  cutlass bf16 gemm        │
ts=423866  rotary_embedding         ┘
ts=423875  dispatch_ll      dur=67   ← 等待：各 rank 数据到齐（计算已做完）
ts=423942  fp8_gemm GemmType::2     ← 紧接着消费收到的 token
```

发送与等待之间的间隙：

\[ g = ts_{\text{等待}} - \left(ts_{\text{发送}} + dur_{\text{发送}}\right) = 423875 - (423753 + 17) = 105\,\mu s \]

这 105µs 就是「数据在飞、SM 在算」的重叠窗口。

时间占比的量化口径：

\[ p_{ll} = \frac{\sum_{e \in ll} dur_e}{\sum_{e \in kernel} dur_e} = \frac{11921 + 5138}{89786} \approx 19.0\% \]

三份轨迹同口径对比（通信内核时间 / 全部 GPU 内核时间）：

| 轨迹 | 通信内核 | 所在流 | 个数 | 总时长 | 占全部内核时长 | 单个典型时长 |
|---|---|---|---|---|---|---|
| train（EP64） | DeepEP normal 族 | stream 27 | 36 | 115.0 ms | **47.1%** | dispatch 3.1~4.6 ms；combine 5.8~6.5 ms |
| prefill（EP32） | DeepEP normal 族 | stream 16 为主 | 596 | 1799.5 ms | **48.0%** | 毫秒级 |
| decode（EP128） | DeepEP low-latency 族 | **stream 7（与计算同流）** | 470 | 17.1 ms | **19.0%** | 17~80 µs |

（train/prefill 的统计口径为「名字含 `dpsk::ep::` 的内核」，与 u2-l3/u2-l4 一致。）

#### 4.3.3 源码精读

- 上表每一格都可复现：decode 侧总内核时长 89786 µs（3647 个内核，span 95889 µs，其中 stream 7 占 88716 µs，约 98.8%——这条流几乎没闲着）；train 侧 36 个 DeepEP 内核合计 115040 µs / 总 244041 µs（normal `dispatch` 的 8 次实测 3113~4604 µs、`combine` 8 次 5847~6549 µs，见 [train.json:L31324](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324) 与 [train.json:L35712](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712)）。
- 19.0% 对 47~48%：两代内核的形态差异一目了然——normal 版把「等所有人」做进同一个内核里（毫秒级、近半 GPU 时间）；low-latency 版把「等」切出来后移，通信内核只剩零头。
- 还剩的 19% 里大头其实是 470 个内核的「等待段」与图内首次 1603 µs 冷启动；纯发送段（470 个中约 232 个短内核）合计只有约 4 ms。
- 注意口径提醒：19% 是「通信**内核**时长」占比，不是「通信**行为**」占比——真正的通信还包括不占 SM 的飞行时间。要量化后者，需要 u3-l2 的区间求交方法。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的单个窗口推广为全量统计——数出每一次「发送 → 等待」之间塞了多少计算。

**操作步骤**（Python，本地运行；单个窗口的数字已用 jq 验证，全量平均值待本地验证）：

```python
import json

trace = json.load(open("decode.json"))
ks = [e for e in trace["traceEvents"]
      if e.get("cat") == "kernel" and e.get("tid") == 7
      and e.get("args", {}).get("correlation") == 7077]      # 只要图回放内的内核
ks.sort(key=lambda e: e["ts"])

ll_pos = [i for i, e in enumerate(ks) if "_ll<" in e["name"]]
samples = []
for i, e in enumerate(ks):
    if "dispatch_ll" in e["name"] and e["dur"] <= 25:         # 短内核 ≈ 发送段
        later = [j for j in ll_pos if j > i]
        if not later:
            continue
        j = later[0]                                          # 下一个 ll 内核 ≈ 等待段
        between = ks[i + 1:j]
        gap = ks[j]["ts"] - (e["ts"] + e["dur"])
        samples.append((len(between), sum(x["dur"] for x in between), gap, ks[j]["dur"]))

print("样本数:", len(samples))                       # 预期约 116（2 微批 × 58 层）
print("平均插入内核数:", sum(s[0] for s in samples) / len(samples))
print("平均计算填隙时长(µs):", sum(s[2] for s in samples) / len(samples))
print("等待段时长分布: min={}, max={}".format(min(s[3] for s in samples),
                                          max(s[3] for s in samples)))
```

**需要观察的现象**：几乎每个短 `dispatch_ll` 之后、下一个 ll 内核之前，都夹着一串计算内核（DeepGEMM 的 fp8_gemm、swiglu、layer_norm、rotary 等）。

**预期结果**：单个窗口实测为 11 个内核 / 105µs 间隙 / 等待段 67µs；全量平均应在「十来个内核、百微秒量级」附近。若某些样本间隙接近 0，多半是微批交错模式在该层不同，记下来，u2-l6 讲双微批结构时会解释。

#### 4.3.5 小练习与答案

**练习 1**：既然等待段（60~80µs）也占着 SM 在轮询，为什么 README 还说「不占 SM」？

答案：「不占」指的是**消息发出之后到等待开始之前**这段飞行时间——期间没有任何通信内核在跑，SM 全部让给计算。等待段本身确实是内核、确实占 SM，但它被调度到计算之后才发生，对应 README 的后半句「等计算结束后再等通信完成」。精确的说法是：不占 SM 的是通信的飞行时间，而不是所有 ll 内核。

**练习 2**：把 decode 的 19.0% 和 train 的 47.1% 直接相比，公平吗？列出至少一个口径差异。

答案：不完全公平。至少三点：(1) 场景不同（解码每 GPU 仅 128 token，传输量天然小）；(2) normal 版把等待做进内核、low-latency 版把等待后移，同样的等待时间前者计入通信内核、后者部分体现在被拉长的时间线上；(3) decode 的飞行时间根本不出现在任何内核时长里。要公平比较「通信开销」应该用 u3-l2 的重叠率/气泡口径，而不是内核时长占比。

**练习 3**：等待段 67µs 比发送段 17µs 长，说明什么？

答案：等待段受「最慢对端」制约——它要等 EP128 里所有相关 rank 的数据到齐，时长接近一次跨节点 all-to-all 的实际时延尾部；而发送段只是把消息推给本机网卡，做完即走。两者之差（约 50µs）大致就是网络传输与对端发送的耗时。

## 5. 综合实践

把本讲三个模块串成一个可复用的小工具 `ll_vs_normal.py`：输入任意一份轨迹 JSON，输出它的 DeepEP 通信内核画像，并对比两代内核。

```python
# ll_vs_normal.py —— 用法: python3 ll_vs_normal.py decode.json train.json [prefill.json ...]
import json, sys, collections

def profile(path):
    trace = json.load(open(path))
    ks = [e for e in trace["traceEvents"] if e.get("cat") == "kernel"]
    comm = [e for e in ks if "dpsk::ep::" in e["name"]]
    total = sum(e["dur"] for e in ks)
    by_name = collections.defaultdict(list)
    for e in comm:
        by_name[e["name"].split("<")[0].split("(")[0]].append(e)
    print(f"\n=== {path} (world_size={trace['distributedInfo']['world_size']}) ===")
    print(f"kernel 总数 {len(ks)}，总时长 {total} µs；DeepEP 内核 {len(comm)} 个，"
          f"占 {sum(e['dur'] for e in comm) / total:.1%}；"
          f"流分布 {dict(collections.Counter(e['tid'] for e in comm))}")
    for name, evs in sorted(by_name.items(), key=lambda kv: -sum(e['dur'] for e in kv[1])):
        durs = sorted(e['dur'] for e in evs)
        print(f"  {name.split('::')[-1]:<22} n={len(evs):>4}  "
              f"总={sum(durs):>7}µs  中位={durs[len(durs)//2]}µs  max={durs[-1]}µs")

for p in sys.argv[1:]:
    profile(p)
```

任务：

1. 对 `decode.json` 与 `train.json` 各运行一次（可加上 `prefill.json`）。
2. 核对输出与本讲给出的表：decode 侧应为 470 个 ll 内核、约 19.0%、流分布 `{7: 468, 16: 2}`；train 侧应为 36 个、约 47.1%、全在流 27，`dispatch`/`combine` 中位时长在毫秒级。
3. 用 3~5 句话写下你的结论，建议覆盖：两代内核单个时长的量级差（µs 对 ms，约两个数量级）、流布局的差异（同流对独立通信流）、时间占比的差异（19% 对 ~48%）以及这个差异里「口径」贡献了多少。
4. 进阶（可选）：把 4.3.4 的间隙分析并进 `profile()`，给每份轨迹补一列「平均计算填隙时长」，为 u3-l2 的重叠率计算做好数据准备。

注意：脚本读入的是仓库根目录的轨迹文件；本讲作者已用等价的 jq 命令验证过全部引用数字，Python 版输出如有细微差异，优先检查你的过滤条件（`cat=="kernel"`、名字包含 `dpsk::ep::`）是否与上文一致。

## 6. 本讲小结

- decode.json（EP128、每 GPU 128 请求）的 MoE all-to-all 全部由低延迟内核完成：`dispatch_ll<true, 3, 10, 7168>` 与 `combine_ll<3, 10, 7168, 9>` 各 235 次，合计 17.1 ms，占 GPU 内核总时长的 19.0%——没有任何 normal 版 DeepEP 内核。
- **实测推翻了「独立通信流」的预期**：ll 内核与计算内核同在 stream 7（stream 13/16 仅 5 个杂项内核）。通信与计算的重叠靠的是「RDMA 飞行时间不占 SM」，而不是两条流的并行。
- 时间线呈三段式：发送段 ~17µs（发出即退出，SM 释放）→ 计算填隙 ~105µs（另一微批的计算内核，实测窗口插入 11 个）→ 等待段 ~60-80µs（等齐各 rank，紧接消费数据的专家 GEMM）。`dispatch_ll` 时长的双峰分布正是这两段的指纹。
- 解码轨迹由**一次 CUDA Graph 回放**执行：3437 个内核（94.3%）共享 correlation 7077，u2-l1 的逐内核反查 CPU 算子方法在此失效；另有 210 个 eager 内核分布在图前热身与图后收尾。
- 与 normal 版对比：train 的 36 个通信内核全在独立流 27、单内核毫秒级、占 47.1%；两代设计把「等待」放在内核内还是内核外，是全部形态差异的根源。

## 7. 下一步学习建议

- **下一讲 u2-l6**：解码的另一半故事——`flash_fwd_splitkv_mla_kernel` 两段式注意力、双微批在时间线上的交替证据，以及用 235/235、120 等次数指纹反推层数与微批数。本讲 4.3.4 遗留的「个别间隙接近 0」的样本会在那里得到解释。
- **延伸阅读**：[DeepEP 仓库](https://github.com/deepseek-ai/DeepEP) 的 low-latency 实现（README 与 `internode_ll` 相关源码），对照本讲推断的「发送/等待」两段式分工；[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) 也明确把 all-to-all 实现指向了它。
- **通往定量分析**：u3-l2 会把本讲的「间隙」升级为严格的区间求交，计算通信-计算重叠率与气泡指标——届时「19.0% 是内核口径、不是通信行为口径」这一提醒会成为关键前提。
