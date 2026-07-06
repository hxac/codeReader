# DeepEP 是什么：专家并行通信库的整体定位

## 1. 本讲目标

本讲是 DeepEP 学习手册的第一篇，目标是让读者在不动手编译、不读 CUDA 代码的前提下，建立一个清晰的"项目全景图"。读完本讲，你应当能够：

- 用一句话说清 DeepEP 是什么、解决什么问题。
- 理解 **专家并行（Expert Parallelism, EP）** 在 MoE 模型中的作用，以及 **dispatch / combine** 两种通信的方向与含义。
- 区分 **NVLink（节点内）** 与 **RDMA（节点间）** 两类物理链路。
- 说出 DeepEP **V2** 相对 V1 的几个关键变化：NCCL Gin 后端、全 JIT 编译、解析式 SM/QP 计算、统一 `ElasticBuffer` 接口。
- 读懂 README 性能表，并解释什么是"逻辑带宽（logical bandwidth）"。

本讲几乎不涉及 CUDA 内核细节，所有结论都来自项目根目录的 `README.md`。后续讲义才会逐步深入到 C++/CUDA 源码。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先建立直觉会顺很多。

- **GPU / SM**：GPU 由很多个流多处理器（Streaming Multiprocessor, SM）组成。可以把 SM 粗略理解为 GPU 上的"计算核心"。DeepEP 经常强调"只占用很少的 SM"，意思是通信只抢走极少的计算核心，把大部分核心留给模型计算。
- **MoE（Mixture of Experts，混合专家）**：一种模型结构。普通模型里每个 token 都会激活所有参数；MoE 模型里有多个"专家（expert）"子网络，每个 token 只被路由到其中少数几个专家。DeepSeek-V3 等模型就是 MoE。
- **分布式训练 / rank**：多卡训练时，每张 GPU（或每个进程）被称为一个 **rank**，用一个从 0 开始的整数编号。
- **all-to-all 通信**：一种通信模式，每个 rank 都要给其他所有 rank 发数据，也要从所有 rank 收数据。MoE 的 dispatch/combine 本质上就是一种 all-to-all。
- **带宽（bandwidth）**：单位时间能传输的数据量，常用 GB/s 衡量。带宽越高，同样多的数据传得越快。
- **JIT（Just-In-Time）**：运行时编译。和"安装时一次性编译好"相对，JIT 是在程序运行起来后再按需编译代码。

> 不熟悉以上名词也没关系，本讲会在用到的地方再解释一遍。

## 3. 本讲源码地图

本讲的"源码"其实就是一份文档：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目总说明。包含项目定位、V2 新特性、性能数据、安装方式、使用示例、环境变量、网络配置等。本讲所有结论都从这里来。 |

为了让你对整个仓库先有个"森林"印象（本讲不会深入，只是让你知道地图上有这些树）：

- `deep_ep/`：Python 接口层，用户主要打交道的地方（如 `buffers/elastic.py`）。
- `csrc/`：C++ / CUDA 源码，包含 `elastic`（V2 实现）、`legacy`（V1 实现）、`jit`（运行时编译子系统）、`kernels`（内核与后端）。
- `deep_ep/include/deep_ep/`：被 JIT 编译的 CUDA 内核头文件（`.cuh`）。
- `tests/`：测试与基准脚本，例如 `tests/elastic/test_ep.py`。
- `docs/`：补充文档，如 `docs/legacy.md`（V1 文档）、`docs/nvshmem.md`（NVSHMEM 安装）。

这张地图的细节会在后续讲义（尤其是 u1-l2「目录结构」）展开。

## 4. 核心概念与源码讲解

### 4.1 DeepEP 的定位：一个面向 MoE 的通信库

#### 4.1.1 概念说明

DeepEP 的全称是 **DeepEveryParallel**。README 第一段就给出了它的定位：

> DeepEP (DeepEveryParallel) is a high-performance communication library for modern machine learning training and inference. The library currently focuses on expert parallelism (EP) ...

把它拆成三句话理解：

1. 它是一个 **通信库（communication library）**，不是模型库、不是训练框架。它只负责"在多张 GPU 之间高效搬数据"这一件事。
2. 它服务的场景是 **机器学习的训练与推理**，尤其是 MoE 模型。
3. 它当前的核心功能是 **专家并行（EP）** 所需的 all-to-all 通信（MoE 的 dispatch 与 combine），并提供 FP8 等低精度支持。

除了 EP，它还提供几个实验性的通信原语：流水线并行（PP）、上下文并行（CP）、远程内存访问（Engram）。这些功能都追求一个共同目标：**零或极少的 SM 占用**，让通信几乎不抢模型的算力。

#### 4.1.2 核心流程

DeepEP 在一次 MoE 前向中扮演的角色，可以用下面这条流水线表示：

```text
[各 rank 上的输入 token]
        │  buffer.dispatch(...)        ← DeepEP 负责：把 token 搬到对应专家所在的 rank
        ▼
[每个 rank 收到"发给本 rank 专家"的 token]
        │  专家网络计算（GEMM 等）       ← 模型自己负责，DeepEP 不参与
        ▼
[每个 rank 拿到专家输出]
        │  buffer.combine(...)         ← DeepEP 负责：把输出搬回原 rank 并加权归约
        ▼
[各 rank 上还原成原始顺序的输出]
```

也就是说，DeepEP 把"跨 rank 搬 token"这件事做到了接近硬件带宽极限，并且几乎不占 SM。

#### 4.1.3 源码精读

项目第一句话直接点题：

定位与核心功能（README 第 3 行）：

[README.md:L3-L3](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L3-L3) —— 说明 DeepEP 是面向训练与推理的高性能通信库，当前聚焦专家并行（EP），提供高吞吐、低延迟的 MoE dispatch/combine all-to-all 内核，支持 FP8 等低精度，并提供 PP/CP/Engram 实验性原语，全部追求零或极少的 SM 占用。

紧接着一句强调它的性能目标（README 第 5 行）：

[README.md:L5-L5](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L5-L5) —— 尽管设计轻量，DeepEP 在多种配置下的性能都能匹配甚至超过硬件带宽极限。

#### 4.1.4 代码实践

1. **实践目标**：用自己的话复述 DeepEP 的定位。
2. **操作步骤**：
   - 打开 `README.md`，只读第 1～6 行。
   - 用一句话回答三个问题：① 它是什么类型的库？② 服务什么场景？③ 当前最核心的功能是什么？
3. **需要观察的现象**：你会注意到 README 把"通信库"和"EP/dispatch/combine"放在最显眼的位置，而把 PP/CP/Engram 标注为 experimental。
4. **预期结果**：你能写出类似"DeepEP 是一个面向 MoE 训练/推理的高性能通信库，核心是 EP 所需的 dispatch/combine all-to-all 内核"这样的概括。
5. 本实践的结论**待本地确认**与否无关，属阅读理解型实践。

#### 4.1.5 小练习与答案

**练习 1**：DeepEP 是训练框架吗？为什么？

> **参考答案**：不是。它是一个通信库，只负责多 GPU 之间高效搬运数据，不负责模型结构定义、优化器、loss 计算等训练框架的职责。

**练习 2**：README 里提到的 PP / CP / Engram 处于什么状态？

> **参考答案**：它们是 experimental（实验性）特性，不是核心稳定功能；核心稳定功能是 EP（dispatch/combine）。

---

### 4.2 专家并行（EP）与 MoE：dispatch 与 combine

#### 4.2.1 概念说明

先理解 **MoE**：模型里有很多"专家"子网络，gate（路由器）会为每个 token 选出少数几个专家（例如 top-8）来处理它。这样每个 token 只激活模型的一部分参数，能在扩大参数规模的同时控制计算量。

再理解 **专家并行（EP）**：分布式训练时，最简单的并行是把一份模型复制到每张卡（数据并行 DP）；而 EP 是把 **不同的专家放到不同的 rank 上**。比如 64 个专家、8 张卡，每张卡常驻 8 个专家。

这样一来立刻产生一个通信需求：

- **dispatch（派发）**：每个 token 算出它要去哪些专家后，要被发送到"持有这些专家的 rank"上。这是前向计算开始前的 all-to-all。
- **combine（合并）**：专家算完后，输出要被送回 token 原来所在的 rank，并按 top-k 权重加权求和，还原成原来的顺序。这是前向计算结束后的 all-to-all。

可以记一句话：**dispatch 是"把 token 送出去找专家"，combine 是"把专家结果收回来求和"。**

#### 4.2.2 核心流程

一次 MoE 前向里，DeepEP 参与的两步如下：

```text
# x: 本 rank 的输入 token；topk_idx: 每个 token 选中的专家编号；topk_weights: 对应权重
recv_x, recv_topk_idx, recv_topk_weights, handle, event = buffer.dispatch(
    x, topk_idx=topk_idx, topk_weights=topk_weights, ...)

# ① 在这里可以做与通信无关的计算，与 dispatch 重叠（event 控制同步）
event.current_stream_wait()          # 等通信完成
out = experts(recv_x)                # ② 本地专家计算（模型负责）

combined_x, _, event = buffer.combine(out, handle=handle, ...)   # ③ 把结果送回并归约
```

`dispatch` 返回的 `handle` 是一个 **EPHandle**，里面装着路由元数据（谁发给谁、各专家收到多少 token 等），combine 必须靠它才能把数据"原路送回"。它就是 dispatch 与 combine 之间的"接线图"。

#### 4.2.3 源码精读

README 在「Example use in model training or inference prefilling」一节给出了完整示例：

dispatch 接口与 handle 的说明（README 第 190～204 行）：

[README.md:L190-L204](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L190-L204) —— 调用 `_buffer.dispatch(...)` 得到 `recv_x / recv_topk_idx / recv_topk_weights / handle / event`；注释指出 `handle` 装载了供 combine 使用的路由元数据，`handle.num_recv_tokens_per_expert_list` 提供给 GEMM 用的每专家接收 token 数。

combine 接口（README 第 224～236 行）：

[README.md:L224-L236](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L224-L236) —— `combine_forward` 调用 `_buffer.combine(x, handle=handle, ...)`，把专家输出按 handle 还原、加权归约回原 rank。

README 还点出一个对称关系（反向与正向互补）：

[README.md:L209-L221](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L209-L221) —— dispatch 的反向其实就是 combine；combine 的反向其实就是 dispatch。

#### 4.2.4 代码实践

1. **实践目标**：建立"dispatch → 专家计算 → combine"的调用顺序直觉。
2. **操作步骤**：在 README 中定位 4.2.3 引用的三段代码（dispatch、combine、backward），用箭头画出三者的依赖关系。
3. **需要观察的现象**：观察 `handle` 变量在 dispatch 里产生、在 combine 与 backward 里被复用。
4. **预期结果**：你会得到 `dispatch(handle 出) → combine(handle 入)` 与 `combine(handle 出) → dispatch(handle 入)` 这对对称关系。
5. 这是源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：dispatch 和 combine 各自的数据流向是什么？

> **参考答案**：dispatch 把每个 token 从其原始 rank 发送到持有目标专家的 rank（fan-out 的 all-to-all）；combine 把专家输出从持有专家的 rank 送回 token 的原始 rank 并按权重归约（fan-in 的 all-to-all）。

**练习 2**：为什么 combine 必须依赖 dispatch 返回的 `handle`？

> **参考答案**：因为 combine 需要知道"收到的这些 token 当初是从哪个 rank、哪个位置发来的"，以及各专家收到了多少 token，这些路由元数据都记录在 handle 里；没有 handle 就无法把数据原路、有序地送回。

---

### 4.3 NVLink 与 RDMA：节点内与节点间两类物理链路

#### 4.3.1 概念说明

把 token 在 rank 之间搬来搬去，最终要落到物理链路上。一个多节点 GPU 集群里通常有两类互连：

- **NVLink**：NVIDIA GPU 之间的高速点对点互连，用于 **同一个节点（机器）内部** 的多张 GPU 之间。带宽极高（几百 GB/s 量级）。
- **RDMA（Remote Direct Memory Access）**：通过网卡（如 InfiniBand 的 CX7）实现的远程直接内存访问，用于 **不同节点之间**。常见实现是 InfiniBand 或 RoCE。

DeepEP 把这两类链路分别叫做：

- **intranode**（节点内）通信 → 走 NVLink；
- **internode**（节点间）通信 → 走 RDMA。

在 V2 的 hybrid 模式下，一次跨节点 dispatch 会拆成两级：先用 RDMA 把数据搬到目标节点，再用 NVLink 在节点内转发到目标 GPU。这些细节会在 u3（拓扑域）和 u5（hybrid dispatch）讲。

#### 4.3.2 核心流程

性能表里的 `Topo`（topology，拓扑）列就对应这两类链路：

```text
EP 8        → 8 个 rank 全在同一节点   → 主要是 NVLink 通信
EP 8 x 2    → 2 个节点，每节点 8 rank  → 同时用 NVLink（节点内）+ RDMA（节点间）
EP 8 x 4    → 4 个节点，每节点 8 rank  → RDMA 跨节点占比更高
```

`EP N x M` 这种写法表示：每节点 N 个 rank，共 M 个节点。

#### 4.3.3 源码精读

README 的 Requirements 明确列出这两类互连：

[README.md:L71-L72](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L71-L72) —— 要求 NVLink 用于节点内（intranode）通信，RDMA 网络用于节点间（internode）通信。

性能表里 `NIC type` 为 `N/A` 的两行就是纯 NVLink 场景：

[README.md:L50-L51](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L50-L51) —— SM100、`EP 8`（单节点 8 卡）、`NIC type=N/A`，瓶颈带宽标注为 NVLink，分别达到 726/740 GB/s（满性能 64 SM）与 643/675 GB/s（最少 24 SM）。

而带 CX7 网卡的行则是跨节点 RDMA 场景（如 4.3.3 第一处引用的 SM90/CX7/EP8x2 行）。

#### 4.3.4 代码实践

1. **实践目标**：在性能表里区分 NVLink 行与 RDMA 行。
2. **操作步骤**：打开 README 性能表（第 45～51 行），按"瓶颈带宽标注是 NVLink 还是 RDMA"把 5 行分成两组。
3. **需要观察的现象**：注意 `NIC type=N/A` 与 `NIC type=CX7` 的对应关系；注意 NVLink 带宽（数百 GB/s）远高于 RDMA 带宽（数十 GB/s）。
4. **预期结果**：NVLink 组 = SM100/EP8 两行；RDMA 组 = SM90/CX7 两行 + SM100/CX7 一行。
5. 阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么节点间带宽（几十 GB/s）远低于节点内带宽（几百 GB/s）？

> **参考答案**：因为节点内走 NVLink（GPU 间专用高速互连），节点间走 RDMA 网卡（InfiniBand/RoCE 网络），后者的物理带宽天然比 NVLink 低一个量级。

**练习 2**：`EP 8 x 2` 表示什么？

> **参考答案**：每节点 8 个 rank、共 2 个节点，总共 16 个 rank；通信既有节点内 NVLink，又有节点间 RDMA。

---

### 4.4 V2 的关键变化与性能：Gin / JIT / 解析式 SM·QP / 逻辑带宽

#### 4.4.1 概念说明

README 顶部 News 明确写了 **V2 release**，它是相对 V1 的一次大重构。本讲只需记住四个关键词：

1. **NCCL Gin 后端**：V2 不再用 NVSHMEM，改用更轻量的 NCCL Gin 后端。它是 header-only（只含头文件），并且能复用已有的 NCCL communicator。可以理解为"借 NCCL 已建的通信通道来开对称内存窗口"，省掉了独立后端的初始化成本。
2. **全 JIT（Fully JIT）**：所有内核都在运行时编译，安装时不需要编译 CUDA。这样一份代码可以按当前硬件/参数即时生成最优内核。
3. **解析式 SM & QP 计算（Analytical SM & QP count）**：V1 需要用户跑 auto-tuning 找到最佳线程/SM 配置；V2 用带宽建模直接算出该用多少 SM、多少 QP（Queue Pair，RDMA 的发送/接收队列对），不再 auto-tuning。
4. **统一 `ElasticBuffer` 接口**：V2 把高吞吐和低延迟 API 合并到单一的 `ElasticBuffer`，并支持更大的并行规模（最高 EP2048）。

#### 4.4.2 核心流程

V2 创建 buffer 与决定 SM 数的流程非常简洁：

```text
# 1) 用 MoE 设置直接创建 buffer（V2 自动算大小）
buffer = ElasticBuffer(group, num_max_tokens_per_rank=..., hidden=...,
                       num_topk=..., use_fp8_dispatch=...)

# 2) 解析式算出该用多少 SM（无需 auto-tuning）
num_comm_sms = buffer.get_theoretical_num_sms(num_experts, num_topk)

# 3) 调用 dispatch/combine 时传入 num_sms，也可手动覆盖
```

性能层面，V2 的收益可量化为两点（README 原话）：

- 相比 V1，**峰值性能最高 1.3 倍**；
- **SM 占用最多降到原来的 1/4（省 4 倍 SM）**。对一个 V3 风格的训练场景，SM 占用从 24 降到 4～6，性能却相当或更好。

#### 4.4.3 源码精读

News 段落对 V2 的总览（README 第 9 行）：

[README.md:L9-L9](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L9-L9) —— V2 是对 EP 的完整重构，用比 V1 少几倍的 SM 达到极端性能，支持更大的 scale-up/scale-out 域，并把后端从 NVSHMEM 切到更轻量的 NCCL Gin。

四个关键新特性（README 第 13～25 行）：

[README.md:L13-L25](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L13-L25) —— 列出 Fully JIT、NCCL Gin 后端（header-only、可复用 NCCL communicator）、EPv2（统一 `ElasticBuffer`、最大 EP2048、解析式 SM/QP 计算、hybrid 与 direct 两种模式都支持、V3-like 训练 SM 从 24 降到 4～6）、以及 0 SM 的 Engram/PP/CP。

统一接口与解析式 SM（README 第 115 行与第 158～160 行）：

[README.md:L115-L115](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L115-L115) —— V2 把所有 EP 操作统一到单一 `ElasticBuffer` 接口，可直接用 MoE 设置初始化，并由解析式算出最优 SM/QP 数。

[README.md:L158-L160](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L158-L160) —— 用 `_buffer.get_theoretical_num_sms(num_experts, num_topk)` 解析式计算最优 SM 数，无需 auto-tuning；也可在 dispatch/combine 调用里用 `num_sms=` 手动覆盖。

性能提升总结（README 第 55 行）：

[README.md:L55-L55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L55-L55) —— 与 V1 相比，V2 峰值性能最高 1.3 倍，同时最多节省 4 倍 SM。

性能表与"逻辑带宽"含义（README 第 45～53 行）：

[README.md:L45-L53](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L45-L53) —— 性能表列出不同 Arch/NIC/Topo 下的 dispatch/combine 瓶颈带宽与所用 SM 数；第 53 行注释说明这些数字是"逻辑带宽"，例如 `EP 8 x 2` 的 90 GB/s 实际上**包含了本 rank 内部流量**。

> **关于"逻辑带宽"的准确理解**：README 明确说这个 90 GB/s "contains local rank traffic"，即它不是 RDMA 网线上的纯物理带宽，而是把"应用视角下经过瓶颈路径的有效吞吐（含本地 rank 那一份）"折算进去之后的数字。具体的测量方法（如何用通信 barrier 隔离、如何用 CUDA Kineto 计时）封装在基准代码里，会在 u8-l4「测试与基准」讲义详细拆解；本讲只需记住"逻辑带宽 ≠ 纯网线带宽"。

#### 4.4.4 代码实践

1. **实践目标**：把 V2 的四个关键变化与 README 行号对应起来。
2. **操作步骤**：在 README 的 New features 列表里，分别找到 Fully JIT、NCCL Gin backend、EPv2、0 SM 三件套对应的行（约第 13～25 行），并各抄一句最有代表性的描述。
3. **需要观察的现象**：注意 V2 标注的代价/取舍（见第 27～31 行 Notes：buffer 占用比 V1 大、不再支持 0 SM RDMA low-latency EP、Engram/PP/CP 为实验性）。
4. **预期结果**：得到一张"特性 → README 行号 → 一句话描述"的小表。
5. 阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：V2 用什么取代了 V1 的 NVSHMEM 后端？这个替代品有什么好处？

> **参考答案**：改用 NCCL Gin 后端。好处是 header-only、轻量，且能复用已有的 NCCL communicator，省去独立后端的初始化与依赖。

**练习 2**：V2 决定"用多少 SM 通信"的方式和 V1 有什么不同？

> **参考答案**：V1 需要 auto-tuning；V2 用带宽建模 **解析式** 直接算出 `get_theoretical_num_sms(...)`，无需 auto-tuning（也可手动覆盖）。

**练习 3**：V2 相比 V1 的两个量化收益是什么？

> **参考答案**：峰值性能最高 1.3 倍；SM 占用最多降到 1/4（即最多省 4 倍 SM）。

## 5. 综合实践

这是本讲的核心实践任务，对应学习手册规划的代码实践。请完成下面这份"性能表阅读理解"小作业：

> **任务**：阅读 README 的 Performance 表格（第 45～53 行），用自己的话写出 **在 `SM90 / CX7 / EP 8 x 2` 配置下**，dispatch 与 combine 的瓶颈带宽分别是多少，并解释"逻辑带宽"的含义。

操作步骤：

1. 打开 `README.md`，定位到第 47 行（`SM90 / CX7 / EP 8 x 2`）。
2. 读出该行的 **Dispatch Bottleneck Bandwidth** 与 **Combine Bottleneck Bandwidth** 两个数字，并注意它们括号里标注的瓶颈链路类型（NVLink 还是 RDMA）以及 `#SMs`。
3. 结合第 53 行的注释，解释"逻辑带宽"为什么说"contains local rank traffic"。
4. （延伸）再对比第 50～51 行的 SM100/EP8（纯 NVLink）数字，体会节点内 NVLink 与节点间 RDMA 的带宽差距。

预期答案要点：

- SM90/CX7/EP8x2：dispatch 瓶颈带宽 **90 GB/s（RDMA）**，combine 瓶颈带宽 **81 GB/s（RDMA）**，使用 **12 个 SM**（见 [README.md:L47-L47](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L47-L47)）。
- "逻辑带宽"指：表中的瓶颈带宽并非 RDMA 网线上的纯物理带宽，而是把应用视角下流经瓶颈路径的有效吞吐（包含本 rank 内部的那部分流量）折算后的数字；所以 `EP 8 x 2` 的 90 GB/s 实际上"contains local rank traffic"（见 [README.md:L53-L53](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L53-L53)）。

> 提示：精确的测量方法（barrier 隔离 + Kineto 计时）封装在基准脚本里，会在后续 **u8-l4「测试与基准测试体系」** 讲义详细拆解；本讲只要能复述数字与"逻辑带宽"的直觉即可。

## 6. 本讲小结

- DeepEP 是一个面向 MoE 训练/推理的 **高性能通信库**，核心是 EP 所需的 dispatch/combine all-to-all 内核，追求零或极少的 SM 占用。
- **dispatch** 把 token 发到目标专家所在的 rank；**combine** 把专家输出送回原 rank 并加权归约；两者通过 `EPHandle` 串联，且互为反向。
- 物理上有两类链路：**NVLink（节点内 intranode）** 与 **RDMA（节点间 internode）**；`EP N x M` 描述每节点 N rank、共 M 节点的拓扑。
- V2 的四个关键词：**NCCL Gin 后端**（取代 NVSHMEM）、**全 JIT**、**解析式 SM/QP 计算**（取代 auto-tuning）、**统一 `ElasticBuffer` 接口**（最大 EP2048）。
- 相比 V1，V2 峰值性能最高 1.3 倍、SM 占用最多降到 1/4；代价是 buffer 占用更大、且不再支持 0 SM RDMA low-latency EP。
- 性能表里的带宽是 **逻辑带宽**：它包含了本 rank 的内部流量，不等于 RDMA 网线上的纯物理带宽。

## 7. 下一步学习建议

本讲只读了 README，还没有真正"摸到"代码。建议下一讲继续：

- **u1-l2「目录结构与代码分层」**：进入仓库内部，搞清 `deep_ep/`（Python）、`csrc/`（C++/CUDA）、`deep_ep/include/`（JIT 内核源）、`tests/`、`docs/` 之间的调用方向，画出一次 `buffer.dispatch()` 从 Python 到 GPU kernel 的依赖图。
- 在读 u1-l2 之前，可以先自己浏览一遍仓库根目录的文件列表，对照本讲的"源码地图"建立感性认识。
- 如果你想先看到一次真实运行，也可以跳到 **u1-l4「快速上手：跑通 test_ep.py」**（但会用到 u1-l3 讲的安装/构建知识）。
