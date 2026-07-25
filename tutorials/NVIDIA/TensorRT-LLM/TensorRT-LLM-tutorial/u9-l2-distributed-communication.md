# 分布式通信原语

> 本讲是单元 u9「分布式与并行」的第二讲，承接 u9-l1「Mapping 与并行策略」。
> u9-l1 解决的是「**拓扑如何描述**」——并行度怎么配置、`Mapping` 如何编码每个 rank 的坐标；
> 本讲解决的是「**通信怎么发生**」——有了拓扑之后，rank 之间到底用什么 API、走什么硬件路径去交换数据。

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 `Distributed` 这个通信抽象基类的作用，以及它如何按 MPI / PyTorch 两种后端分裂出 `MPIDist` 与 `TorchDist`。
2. 掌握 `AllReduce` 原语的多策略选择（NCCL / MIN_LATENCY / UB / SYMM_MEM / MNNVL / AUTO），并理解 `allreduce_helper`（IPC + Lamport 工作区）与 `symm_mem_allreduce`（MULTIMEM 硬件指令）两套实现各自适合的场景。
3. 了解 MoE 专家并行中 `MoeAlltoAll` 的 dispatch / combine 两阶段状态机，能画出一次 all-to-all 在前向中的数据搬运方向。
4. 区分「集合通信（张量数据，走 NCCL/MULTIMEM）」与「控制面通信（Python 对象，走 pickle+MPI/torch object API）」两条不同的链路。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个会被反复用到的概念。

### 2.1 为什么分布式推理需要「通信原语」

把一个模型切成多块放到多张 GPU 上后，每次前向都需要把各 GPU 的中间结果拼回完整结果，最典型的两类操作：

- **AllReduce（全员规约）**：每张 GPU 各持有一份部分和，规约（通常是求和）后**每张 GPU 都得到相同的完整和**。张量并行（TP）里，attention/MLP 的输出按列切分，必须 AllReduce 求和才能得到完整 hidden states。
- **All-to-All（全交换）**：每张 GPU 都要把自己的一部分数据发给**所有其他 GPU**，同时从所有 GPU 收数据。专家并行（EP）里，token 要被送到「持有它所选专家」的那张 GPU 上计算，这就是 all-to-all。

### 2.2 两类完全不同的「通信」

这是初学者最容易混淆的点，本讲会反复强调：

| 类型 | 传什么 | 走什么 | 例子 |
|------|--------|--------|------|
| **数据面（data plane）** | 大块 GPU 张量 | NCCL、自定义 kernel、MULTIMEM 硬件 | AllReduce、All-to-All、PP send/recv |
| **控制面（control plane）** | 小的 Python 对象（配置、元数据、调度决策） | pickle 序列化 + MPI / `torch.distributed` object API | 广播 `SamplingParams`、allgather 调度状态 |

u9-l1 讲的 `Mapping` 是「地图」，本讲的 `Distributed` 是「地图上跑的交通工具」。控制面用 `Distributed.broadcast/allgather`，数据面用 `AllReduce` / `MoeAlltoAll` / `PPCommNCCL`。

### 2.3 环形 AllReduce 的通信量

理解后面策略选择的数学基础：对一个大小为 \(M\) 的张量、\(N\) 个 rank，环形 AllReduce 分 scatter-reduce 与 allgather 两半，每 rank 的通信量为

\[
\frac{2(N-1)}{N} \cdot M \;\approx\; 2M \quad (N \text{ 较大时})
\]

即通信量与 rank 数基本无关，这正是 AllReduce 可扩展的关键。而 all-to-all 的通信量是每 rank 收发 \(O(M)\)，总量 \(O(N \cdot M)\)。

### 2.4 MULTIMEM 是什么

NVIDIA NVSwitch / NVLink 网络提供的硬件「多播 + 规约」指令。一次 MULTIMEM all_reduce 可以让一张 GPU **在一次内存读操作里**读到所有 peer 的数据并求和，比 NCCL 的多步 ring/tree 协议延迟低很多。它要求参与 rank 的缓冲区被注册成「对称内存（symmetric memory）」。这就是 `symm_mem_allreduce` 想用的硬件能力。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `tensorrt_llm/_torch/distributed/` 与 `tensorrt_llm/_torch/` 下：

| 文件 | 作用 |
|------|------|
| [`tensorrt_llm/_torch/distributed/communicator.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py) | 通信抽象基类 `Distributed` 及其 MPI/PyTorch 两个实现；控制面对象广播/聚合；流水线 P2P 通信 |
| [`tensorrt_llm/_torch/distributed/allreduce_helper.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/allreduce_helper.py) | 自定义 allreduce 的工作区/IPC 缓冲区分配与容量计算（Lamport 三缓冲） |
| [`tensorrt_llm/_torch/distributed/symm_mem_allreduce.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allreduce.py) | 基于 PyTorch 对称内存 + MULTIMEM 硬件指令的低延迟 AllReduce |
| [`tensorrt_llm/_torch/distributed/ops.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py) | `AllReduce` / `MoEAllReduce` / `MNNVLAllReduce` 模块、`allgather` / `reducescatter` 等函数 |
| [`tensorrt_llm/_torch/distributed/moe_alltoall.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py) | MoE 专家并行的 all-to-all dispatch/combine 状态机 |
| [`tensorrt_llm/_torch/device_mesh.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py) | 用 `torch.distributed.device_mesh` 把拓扑维度映射成 ProcessGroup |
| [`tensorrt_llm/_torch/distributed/pg_utils.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/pg_utils.py) | 子通信组创建工具（`split`） |
| [`tensorrt_llm/functional.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/functional.py) | `AllReduceStrategy` / `AllReduceFusionOp` 等枚举的真值表 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**communicator 抽象**、**AllReduce 原语**、**MoE All-to-All**。

### 4.1 Communicator 抽象：Distributed 与 ProcessGroup

#### 4.1.1 概念说明

`Distributed` 是 TensorRT-LLM 在 PyTorch 后端里对「**控制面通信**」的统一抽象。它的职责不是搬大张量，而是：

- 在所有 rank 之间**广播 / 聚合 Python 对象**（如采样参数、调度决策、KV cache 元信息）；
- 提供一组与并行维度对齐的「分组」操作：`tp_allreduce`（只在张量并行组内）、`cp_broadcast`（只在上下文并行组内）、`pp_allgather`（只在流水线并行组内）等；
- 屏蔽底层到底是 MPI 还是 `torch.distributed`（NCCL/gloo）。

为什么需要两种后端实现？因为 TensorRT-LLM 有两种多进程编排方式：

- **MPI 模式**（`ENABLE_MULTI_DEVICE=True`）：用 `mpi4py` 拉起多个进程，跨进程通信用 MPI；
- **torch.distributed 模式**（`mpi_disabled()` 为真，即 `ENABLE_MULTI_DEVICE=False`）：用 `torchrun` / Ray 拉起进程，通信用 `torch.distributed`。

`Distributed.get()` 这个带 `lru_cache` 的工厂方法负责按运行环境二选一：

[communicator.py:81-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L81-L87) —— `get()` 按 MPI 是否启用选择 `TorchDist` 或 `MPIDist`，并用 `lru_cache` 保证「一个 Mapping 只造一个通信器」。

> 关键点：`lru_cache(maxsize=None)` 的入参是 `Mapping`，意味着同一个拓扑只会创建一次通信器实例并全局复用。在 u3 的 PyExecutor 启动流程里，[py_executor_creator.py:304](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L304) 就是用 `Distributed.get(mapping)` 拿到这个单例的。

#### 4.1.2 核心流程

`Distributed` 是 ABC（抽象基类），定下一组必须实现的「原语契约」，然后派生类各填各的实现：

```
Distributed (ABC, communicator.py:76)
├── 抽象方法（必须实现）
│   ├── local_world_size   # 本机共置的 rank 数
│   ├── barrier / tp_barrier
│   ├── broadcast / allgather / allreduce      # 全员（WORLD）级
│   ├── tp_broadcast / tp_allreduce / tp_allgather  # 张量并行级
│   └── cp_broadcast / cp_allgather            # 上下文并行级
├── 具体方法（已有默认实现）
│   ├── tp_cp_broadcast   # 先 TP 广播再 CP 广播
│   └── tp_cp_allgather   # 先 CP 聚合再 TP 聚合，最后展平
├── MPIDist   (communicator.py:647)  ← MPI 后端
└── TorchDist (communicator.py:798)  ← torch.distributed 后端
```

抽象方法定义在：

[communicator.py:162-216](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L162-L216) —— 抽象方法清单：`local_world_size`、`barrier`、`tp_barrier`、`broadcast`、`allgather`、`allreduce`、`tp_allreduce`、`tp_broadcast`、`cp_broadcast`、`tp_allgather`、`cp_allgather`。

`tp_cp_allgather` 这个默认实现体现了「**复合 = 串联两个分组操作**」的设计思路：先在 CP 组里 allgather（得到 `[[cp0,cp1],...]` 的形状），再在 TP 组里 allgather，最后展平成 `[tp0_cp0, tp0_cp1, tp1_cp0, ...]`：

[communicator.py:218-237](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L218-L237) —— 先 CP 后 TP 的两段聚合，注释里给出了嵌套列表展平后的顺序。

#### 4.1.3 源码精读

**（a）TorchDist：torch.distributed 后端**

`TorchDist` 把每个抽象方法翻译成 `torch.distributed` 的对应调用。最典型的是 `allreduce`：

[communicator.py:1001-1015](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L1001-L1015) —— `TorchDist.allreduce`：把 Python 标量包成 `torch.tensor`，调 `dist.all_reduce`，再 `.item()` 取回标量；张量则直接规约。这是**控制面**对**小标量**的规约，不是大张量数据面。

注意它还做了「按节点切本地通信组」的工作——`setup_local_comm` 用 Ray 拿到每个 rank 所在的节点 IP，把同节点的 rank 聚成一个 `cuda:nccl,cpu:gloo` 混合后端的子 ProcessGroup：

[communicator.py:826-843](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L826-L843) —— `setup_local_comm`：按节点 IP 分组，为同节点 rank 建 `cuda:nccl,cpu:gloo` 双后端本地组。`local_world_size` 这个属性就来自它。

构造函数里还有一行很关键——`set_torch_comm(self)` 把自己注册成全局通信器，并 `mapping.build_mesh()` 触发 DeviceMesh 构建：

[communicator.py:808-824](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L808-L824) —— `TorchDist.__init__`：断言 `torch.distributed` 已初始化，设全局 comm，建 mesh，建本地组，再用 `init_pg` 把本地组同步给 C++ 侧。

**（b）MPIDist：MPI 后端**

`MPIDist` 的分组通信靠「**惰性创建 MPI 子通信器**」实现——`tp_comm` / `pp_comm` / `cp_comm` 都是 `@property`，首次访问时才用 `mpi_comm().Create_group(...)` 把 `mapping.tp_group` 这串 rank 编号变成一个 MPI 子通信器：

[communicator.py:707-731](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L707-L731) —— 三个子通信器属性都先 `_validate_world_size()` 防 segfault，再用 `group.Incl(mapping.tp_group)` + `Create_group` 建子通信器，结果缓存到 `self._tp_comm`。

MPIDist 还有一组针对**大 Python 对象**的安全传输函数 `safe_broadcast` / `safe_gather` / `safe_allgather`——它们用 `pickle.dumps` 序列化后，按固定 4MB 分块用原始字节 MPI 集合通信（`MPI.Bcast` / `MPI_Gatherv` / `MPI_Allgatherv`）传输，绕开 pickle5 的带外缓冲区，并在总字节数超过 int32 上限（约 2GB）时自动分轮：

[communicator.py:240-333](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L240-L333) —— `safe_broadcast`：序列化 → 广播定长 header（ok_flag/total_size/num_chunks）→ 分块 `MPI.Bcast` 原始字节 → 反序列化；root 失败会用 `ok_flag=0` 通知所有 rank。

> 这是控制面通信里「**把对象当字节流传**」的典型实现，与数据面张量通信完全无关。

**（c）DeviceMesh：从拓扑维度到 ProcessGroup**

`TorchDist` 里调的 `mapping.build_mesh()` 实际落在 `DeviceMeshTopologyImpl`（device_mesh.py）。它的作用是把 u9-l1 讲的并行维度（pp / tp / cp / moe_tp / moe_ep）用 PyTorch 的 `DeviceMesh` 组织起来，之后 `tp_group_pg` / `cp_group_pg` 这些属性直接返回对应维度的 ProcessGroup：

[device_mesh.py:109-146](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L109-L146) —— `build_mesh`：维度从外到内是 `pp → (moe_tp, moe_ep 或 tp) → cp`，内层维度 rank 连续（便于 NVLink 通信）；若启用专家并行，会把 `moe_tp × moe_ep` 展平成一个合并的 `tp` mesh。

注意「**内层维度连续**」这条约定：cp 放最内层意味着同一 TP 组的各 cp rank 在全局 rank 编号上相邻，这正是 NVLink/MULTIMEM 这类「同节点优先」硬件所期望的布局。

**（d）流水线并行的点对点通信**

PP（流水线并行）不需要集合通信，只需要相邻 stage 之间 send/recv 激活值。这部分由 `PPCommNCCL`（MPI 模式下用自定义 NCCL communicator）与 `PPCommTorch`（torch.distributed 模式下用 ProcessGroup）承担，并暴露成两个 `torch.library.custom_op`，这样能被 `torch.compile` / CUDA Graph 正确捕获：

[communicator.py:1183-1217](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L1183-L1217) —— `PPCommNCCL.send/recv`：CUDA Graph 捕获时不能在 `send_stream` 里发 NCCL send，故特别判断 `torch.cuda.is_current_stream_capturing()` 改走当前流；非捕获场景会把张量 `clone()` 再发，避免 userbuffers 内存池的写-写冲突。

[communicator.py:1294-1308](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L1294-L1308) —— 把 `pp_recv` / `pp_send` 包装成 `trtllm::pp_recv_tensors` / `trtllm::pp_send_tensors` 自定义算子，`mutates_args` 声明它会在原地改张量，让图编译器知道这是通信副作用。

#### 4.1.4 代码实践

**实践目标**：用单进程版 `torch.distributed` 的 mental model 读懂「控制面通信」与「数据面通信」的分界。

**操作步骤**（源码阅读型实践，无需多卡）：

1. 打开 `communicator.py`，对比 `TorchDist.allreduce`（[L1001-L1015](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L1001-L1015)）和 `ops.py` 里的 `AllReduce.forward`（[L813](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L813)）。
2. 在仓库里搜索 `Distributed.get(mapping).allgather` 与 `Distributed.get(mapping).broadcast`，看它们各被用来传什么对象（提示：在 `kv_cache_manager_v2.py`、`resource_manager.py` 里能看到）。
3. 对比 `tp_allreduce`（组内）与 `allreduce`（全员）的 group 参数差异。

**需要观察的现象**：`Distributed` 的方法签名里传的几乎都是 Python 对象（`obj`）或小标量，而 `AllReduce` 模块传的是 `torch.Tensor`（hidden states）。

**预期结果**：你会清楚地看到「控制面用 `Distributed`、数据面用 `AllReduce`/`MoeAlltoAll`」这条分界线——这正是本讲最重要的一条心智模型。

> 如需真正运行多卡验证，可参考 [`tests/unittest/_torch/multi_gpu/test_allreduce.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/unittest/_torch/multi_gpu/test_allreduce.py)（`AllReduce` 文档里点名的参考实现测试）。

#### 4.1.5 小练习与答案

**练习 1**：`Distributed.get()` 为什么用 `lru_cache`？如果不用会怎样？

**参考答案**：`Mapping` 是不可变的拓扑描述，由它派生的 MPI 子通信器 / ProcessGroup 创建代价高（涉及集合握手）。`lru_cache` 保证「同一拓扑只握手一次」并全局复用同一个通信器；不用的话每次 `get` 都会重复建子通信器，既慢又可能因重复 `new_group` 导致资源泄漏或死锁。

**练习 2**：`tp_cp_allgather` 为什么先 CP 后 TP，而不能反过来？

**参考答案**：因为 TP 与 CP 是两个独立的分组维度，allgather 的语义是「在某个组内聚合」。先 CP 得到「本 TP rank 在各 CP 上的副本」，再 TP 把这些副本跨 TP 聚合，最终展平成 `tp_size × cp_size` 个条目。顺序调换会得到不同的条目排列（TP 外层 CP 内层），与下游消费方期望的 `[tp0_cp0, tp0_cp1, tp1_cp0, ...]` 顺序不符。

---

### 4.2 AllReduce 原语：从 NCCL 到 MULTIMEM

#### 4.2.1 概念说明

数据面的 AllReduce 是张量并行里**最高频**的通信，每过一个 decoder layer 的 attention/MLP 都要做一次，所以它有极其多样的实现策略，按硬件能力从「通用」到「专用」排开：

| 策略（`AllReduceStrategy`） | 实现路径 | 适用场景 |
|------|--------|--------|
| `NCCL` (0) | 标准 NCCL ring/tree | 兜底，任意硬件 |
| `NCCL_SYMMETRIC` (8) | NCCL + 对称内存 window 零拷贝 | 支持 NCCL window 的拓扑 |
| `MIN_LATENCY` (1) | 自定义 one-shot/two-shot kernel | 单节点 NVLink，小张量 |
| `UB` (2) | userbuffers 持久化缓冲 | 融合场景，可与其他算子重叠 |
| `LOWPRECISION` (6) | 量化后低精度传输 | 仅 PCIe 交换、无 NVLink 的拓扑 |
| `MNNVL` (7) | 多节点 NVLink（MNNVL）专用 | aarch64 + 多节点 NVLink |
| `SYMM_MEM` (9) | PyTorch 对称内存 + MULTIMEM 指令 | NVSwitch，SM9.0/10.0，特定 world_size |
| `AUTO` (3) | 按拓扑/heuristic 自动选 | **默认值**，生产推荐 |

枚举真值在 [functional.py:108-118](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/functional.py#L108-L118)。

还有一个正交维度——**融合算子** `AllReduceFusionOp`：把 AllReduce 与紧跟其后的 residual + RMSNorm（甚至 FP8/NVFP4 量化）融合成一个 kernel，省一次显存往返。支持的融合模式见 [functional.py:121-131](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/functional.py#L121-L131)（`RESIDUAL_RMS_NORM`、`..._QUANT_FP8`、`..._QUANT_NVFP4` 等）。

> 经验法则：张量越大、rank 越多，越适合 NCCL ring（带宽利用好）；张量越小、延迟越敏感，越适合 MULTIMEM / 自定义 one-shot kernel。`AUTO` 策略 + autotune 就是在自动做这个权衡。

#### 4.2.2 核心流程

`AllReduce` 模块（`ops.py:654`）是面向模型代码的入口，它的 `forward` 按如下顺序挑实现：

```
AllReduce.forward(input, all_reduce_params)
│
├─ tp_size==1 或 enable_allreduce==False → 直接返回 input（短路）
│
├─ 1. 若有 symm_mem_allreduce 且 fusion_op==NONE
│      → 试 MULTIMEM；成功就返回，失败（返回 None）继续往下
│
├─ 2. 若有 mnnvl_allreduce
│      → 试 MNNVL；成功返回，失败继续
│
├─ 3. 策略归一化：MNNVL/SYMM_MEM → AUTO（因为底层 op 没有这俩分支）
│
├─ 4a. AUTO + 未禁用 autotune + MPI 模式
│      → torch.ops.trtllm.tunable_allreduce（自动选 one-shot/two-shot/NCCL）
│
└─ 4b. 否则
       → self.all_reduce_op（trtllm.allreduce 或 trtllm.allreduce_pg）
```

这个「**逐层尝试、失败回退**」的模式是本模块的核心设计——专门硬件路径（MULTIMEM、MNNVL）先试，拿不到再退到通用 NCCL/AUTO。

#### 4.2.3 源码精读

**（a）AllReduce 构造：按策略装配**

构造函数根据 `strategy` 决定要预分配哪些资源：

[ops.py:731-787](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L731-L787) —— 关键装配逻辑：`SYMM_MEM` 时尝试建 `SymmetricMemoryAllReduce`，建不成功就退回 `AUTO`；`AUTO`/`MNNVL` 时按 `MNNVLAllReduce.is_mnnvl` 判断是否建 MNNVL；需要工作区的策略调 `get_allreduce_workspace`。注意 `tp_size > 1 and not enable_attention_dp` 这个前置条件——attention 数据并行时跳过 AllReduce（因为各 DP rank 各算各的）。

其中 `all_reduce_op` 按 MPI 是否禁用选两个不同的 torch op：

[ops.py:711](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L711) —— `torch.ops.trtllm.allreduce_pg`（禁用 MPI 时，需显式传 ProcessGroup）vs `torch.ops.trtllm.allreduce`（MPI 模式，内部已知通信组）。

**（b）AllReduce.forward：逐层尝试**

[ops.py:843-883](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L843-L883) —— forward 主干：短路判断 → 先试 `symm_mem_allreduce`（仅 `NONE` 融合）→ 再试 `mnnvl_allreduce` → 把 MNNVL/SYMM_MEM 归一化成 AUTO。

[ops.py:900-932](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L900-L932) —— AUTO 走 `tunable_allreduce`（带 autotune），其余走 `all_reduce_op`；`_disable_mpi` 时额外塞 `rank` 和 `pg.boxed()`。最后 `len(output) > 1` 时返回多输出（融合场景），否则返回单个张量。

**（c）CustomAllReduceHelper：自定义 kernel 的工作区管家**

自定义 allreduce（one-shot/two-shot、UB）需要在每张 GPU 上预分配一块**对所有 peer 可见**的 IPC 缓冲区，配 Lamport 三缓冲做无锁同步。`CustomAllReduceHelper` 就是这个工作区的分配器：

[allreduce_helper.py:46-84](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/allreduce_helper.py#L46-L84) —— `max_workspace_size_auto`：默认 64 MiB（支持 hidden=4096/max_tokens=8192 一类常见配置），可用 `TRTLLM_ALLREDUCE_FUSION_WORKSPACE_SIZE` 覆盖；强制确定性时用更大工作区。注释里给出了 Llama 8B / TP=4 的容量推算。

[allreduce_helper.py:122-155](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/allreduce_helper.py#L122-L155) —— `allocate_allreduce_fusion_workspace`：分配四块缓冲——`ipc_buffers`（数据）、`ipc_barriers`（屏障）、`lamport_buffers`（三缓冲，`3*size*tp_size`）、`flag_buffer`/`layout_buffer`（控制标志）；P2P 支持时调 `lamport_initialize`。最终把所有指针序列化成一个 `int64` 张量传给 C++ kernel。

> 通俗理解：这相当于在每张 GPU 上提前画好一块「**公告板**」（IPC buffer），所有 peer 都能直接读写自己那块；用 Lamport 三缓冲 + 原子标志位保证「我写完了」的可见性，不靠 NCCL 协议握手，所以延迟更低。

**（d）SymmetricMemoryAllReduce：MULTIMEM 硬件指令**

这是另一条完全不同的路线——不走 NCCL，而是用 PyTorch 的 symmetric memory + MULTIMEM 硬件多播规约指令。它对硬件/拓扑要求严格：

[symm_mem_allreduce.py:32-53](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allreduce.py#L32-L53) —— 支持矩阵 `_WORLD_SIZES_MULTIMEM` / `_MAX_SIZES`：SM9.0 支持 world_size {4,6,8}，SM10.0 支持 {6,8}；每档有最大缓冲上限（如 SM9.0/8 卡 = 64 MiB）。不在表里的配置直接 `disabled=True` 退出。

它的 `forward` 非常短，核心就一行 `multimem_all_reduce_`：

[symm_mem_allreduce.py:191-227](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allreduce.py#L191-L227) —— 把输入 copy 进对称内存 buffer → 调 `torch.ops.symm_mem.multimem_all_reduce_`（硬件一次多播规约）→ copy 回输出。

能否用由 `can_use_symm_mem` 把关：

[symm_mem_allreduce.py:178-189](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allreduce.py#L178-L189) —— 四道关卡：未禁用、dtype 匹配、字节数 4 对齐、小于 `max_size`。任何一条不满足就返回 `False`，让 `AllReduce.forward` 走回退路径。

> **关键设计**：`forward` 在不满足条件时返回 `None`（而非抛异常），让调用方（`AllReduce.forward` 的第 857-863 行）能优雅回退。这是「**能力探测 + 回退**」模式的典型写法。

**（e）MNNVLAllReduce：多节点 NVLink**

MNNVL（Multi-Node NVLink）是跨节点 NVLink 直连，主要用于 aarch64 平台的多节点大模型。它的判定条件很苛刻：

[ops.py:564-574](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L564-L574) —— `is_mnnvl`：dtype 受限、不能有 CP、必须是多节点、`MnnvlMemory.supports_mnnvl()`、且架构是 aarch64（单机单测可用 `TLLM_TEST_MNNVL=1` 绕过）。

它还把 one-shot / two-shot 按张量大小自动切换，阈值 `_MNNVL_ONE_SHOT_THRESHOLD_BYTES`：

[ops.py:577-591](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L577-L591) —— `get_required_workspace_size`：小张量（≤阈值）one-shot，每 rank 存 `num_tokens*group_size` 份；大张量 two-shot，分片存且需 2 stage。

> one-shot 一次性把所有 peer 数据拉齐，延迟最低但显存占用大；two-shot 分批，省显存但多一轮。这与 ring allreduce 的带宽-延迟权衡是同一类问题。

#### 4.2.4 代码实践

**实践目标**：对比 `CustomAllReduceHelper`（自定义 kernel + IPC/Lamport）与 `SymmetricMemoryAllReduce`（MULTIMEM）两套实现的适用场景。

**操作步骤**（源码阅读型实践）：

1. 打开 `allreduce_helper.py` 的 `allocate_allreduce_fusion_workspace`（[L122-L155](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/allreduce_helper.py#L122-L155)），数一数它分配了几块缓冲、每块的作用（数据/屏障/Lamport/标志位）。
2. 打开 `symm_mem_allreduce.py` 的 `__init__`（[L55-L171](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allreduce.py#L55-L171)），找出它检查了哪些硬件前置条件（device capability、world_size、`multicast_ptr`）。
3. 对照 `AllReduce.forward`（[ops.py:843-932](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py#L843-L932)），画出「SYMM_MEM 失败 → MNNVL 失败 → AUTO/NCCL」的回退决策树。

**需要观察的现象**：两套实现都遵循「**初始化时探测能力、forward 时尝试并回退**」的模式，从不硬性要求硬件必须支持。

**预期结果**（适用场景对比表）：

| 维度 | `CustomAllReduceHelper` 路线 | `SymmetricMemoryAllReduce` 路线 |
|------|------------------------------|----------------------------------|
| 硬件依赖 | IPC + P2P（NVLink/PCIe） | NVSwitch + MULTIMEM 指令 |
| 同步机制 | Lamport 三缓冲 + 原子标志 | 硬件多播规约（一次读所有 peer） |
| 覆盖面 | 宽（几乎所有多 GPU） | 窄（仅 SM9.0/10.0 + 特定 world_size） |
| 支持融合 | 是（residual+RMSNorm+quant） | 否（仅 `NONE`） |
| 典型用途 | 默认 `AUTO`/`MIN_LATENCY`/`UB` | 小张量极致低延迟 |

> 如需真机验证：在有 NVSwitch 的 8 卡 H100（SM9.0）上，设 `attn_backend` 不变、把模型 `AllReduce` 策略分别设成 `AUTO` 与 `SYMM_MEM`，跑同一 prompt，用 Nsight Systems 观察 allreduce kernel 的耗时差异。「待本地验证」具体数值，但应观察到 `SYMM_MEM` 在小 batch 下延迟更低。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SymmetricMemoryAllReduce.forward` 在不能用时返回 `None` 而不是抛异常？

**参考答案**：因为它被设计成 `AllReduce.forward` 里的「优先尝试路径」。返回 `None` 让调用方平滑回退到 MNNVL 或 NCCL/AUTO；若抛异常，调用方就得用 try/except 包裹，且异常构造开销大。这是「能力探测」模式的标配写法。

**练习 2**：`AUTO` 策略最终可能落到哪几种实际实现？

**参考答案**：依次可能落到 `MNNVLAllReduce`（多节点 NVLink）、`tunable_allreduce`（autotune 自动选 one-shot/two-shot/NCCL），如果运行时 `symm_mem_allreduce` 被启用也会先试它。即 `AUTO` 是「MNNVL > symm_mem > tunable(NCCL/one-shot/two-shot)」的决策链。

**练习 3**：融合算子 `RESIDUAL_RMS_NORM_QUANT_FP8` 相比 `NONE` 多省了什么？

**参考答案**：把 `AllReduce → 加残差 → RMSNorm → FP8 量化` 四步合成一个 kernel，省掉了中间结果（hidden states、residual）多次写回 HBM 再读出的显存往返。融合对带宽密集的 decode 阶段尤其重要。但注意 `SYMM_MEM` 路线目前**不支持**融合（见 `AllReduce.forward` 第 857 行的 `fusion_op == AllReduceFusionOp.NONE` 判断），融合会走回退路径。

---

### 4.3 MoE All-to-All：专家并行的数据搬运

#### 4.3.1 概念说明

专家并行（Expert Parallelism, EP）把不同的「专家」放到不同 GPU 上。一个 token 经 router 算出它要去的 top-k 个专家后，这些专家很可能不在本地 GPU 上——必须把 token 的 hidden states **送到持有目标专家的 GPU**，算完再**收回来**加权求和。这个「发出去 → 各自算 → 收回来」就是 MoE all-to-all，它由两个对称的阶段组成：

- **dispatch（分发）**：每个 rank 把本地 token 按其路由结果发给对应的远端 rank；同时收到所有 rank 发来的、需要本地专家处理的 token。
- **combine（合并）**：本地专家算完后，把结果按原路发回，每个 rank 把自己发出的 token 的多份专家输出加权合并。

这与 AllReduce 的「全员都得到相同结果」完全不同——all-to-all 是「**定向交换**」，每个 rank 收发的数据内容和大小都不同（取决于路由）。

#### 4.3.2 核心流程

`MoeAlltoAll` 类（`moe_alltoall.py:38`）用 `MNNVL` 单边内存（one-sided）实现这套交换，并把 dispatch/combine 串成一个**显式状态机**：

```
状态: _A2AState.phase ∈ {idle, dispatched}

1. dispatch(token_selected_experts, input_payloads, ...)
   ├─ 断言 phase == "idle"（不能连续 dispatch 两次）
   ├─ moe_a2a_dispatch(...)   # 把 token 按 expert 路由发到各 rank
   ├─ 记录 local_num_tokens / combine_payload_offset / eplb_stats
   └─ phase → "dispatched"
        ↓  （中间：本地对各 rank 收到的 token 跑 GroupGEMM = 专家计算）

2. combine(payload, runtime_max_tokens_per_rank, ...)
   ├─ 断言 phase == "dispatched"（必须先 dispatch）
   ├─ moe_a2a_combine(...)     # 把专家输出按原路发回并加权合并
   └─ reset_state() → phase 回 "idle"
```

关键约束：**dispatch 和 combine 必须严格配对**，中间夹着专家计算。状态机用 `phase` 字段强制保证这个顺序——连续两次 dispatch 或先 combine 后 dispatch 都会断言失败。

#### 4.3.3 源码精读

**（a）工作区：单进程共享、按 ep 维度铺开**

`MoeAlltoAll` 把整个 all-to-all 的工作区做成**类级单例** `_WORKSPACE`（同一进程内多个 MoE 层复用），并用 `MnnvlMemory` 提供 NVLink 单边访问的显存：

[moe_alltoall.py:97-126](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L97-L126) —— `_init_constants` 从 C++ 头读出 metainfo 字段在 workspace 里的偏移索引（发送/接收计数器、完成标志、topk 目标 rank 等）。

[moe_alltoall.py:60-95](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L60-L95) —— `calculate_required_workspace_size`：按 `ep_size × max_num_tokens` 铺排 dispatch 与 combine 两段各需要的 hidden states / token_selected_experts / token_final_scales / 额外 payload，每段都 `pad_up(..., 128)` 对齐。

**（b）dispatch：发出去**

[moe_alltoall.py:279-363](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L279-L363) —— `dispatch` 的核心是 `torch.ops.trtllm.moe_a2a_dispatch`：输入 `token_selected_experts`（每个 token 选了哪些专家）和 `input_payloads`（要发的张量列表），输出 `recv_tensors`（形状 `[ep_size, max_tokens_per_rank, ...]`，即从所有 rank 收到的 token）。它还把状态推到 `"dispatched"`，记录后续 combine 需要的 `local_num_tokens` 与 `combine_payload_offset`。

注意 `runtime_max_tokens_per_rank` 这个参数——它是各 DP rank 本地 batch 的最大 token 数（≤ `max_num_tokens`），用来做 padding 对齐，使所有 rank 收发等长（all-to-all 要求形状对齐）。

**（c）combine：收回来并加权合并**

[moe_alltoall.py:365-413](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L365-L413) —— `combine` 把专家计算后的 `payload`（形状 `[ep_size, max_tokens_per_rank, ...]`）经 `torch.ops.trtllm.moe_a2a_combine` 发回原 rank 并按 `top_k` 权重加权求和，输出形状回到 `[local_num_tokens, ...]`。`payload_in_workspace=True` 时省一次 staging copy（专家输出直接写进 workspace），`use_low_precision_combine=True` 时量化成 FP8 走 NVLink（省一半带宽、保输出精度）。结束后 `reset_state()` 回到 idle。

**（d）数据搬运方向（重点）**

把 dispatch → 专家计算 → combine 串起来看，数据流向是：

```
本地 token (hidden_states)
   │  router 选 top-k 专家
   ▼
dispatch:  本地 ──发往──▶ 各 ep rank（按 token→expert 映射）
                              │
                              ▼ 每个 rank 收到 [ep_size, max_tokens] 个 token
                 各 rank 对「落到本地的专家」跑 GroupGEMM
                              │
                              ▼ 专家输出 [ep_size, max_tokens, hidden]
combine:   各 ep rank ──发回──▶ 本地（原路返回）
   │  按 top-k 权重加权求和
   ▼
本地合并后的 hidden_states（形状回到 [local_num_tokens, hidden])
```

也就是说：**dispatch 把 token 按「专家所在 rank」散出去，combine 把专家输出按「token 来源 rank」聚回来**。两次 all-to-all 方向相反、负载由 router 决定（可能不均衡，这是 MoE 负载均衡 / EPLB 要解决的问题，见 u10-l1）。

**（e）watchdog 与容错**

dispatch/combine 各调一次 `watch_collective`，配合 `AlltoAllWatchdogCoordinator` 检测 all-to-all 是否卡死（某个 rank 没响应）；`ep_group_health` + `active_rank_mask` 还支持「部分 rank 失联」的场景，把失效路由在 kernel 里提前剔除：

[moe_alltoall.py:316-340](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L316-L340) —— `active_rank_mask` 捕获与 `watch_collective` 调用，rank-mask 模式下会在 dispatch 前捕获 committed 成员快照，combine 复用同一 mask，generation 变化则 fail-closed。

#### 4.3.4 代码实践

**实践目标**：追踪一次 MoE all-to-all 在专家并行 forward 中的数据搬运方向，确认 dispatch/combine 的配对关系。

**操作步骤**（源码阅读型实践）：

1. 在 `moe_alltoall.py` 里定位 `_A2AState`（[L29-L36](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L29-L36)），记下 `phase` 字段在 idle/dispatched 之间的迁移点。
2. 看 `dispatch`（[L305](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L305)）和 `combine`（[L388](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L388)）开头那两条 `assert self._state.phase == ...`，体会状态机如何强制配对。
3. 进入 MoE 调用方 [`tensorrt_llm/_torch/modules/fused_moe/communication/nvlink_one_sided.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/communication/nvlink_one_sided.py)，找到它的 `dispatch`（[L412](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/communication/nvlink_one_sided.py#L412)）与 `combine`（[L552](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/fused_moe/communication/nvlink_one_sided.py#L552)），确认它们之间夹着「专家 GroupGEMM」。
4. 画一张时序图：标注本地 token → dispatch → 收到 `[ep_size, max_tokens]` → GroupGEMM → combine → 合并输出，并标出每个箭头是「发往 ep rank」还是「从 ep rank 收回」。

**需要观察的现象**：dispatch 与 combine 严格成对出现，且中间一定有专家计算；两次 all-to-all 的张量形状从 `[local_num_tokens, ...]` 膨胀成 `[ep_size, max_tokens_per_rank, ...]` 再缩回。

**预期结果**：你能复述出「**dispatch 散、combine 聚、中间夹 GroupGEMM**」这条铁律，并解释为何 `phase` 状态机必须强制配对（防止 OOM 后下一次 forward 错乱，见 `reset_state` 的注释 [L415-L423](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/moe_alltoall.py#L415-L423)）。

> 如需真机验证：在多卡环境跑一个 EP>1 的 MoE 模型（如 DeepSeek-V3），用 Nsight Systems 抓 trace，应能看到每层 MoE 有一次 dispatch + 一次 combine 的成对通信，中间夹着 GroupGEMM 计算区间。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MoeAlltoAll` 要用状态机强制 dispatch/combine 配对，而不是提供单个 `all_to_all` 调用？

**参考答案**：因为 dispatch 和 combine 之间必须插入「本地专家计算」（GroupGEMM），这两次通信天然被计算隔开，无法合成一次集合通信。状态机用 `phase` 保证「dispatch 后必须 combine 才能下一次 dispatch」，避免某次 forward 因 OOM 提前退出后，下一次 dispatch 撞上残留的 `dispatched` 状态而断言失败——`reset_state()` 正是为这种异常恢复设计的（见其注释）。

**练习 2**：`use_low_precision_combine=True` 把数据量化成 FP8 走 NVLink，会损失输出精度吗？

**参考答案**：不损失最终输出精度。它只对「**传输中的 combine payload**」做 FP8 量化以省一半 NVLink 带宽，combine kernel 在加权求和时会反量化回原 dtype，输出精度由 `combine` 的返回 dtype 决定（保持 bf16/fp16/fp32）。这是一种「传输有损、输出无损」的带宽优化，前提是中间误差在可接受范围。

**练习 3**：all-to-all 与 AllReduce 在通信模式上最本质的区别是什么？

**参考答案**：AllReduce 是「**全员规约**」——每个 rank 收到的结果相同（replicated）；all-to-all 是「**定向交换**」——每个 rank 发出/收到的数据内容和大小都不同，由路由（token→expert 映射）决定。因此 all-to-all 的负载可能不均衡（某 rank 收到很多 token、某 rank 很少），需要 EPLB 等机制做负载均衡，而 AllReduce 天然均衡。

---

## 5. 综合实践

把本讲三个模块串起来的综合任务：**画出一次 MoE 层前向里「控制面 + 数据面」的全部通信**。

**背景**：假设一个 EP=4、TP=2 的 MoE 模型，一个 decoder layer 的 MoE 部分要完成一次前向。

**任务**：

1. **控制面**：列出这次前向里「可能」经过 `Distributed` 抽象的通信（提示：调度阶段 allgather 各 rank 的 token 数、broadcast 路由配置等），标注它们走 MPI 还是 torch.distributed、传的是对象还是张量。
2. **数据面 - AllReduce**：标出 TP 组内的 AllReduce 发生在哪（提示：MoE 前后各有一次 TP AllReduce，可用 `MoEAllReduce` 融合），并说明 `AUTO` 策略可能落到哪条硬件路径。
3. **数据面 - All-to-All**：画出 EP 组内 `MoeAlltoAll` 的 dispatch → GroupGEMM → combine 三段，标出张量形状变化 `[local_tokens] → [ep_size, max_tokens] → [local_tokens]`，并解释为何这里不能用 AllReduce。
4. **回退链**：如果该机器不支持 MULTIMEM（`symm_mem`），也不支持 MNNVL，追踪 `AllReduce.forward` 最终会落到哪个 op（答案：`tunable_allreduce` 或 `trtllm.allreduce`，即 NCCL/自定义 kernel）。

**交付物**：一张包含「控制面 / AllReduce / All-to-All」三条泳道的时序图，每条泳道标注所用源码文件与行号、通信硬件路径、张量形状。

**预期收获**：完成此任务后，你会清晰地区分三类通信的职责边界，并能针对任意 decoder layer 指出「这步通信用的是哪个类、走哪条硬件路径、为什么是它」。这是阅读 u10-l1（MoE 架构）和 u9 之后进阶内容的前置能力。

## 6. 本讲小结

- **`Distributed` 是控制面抽象**：定下 `broadcast/allgather/allreduce` 及其 TP/CP/PP 分组变体的契约，按 MPI / torch.distributed 分裂成 `MPIDist` / `TorchDist`，用 `lru_cache` 全局单例化；它传的是 Python 对象/小标量，不是大张量。
- **数据面 AllReduce 多策略**：`AllReduce` 模块按「symm_mem → MNNVL → AUTO/NCCL」逐层尝试并回退；`AllReduceStrategy` 从 `NCCL` 到 `SYMM_MEM` 共 10 档，正交叠加 `AllReduceFusionOp` 融合算子。
- **`allreduce_helper` vs `symm_mem`**：前者是自定义 kernel 的 IPC + Lamport 三缓冲工作区管家（覆盖广、支持融合），后者是 MULTIMEM 硬件多播规约（覆盖窄、仅 `NONE`、小张量低延迟）；二者都遵循「初始化探测能力、forward 失败回退」。
- **`MoeAlltoAll` 是定向交换**：dispatch 散、combine 聚、中间夹 GroupGEMM，用 `phase` 状态机强制配对；与 AllReduce 的「全员相同结果」本质不同，负载由 router 决定。
- **DeviceMesh 把拓扑变成 ProcessGroup**：维度从外到内 `pp → (moe_tp/moe_ep 或 tp) → cp`，内层连续便于 NVLink；`pg_utils.split` 支持按 color/key 动分子组。
- **两条铁律**：控制面用 `Distributed`、数据面用 `AllReduce`/`MoeAlltoAll`/`PPComm`；专门硬件路径（MULTIMEM/MNNVL）总是「先试、失败回退到 NCCL」。

## 7. 下一步学习建议

- **u10-l1「MoE 架构与后端」**：本讲的 `MoeAlltoAll` 是 MoE 的通信骨架，u10-l1 会讲 `ConfigurableMoE` 如何把 dispatch/专家计算/combine 编排成完整 forward，并讲 MoE 负载均衡（EPLB）如何缓解本讲提到的「all-to-all 负载不均衡」。
- **u9-l1「Mapping 与并行策略」**（若尚未精读）：本讲的 `tp_group` / `ep_group` / `cp_group_pg` 全部来自 `Mapping`，回去对照 `to_mapping()` 与 `CpType` 会更扎实。
- **继续阅读源码**：
  - [`tensorrt_llm/_torch/distributed/ops.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/ops.py) 的 `allgather` / `reducescatter` / `alltoall_helix`（CP Ulysses/Helix 用）补齐集合通信全家桶。
  - [`tensorrt_llm/_torch/distributed/symm_mem_allgather.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/symm_mem_allgather.py)：与 `symm_mem_allreduce` 对称的 MULTIMEM allgather 实现。
  - [`tests/unittest/_torch/multi_gpu/test_allreduce.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/unittest/_torch/multi_gpu/test_allreduce.py)：`AllReduce` 文档点名的参考实现测试，是验证各策略行为最快的方式。
- **性能方向**：结合 `docs/source/developer-guide/perf-benchmarking.md` 与 Nsight Systems，观察真实 MoE 模型里 dispatch/combine/GroupGEMM 的时间占比，理解通信-计算重叠（overlap）为何是 MoE 性能关键。
