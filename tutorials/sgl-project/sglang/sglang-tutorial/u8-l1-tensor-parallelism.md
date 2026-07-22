# 张量并行与并行状态

## 1. 本讲目标

本讲是「分布式与并行」单元的第一篇。读完后你应该能够：

- 说清楚**张量并行（Tensor Parallelism, TP）**为什么能让一个装不进单卡的大模型跑起来，以及它在一次前向中需要付出怎样的通信代价；
- 看懂 `parallel_state.py` 是如何用「进程组（process group）+ `GroupCoordinator`」这一套抽象把多张 GPU 组织成若干通信子群的；
- 读懂 `communication_op.py` 这层薄包装，以及它背后「自定义 all-reduce（`ca_comm`）」的多路选择策略；
- 理解**融合 collective** 的动机，重点精读本轮（#24651）新增的 `fused_allreduce_rmsnorm_quant_per_group`——它是 ROCm/aiter/gfx95 专用、并在非 AMD 或条件不满足时优雅返回 `None` 让调用方回退的典型案例；
- 理解 `TpModelWorker` 作为「调度器 ↔ GPU 计算」桥梁的角色，以及它与 `ModelRunner` 的分工。

本讲承接 u5-l1（ModelRunner 与前向执行路径）。在 u5-l1 里我们把「一次前向」当作黑盒看了张量怎么进、logits 怎么出；本讲专门拆开前向里**跨 GPU 的那一环通信**。

## 2. 前置知识

### 2.1 什么是张量并行

一个大模型的一个线性层可以写成 \( y = xW^\top + b \)。当权重矩阵 \(W\) 太大、单卡放不下（或单卡算得太慢）时，最直观的做法是把 \(W\) **切开**分到多张卡上：

- **列切分（Column / Colwise）**：把 \(W\) 按输出维度切成 \(N\) 份，每张卡持有 \(W_i\)。每张卡独立算出自己的 \(y_i = xW_i^\top\)，结果拼接（all-gather）或在下一次 all-reduce 时合并。
- **行切分（Row / Rowwise）**：把 \(W\) 按输入维度切成 \(N\) 份，每张卡持有 \(W_i\)，输入 \(x\) 也要相应切分。每张卡算 \(y_i = x_i W_i^\top\)，最后把所有 \(y_i\) **相加（all-reduce sum）**得到完整 \(y\)。

Transformer 里的注意力 `qkv_proj` 通常用 Colwise（输出维度切分），而 MLP 的 `gate_up_proj` 用 Colwise、`down_proj` 用 Rowwise。于是**一个 Transformer block 的结尾必然有一次 all-reduce**——这就是 TP 的通信代价。

### 2.2 几个反复出现的名词

- **rank / world_size**：每个进程的全局编号叫 rank；一组里的进程数叫 world_size（这组里的「世界」大小）。
- **进程组（process group）**：把若干 rank 圈成一个子集，子集内的集合通信（all-reduce / all-gather）不干扰子集外的进程。SGLang 至少有 TP 组、PP 组、attention TP 组、MoE TP/EP 组等好几套。
- **collective（集合通信）**：all-reduce、all-gather、reduce-scatter 这类需要多个 rank 协同的通信原语。
- **NCCL / RCCL / aiter**：NCCL 是 NVIDIA 的集合通信库；ROCm 上对应 RCCL；**aiter** 是 AMD 的推理加速库，提供融合的 all-reduce+RMSNorm 等专用 kernel。
- **gfx95 / gfx942**：AMD GPU 的架构代号。gfx942 是 MI300X/MI325X（CDNA3）；gfx95 是更新的 MX 类型平台。本讲新增的融合 quant 路径**仅 gfx95 可用**。

### 2.3 本讲用到的「配置访问器」背景

u2-l5 讲过，SGLang 运行期配置统一从 `runtime_context` 的命名空间袋读取（`get_parallel()`、`get_exec()`、`get_schedule()` 等）。本讲在讲模型层与 worker 时会用到 `get_parallel()`（读 `tp_size`、`tp_rank` 等并行拓扑），这一点先记住即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/sglang/srt/distributed/parallel_state.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py) | 进程组的创建与组织中枢。`GroupCoordinator` 封装一个通信子群及其多种通信器；`initialize_model_parallel` 按拓扑把 rank 切分成 TP/PP 等组 |
| [python/sglang/srt/distributed/communication_op.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/communication_op.py) | 一层薄薄的「函数包装」。把「找到对应组 + 调用其 collective」封装成 `tensor_model_parallel_all_reduce` 等全局函数，供模型层调用 |
| [python/sglang/srt/layers/communicator.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/communicator.py) | `LayerCommunicator`：每个 decoder layer 持有的通信协调者。负责在 attention/MLP 前后做 scatter/gather、并在条件满足时调用融合 AR+RMSNorm（含本轮新增的融合 quant 路径） |
| [python/sglang/srt/managers/tp_worker.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/managers/tp_worker.py) | `TpModelWorker`：每个 GPU 一个，持有 `ModelRunner`，是 Scheduler 派活、`ModelRunner` 干活之间的「翻译+编排」层 |
| [python/sglang/srt/layers/model_parallel.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/model_parallel.py) | 基于 PyTorch DTensor 的通用 TP 工具（`tensor_parallel`/`apply_torch_tp`），按模块的 `_tp_plan` 自动切分权重 |
| [python/sglang/srt/layers/layernorm.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/layernorm.py) | `RMSNorm` 等。本轮新增的 `forward_with_allreduce_fusion_quant_per_group` 把「融合 AR+RMSNorm+per-group FP8 量化」串起来 |
| [python/sglang/srt/models/qwen3_5.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/models/qwen3_5.py) | Qwen3.5 模型。它把融合 quant 开关（`enable_fused_ar_quant`）传给 `LayerCommunicator`，是本轮融合 quant 的主要服务对象 |

## 4. 核心概念与源码讲解

### 4.1 张量并行的核心思想与 model_parallel 工具

#### 4.1.1 概念说明

TP 的核心问题是：**怎么把一个按单卡写的模型，变成多卡各持一份切片、并在正确的地方做集合通信？**

SGLang 的模型代码（如 `models/llama.py`）绝大多数是**单卡视角**写的——`qkv_proj`、`o_proj` 看起来就是一个普通的 `nn.Linear`。真正的「切分」发生在两处：

1. **权重加载时**：`auto_loader`（见 u5-l4）按 `stacked_params_mapping` 把 HF 的完整权重切成每张卡对应的那一份灌进去；
2. **前向时**：在每个需要通信的地方插入 all-reduce / all-gather。

`model_parallel.py` 提供的是第三种、更「声明式」的工具：基于 PyTorch 原生 DTensor 的 `tensor_parallel`。它**不是** SGLang 的主路径（SGLang 主路径用手写的 `LayerCommunicator` 精细控制），但在某些模型里被 `apply_torch_tp` 用来做一次性的权重切分。

#### 4.1.2 核心流程

`tensor_parallel` 的工作方式是「按计划切分」：

```
对模型里每个子模块：
    读取它的 _tp_plan（一个 {子模块名: "Colwise"|"Rowwise"|...} 字典）
    按计划把该子模块的权重注册成 DTensor（本地只存一片）
    这样该 Linear 的前向就自动带上了正确的通信语义
```

三种切分风格对应：

- `"Colwise"`：权重按输出维切分，前向**不需要**通信（每张卡各算各的输出列），但下游必须 all-gather 或 all-reduce。
- `"Rowwise"`：权重按输入维切分，前向末尾需要一次 all-reduce（把各卡的部分和加起来）。
- `"Colwise_Sharded"`：用于「加载时已经切好并融合」的场景（如 fused qkv），跳过再切分。

#### 4.1.3 源码精读

先看主入口 `tensor_parallel`，它递归遍历模型、对声明了 `_tp_plan` 的模块做切分：

[python/sglang/srt/layers/model_parallel.py:124-158](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/model_parallel.py#L124-L158) —— 这是 `tensor_parallel` 的 `tplize` 内部函数：读取每个子模块的 `_tp_plan`，按 `Colwise`/`Rowwise`/`Colwise_Sharded` 三种风格调用 `parallelize_module`。

特别注意 Rowwise 那一支用的是 SGLang 自定义的 `RowwiseParallelMaybeWait`：

[python/sglang/srt/layers/model_parallel.py:114-121](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/model_parallel.py#L114-L121) —— 它重写了 `_prepare_output_fn`，在 Rowwise 的输出上多加了一次 `torch.distributed._functional_collectives.wait_tensor()`。注释解释：这是为了在「通信流」与「计算流」之间建立 CUDA 依赖，避免 all-reduce 的异步输出被随后的 `RMSNorm` 在还没真正完成时就消费掉。

> 这其实是个性能/正确性的小细节：Rowwise 后的输出是一个 `AsyncCollectiveTensor`（all-reduce 的异步句柄），如果不 wait，紧接着的 RMSNorm 可能在通信尚未结束时就读到未就绪的数据。`MaybeWait` 这个名字正是这个意思。

#### 4.1.4 代码实践

**实践目标**：理解「一个 Linear 是怎么被声明成 TP 切分的」。

**操作步骤**：

1. 在仓库里搜索哪些模型模块声明了 `_tp_plan`：

```
grep -rn "_tp_plan" python/sglang/srt/models/ | head
```

2. 打开 `python/sglang/srt/models/llama.py`，找到 `LlamaAttention` 的 `_tp_plan`，你会看到类似 `{"qkv_proj": "Colwise", "o_proj": "Rowwise", ...}` 的声明。
3. 对照本节，判断：`o_proj` 为什么是 Rowwise？它前向后必须发生什么通信？

**需要观察的现象 / 预期结果**：你能用一句话说清「`qkv_proj` 是 Colwise 所以前向不需要跨卡通信，`o_proj` 是 Rowwise 所以前向末尾需要一次 all-reduce」。

> 说明：SGLang 主路径并不依赖 `apply_torch_tp` 来做运行期通信（那由 `LayerCommunicator` 接管，见 4.4），`model_parallel.py` 更多用于一次性权重切分或与 PyTorch DTensor 生态对接。本实践为**源码阅读型实践**，无需运行 GPU。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `nn.Linear` 被声明为 `"Colwise"` 切分，它的 `weight` 本地 shape 与完整 shape 是什么关系？

**参考答案**：Colwise 按输出维（即 `weight` 的第 0 维，因为 `y = xW^T`）切分，所以本地 `weight` 的第 0 维 = 完整第 0 维 / world_size，第 1 维（输入维）保持不变。

**练习 2**：为什么 `ColwiseParallelSharded` 要单独存在，而不是复用 `ColwiseParallel`？

**参考答案**：因为 fused qkv 这类参数在**权重加载阶段**就已经被切好并融合成一个张量了，`ColwiseParallelSharded._partition_linear_fn` 直接把已切好的本地片当成 `Shard(0)` 注册，**跳过再切分**的步骤，避免把已经切好的权重又切一遍。

---

### 4.2 parallel_state：进程组与 GroupCoordinator

#### 4.2.1 概念说明

`parallel_state.py` 是 SGLang 分布式通信的「户籍管理处」。它解决两个问题：

1. **谁来和谁通信？** —— 用进程组（process group）把 rank 划分成若干子群；
2. **怎么通信得快？** —— 每个 `GroupCoordinator` 同时持有好几种通信器（NCCL、自定义 all-reduce、symmetric memory 等），按 tensor 大小和运行模式挑最快的那个。

`GroupCoordinator` 是这套抽象的核心：**一个 `GroupCoordinator` 实例 = 一个通信子群 + 这个子群里所有可用的通信手段**。

#### 4.2.2 核心流程

启动时的拓扑构建（`initialize_model_parallel`）大致是：

```
读 ServerArgs 里的 tp_size / pp_size / ep_size ...
按 tp_size 把所有 rank 切成若干 TP 组（相邻 tp_size 个 rank 一组）
为每个组创建一个 GroupCoordinator，挂上：
   - device_group（GPU 后端，跑真正的集合通信）
   - cpu_group（gloo 后端，做 CPU 侧协调/广播）
   - pynccl_comm / ca_comm / qr_comm / pymscclpp_comm / torch_symm_mem_comm ...
注册到全局单例（get_tp_group() 取 TP 组）
```

以 8 卡、tp_size=2 为例，文档里画的拓扑是：

```
4 个 TP 组：[g0,g1] [g2,g3] [g4,g5] [g6,g7]
```

每个 TP 组内部共享同一份模型权重的不同切片，组内需要 all-reduce；不同 TP 组之间（数据并行）互不通信。

#### 4.2.3 源码精读

先看 `GroupCoordinator.__init__` 是如何创建进程组的：

[python/sglang/srt/distributed/parallel_state.py:262-385](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L262-L385) —— 构造函数接收 `group_ranks`（一组 rank 列表），对每个 rank 子集调用 `torch.distributed.new_group` 创建 **两个**组：`device_group`（GPU 后端，真正传张量）和 `cpu_group`（gloo 后端，CPU 协调）。`self.rank in ranks` 时记下自己的 `world_size`、`rank_in_group`、两个组句柄。

> 关键设计：**device_group 与 cpu_group 分离**。GPU 集合通信用 device_group，但像权重广播、随机种子同步这种 CPU 侧的协调用 cpu_group（gloo），两者互不阻塞。Mooncake 传输后端则成对创建 `mooncake`+`mooncake-cpu`，逻辑同构。

接着看构造函数里如何挂载多种通信器（ca_comm / qr_comm 等）：

[python/sglang/srt/distributed/parallel_state.py:441-473](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L441-L473) —— 当 `use_custom_allreduce` 且 `world_size > 1` 时，`dispatch_custom_allreduce` 选出具体的自定义 all-reduce 类（`ca_comm`）；若在 AMD 且 `qr_rocm_arch_available()`，再额外建一个 `QuickAllReduce`（`qr_comm`）作为补充。若都失败，仅打 warning 不致命——后续会回退到 NCCL。

再看「用户面」的 `all_reduce`，它体现了「多路通信器择优」：

[python/sglang/srt/distributed/parallel_state.py:621-711](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L621-L711) —— 这是理解 TP 通信最关键的一段。逻辑是按优先级**逐层判优**：

1. `world_size == 1` 直接返回（单卡无需通信）；
2. CPU 张量走共享内存或 gloo；
3. HPU/XPU/NPU 专用通信器优先；
4. 否则在一堆 GPU 通信器里挑：symmetric memory → custom all-reduce（`ca_comm`）→ quick all-reduce（`qr_comm`）→ pymscclpp → torch_symm_mem → pynccl，每种都有 `should_*` 形状/大小门控；
5. 选定一个 `outplace_all_reduce_method` 后走 `_all_reduce_out_place`（非原地），否则 `inplace_all_reduce`。

最后看拓扑构建入口：

[python/sglang/srt/distributed/parallel_state.py:2108-2230](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L2108-L2230) —— `initialize_model_parallel` 计算 `num_tensor_model_parallel_groups = world_size // tp_size`，按 tp_size 步长切出各组 rank 列表，交给 `init_model_parallel_group` 构造 `GroupCoordinator` 并存入全局 `_TP`。docstring（2145-2170 行）用 8 卡的例子画出了 TP/PP/CP/EP 组的划分，值得通读一遍。

#### 4.2.4 代码实践

**实践目标**：看清一次 `--tp 2` 启动时，到底创建了哪些进程组。

**操作步骤**：

1. 用 `--tp 2` 启动一个服务（如能拿到两张 GPU）：

```
python -m sglang.launch_server --model-path <小模型> --tp 2
```

2. 在启动日志里搜索 `[TP]` 或 `GroupCoordinator`，你会看到每个 rank 打印的 `__repr__`（517-522 行定义），形如 `ranks=[0,1] rank=0 local_rank=0 ... world_size=2 rank_in_group=0`。
3. 在 `parallel_state.py:262` 的 `__init__` 里临时加一行日志（**示例代码，仅用于学习，跑完请还原**）：

```python
# 示例代码：仅用于调试，学习后请删除
print(f"[TP-DEBUG] group_name={group_name} ranks={group_ranks} my_rank={self.rank}")
```

4. 观察日志，数一下 `new_group` 被调用了几次、分别对应 TP/PP/attention 哪个组。

**需要观察的现象**：`world_size=1` 的旁路（637 行）会让单卡启动时跳过所有通信；`--tp 2` 时你会看到至少两个 `new_group`（一个 device、一个 cpu）。

**预期结果**：你能画出自己这次启动的进程组拓扑图，标出每个 rank 属于哪个 TP 组。若无多卡环境，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GroupCoordinator` 要同时建 `device_group` 和 `cpu_group` 两个组？

**参考答案**：GPU 集合通信（device_group）和 CPU 侧协调（如随机种子广播、权重元信息同步）走不同的后端，分离后 CPU 协调不会阻塞 GPU 通信、反之亦然；CPU 协调用 gloo（cpu_group）更稳。

**练习 2**：`all_reduce` 方法里，`world_size == 1` 直接 `return input_`（637-638 行）有什么意义？

**参考答案**：这是单卡启动的快速旁路。单卡没有「别人」可通信，all-reduce 语义上等于不变，直接返回原张量省去任何通信开销，也让上层模型代码无需区分单卡/多卡。

---

### 4.3 communication_op 包装与 ca_comm 自定义 all-reduce

#### 4.3.1 概念说明

模型层（如 `communicator.py`、`layernorm.py`）不应该直接去「找 TP 组、调 all-reduce」——那样会到处依赖全局单例，难以测试和复用。`communication_op.py` 提供一层**函数式包装**：把「定位正确的组 + 调用其方法」封装成 `tensor_model_parallel_all_reduce(input)` 这样一句话。

这层包装很薄，但很重要：它让模型代码只面向「语义函数」，不面向「组的细节」。SGLang 同时为 TP 组、attention TP 组、MoE TP/EP 组各提供一组同名函数。

#### 4.3.2 核心流程

每一个包装函数的套路都是：

```
def tensor_model_parallel_all_reduce(input_):
    return get_tp_group().all_reduce(input_)
```

即「取 TP 组 → 调同名小写方法」。底层真正干活的是 `GroupCoordinator.all_reduce`（上一节 4.2 看过的多路择优），而它内部又会根据 tensor 形状决定走 `ca_comm`（自定义 all-reduce）还是 NCCL 等。

**自定义 all-reduce（ca_comm）** 是 SGLang 的一个性能关键点：NCCL 是通用但偏重的库通信；当 tensor 较小、GPU 间又同处一个节点时，用基于共享内存/寄存器的自研 kernel（custom all-reduce）能省掉 NCCL 的调度开销，显著降低小消息延迟——这对 decode 阶段（每步只 all-reduce 一个很小的 hidden state）尤其重要。

#### 4.3.3 源码精读

看 `communication_op.py` 的函数族，体会「语义函数 = 定位组 + 调方法」的统一模式：

[python/sglang/srt/distributed/communication_op.py:18-40](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/communication_op.py#L18-L40) —— 三个核心包装：`tensor_model_parallel_all_reduce`、`tensor_model_parallel_quant_all_reduce`、`tensor_model_parallel_fused_allreduce_rmsnorm`。注意第三个返回 `Optional[Tuple[...]`——它可能返回 `None`，调用方必须处理回退。

再看 attention/MoE 族的对称包装：

[python/sglang/srt/distributed/communication_op.py:88-107](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/communication_op.py#L88-L107) —— `attention_tensor_model_parallel_all_reduce` 走 `get_attn_tp_group()`，`moe_tensor_model_parallel_all_reduce` 走 `get_moe_tp_group()`，`moe_expert_parallel_all_reduce` 走 `get_moe_ep_group()`。**同名语义、不同目标组**，这正是这层包装的价值。

回到 ca_comm 的择优逻辑（在 4.2.3 看过的 `all_reduce` 里）：

[python/sglang/srt/distributed/parallel_state.py:678-708](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L678-L708) —— 这里体现了 ca_comm 的「形状门控」：只有当 `ca_comm.should_custom_ar(input_)` 通过（tensor 够小、形状受支持）时才选 `"ca"`，否则降级到 `qr`/`pymscclpp`/`torch_symm_mem`/`pynccl`。最终通过 `outplace_all_reduce`（859 行的 `_all_reduce_out_place`）派发到具体通信器的 `custom_all_reduce`。

> 直觉：NCCL 对大消息吞吐高，但每调一次有固定开销；ca_comm 用共享内存做小消息 all-reduce，延迟更低。所以「小消息走 ca_comm，大消息走 NCCL」就是 `should_custom_ar` 背后的取舍。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 decode 前向中 all-reduce 的「择优」过程。

**操作步骤**：

1. 在 `parallel_state.py:703` 的 `if outplace_all_reduce_method is not None:` 分支前，临时加日志（**示例代码，学习后删除**）：

```python
# 示例代码：仅用于学习，跑完请还原
print(f"[AR-DEBUG] method={outplace_all_reduce_method} "
      f"shape={tuple(input_.shape)} bytes={input_.numel()*input_.element_size()}")
```

2. 用 `--tp 2` 启动服务并发若干请求。
3. 观察 decode 阶段（小 batch、小 hidden）和 prefill 阶段（大 batch）分别选了哪种 method。

**需要观察的现象**：小消息（如单 token decode 的 hidden state）倾向于 `ca`；大 prefill 批可能因为超过 ca_comm 的形状上限而落到 `pynccl`/`inplace`。

**预期结果**：你能说出「decode 走自定义 all-reduce 是为了低延迟」。若无法运行，标注「待本地验证」，并改为源码阅读：在 `_all_reduce_out_place`（859 行起）里逐一阅读每个 method 分支调用的通信器方法。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在模型层写 `get_tp_group().all_reduce(x)`，而要套一层 `tensor_model_parallel_all_reduce(x)`？

**参考答案**：解耦与可测试性。模型层只依赖语义函数，不直接耦合全局单例的取法；同时为 TP/attn-TP/MoE-TP/MoE-EP 提供对称的同名函数，模型代码切换通信组只需换函数名。

**练习 2**：ca_comm（自定义 all-reduce）相比 NCCL，在什么场景下有优势、什么场景下反而更差？

**参考答案**：小消息、节点内（共享内存可达）时 ca_comm 延迟更低，适合 decode；但大消息受限于共享内存带宽和注册张量开销，`should_custom_ar` 会判否，此时回退到 NCCL（吞吐更高）更优。

---

### 4.4 融合 collective：fused_allreduce_rmsnorm 与本轮新增的 per-group 量化融合

#### 4.4.1 概念说明

一个 Transformer block 的结尾是这样的计算：

\[
\text{hidden} = \text{RMSNorm}(\text{all\_reduce}(y_{\text{partial}}) + \text{residual})
\]

如果按朴素做法，这是**三个独立的 kernel/通信**：① all-reduce 跨卡求和 → ② 加 residual → ③ RMSNorm。每一步都有 kernel launch 和中间张量的显存读写开销。

**融合 collective** 的想法是：既然这三步的数据依赖是线性的、且都作用在同一个 tensor 上，能不能用**一个 kernel** 把「all-reduce + 加残差 + RMSNorm」一口气做完？答案是可以，尤其在 AMD ROCm 上，aiter 库提供了 `custom_fused_ar_rms` 这类融合 kernel，能省下多次 launch 和中间显存往返。

本轮提交 #24651 进一步把**量化**也融进来。Qwen3.5 FP8 模型在 RMSNorm 之后还要做一次 per-group（每 128 个通道一组）FP8 量化，把 bf16 的 norm 结果转成 `(fp8_tensor, scale)` 交给后面的 FP8 线性层。于是新的融合变成：

\[
\text{all\_reduce} \rightarrow +\text{residual} \rightarrow \text{RMSNorm} \rightarrow \text{per-group FP8 quant}
\]

融合成一个 kernel（`custom_fused_ar_rms_per_group_quant`），理想情况下从「3～4 次 launch」降到「1 次」。**但这条路径只在新款 AMD gfx95 平台上可用**，其它平台必须优雅回退。

#### 4.4.2 核心流程

整条「融合 AR+RMSNorm+quant」的调用链分四层，每层都可能返回 `None` 让上层回退：

```
模型层 LayerCommunicator.prepare_attn
   │  (enable_fused_ar_quant 且 _use_aiter 时)
   ▼
layernorm.RMSNorm.forward_with_allreduce_fusion_quant_per_group
   │  调 tensor_model_parallel_fused_allreduce_rmsnorm_quant_per_group
   ▼
communication_op 包装函数  ──►  get_tp_group().fused_allreduce_rmsnorm_quant_per_group
   ▼
GroupCoordinator.fused_allreduce_rmsnorm_quant_per_group
   │  逐道关卡判否就 return None：
   │   1. 必须 is_hip() 且 is_gfx95_supported()
   │   2. ca_comm 存在且未 disabled
   │   3. ca_comm 有 custom_fused_ar_rms_per_group_quant 方法
   │   4. 形状/大小合格（K 整除 group_size、K≤16384、字节上限、world_size≠6…）
   ▼
ca_comm.custom_fused_ar_rms_per_group_quant   ← 真正的 gfx95 融合 kernel
```

**任何一道关卡不满足，`GroupCoordinator` 这一层就返回 `None`**；`layernorm` 那一层收到 `None` 后会回退到「fused AR+RMSNorm + 单独的 per-group quant」两步路径；如果连两步的 fused AR+RMSNorm 也返回 `None`，`LayerCommunicator` 会进一步回退到「普通 all-reduce + 普通 RMSNorm」。**这种「逐层 return None、逐层回退」是本节最重要的设计范式。**

#### 4.4.3 源码精读

**第一层：GroupCoordinator 的融合 quant 入口**（本轮新增重点）：

[python/sglang/srt/distributed/parallel_state.py:788-857](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L788-L857) —— `fused_allreduce_rmsnorm_quant_per_group`。逐行看它的「四道关卡」：

- 810 行：`if not (is_hip() and is_gfx95_supported()): return None`——**非 AMD 或非 gfx95 直接返回 None**，这是本讲实践任务要求定位的关键点；
- 813-817 行：`ca_comm` 必须存在、未 disabled、且**具备** `custom_fused_ar_rms_per_group_quant` 方法（用 `hasattr` 探测，不存在的通信器直接 None）；
- 819-828 行：形状门控——`K % group_size != 0` 或 `K > 16384`、总字节数超上限、`world_size == 6` 等都判否；
- 830-844 行：1-stage vs 2-stage kernel 的选择，参考 `SGLANG_USE_1STAGE_ALLREDUCE` 环境变量，并对 TP=8、K 在 (4096,7168] 的 graph-replay 交叉点做了特判；
- 846-857 行：`try/except` 包住真正的 kernel 调用，**任何异常都吞掉返回 None**，让调用方回退。

对比一下「已有的、不带 quant 的」融合 AR+RMSNorm，体会两段代码的同构：

[python/sglang/srt/distributed/parallel_state.py:727-786](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L727-L786) —— `fused_allreduce_rmsnorm`。它没有 gfx95 平台门控（735-736 行只检查 ca_comm），路径更宽：先试 `ca_comm.fused_allreduce_rmsnorm`（通信器原生融合 API），失败再走 `custom_fused_ar_rms`，并对 piecewise CUDA Graph 场景（764-778 行）单独走 `fused_ar_rms`。这两段是「同一家族」的融合 collective。

**第二层：communication_op 的包装函数**：

[python/sglang/srt/distributed/communication_op.py:43-63](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/communication_op.py#L43-L63) —— `tensor_model_parallel_fused_allreduce_rmsnorm_quant_per_group`。docstring 写得很明确：默认返回 `(fp8_output, residual_out, per_group_scale)`；`emit_bf16=True` 时多带一个 bf16 输出（给 GDN 层用，下文解释）；**`None` 表示后端无法服务，调用方必须回退**。

**第三层：layernorm 的串联**：

[python/sglang/srt/layers/layernorm.py:229-350](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/layernorm.py#L229-L350) —— 模块级 `_forward_with_allreduce_fusion_quant_per_group`。它体现了「双层回退」：

- 290-298 行（`keep_bf16=False`，普通注意力层）：先试完全融合的 quant kernel；若 `result is not None` 就用之；否则（300-315 行）回退到 `fused_allreduce_rmsnorm`（只融合 AR+RMSNorm）+ 单独的 `_get_aiter_per_group_quant()`（再单独量化），仍比 baseline 少一次 launch。
- 317-350 行（`keep_bf16=True`，GDN 层）：GDN 风格的层**同时**需要一个 bf16 输出（给 `in_proj_ba` 小门控投影）和一个 `(fp8, scale)`（给 `in_proj_qkvz`）。理想路径用 `emit_bf16=True` 让融合 kernel 一次产出两者；回退路径再单独量化。

`RMSNorm` 方法只是个转发：

[python/sglang/srt/layers/layernorm.py:749-766](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/layernorm.py#L749-L766) —— `forward_with_allreduce_fusion_quant_per_group` 转发给上面那个模块级函数，返回 `((fp8, scale), residual)` 或 `((bf16, fp8, scale), residual)` 或 `None`。

**第四层：LayerCommunicator 的入口与开关**：

[python/sglang/srt/layers/communicator.py:586-611](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/communicator.py#L586-L611) —— `prepare_attn` 里的这段是融合 quant 的触发点。当 `self.enable_fused_ar_quant` 且 `_use_aiter` 且 layernorm 具备 `forward_with_allreduce_fusion_quant_per_group` 时，尝试融合 quant；若 `quant_result is not None` 就用之，否则退回普通的 `forward_with_allreduce_fusion`（AR+RMSNorm）。

开关本身是构造时传入的两个字段：

[python/sglang/srt/layers/communicator.py:460-461](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/communicator.py#L460-L461) —— 本轮新增的 `enable_fused_ar_quant`、`fused_ar_quant_keep_bf16` 两个构造参数（470-471 行存为属性）。

谁设置这个开关？Qwen3.5 模型：

[python/sglang/srt/models/qwen3_5.py:159-181](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/models/qwen3_5.py#L159-L181) —— `_enable_qwen35_fused_ar_quant()`：必须 `_use_aiter`（即 `SGLANG_USE_AITER` 且 HIP）、未被 `SGLANG_DISABLE_FUSED_AR_QUANT` 关闭、且 `--enable-aiter-allreduce-fusion` 已开。注释（167-172 行）说得很清楚：开了它**永远不会让 AR+RMSNorm 融合变差**，因为最坏情况就是回退到普通 AR+RMSNorm。

最后，平台门控 `is_gfx95_supported` 的实现：

[python/sglang/srt/utils/common.py:1012-1020](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/utils/common.py#L1012-L1020) —— 非 HIP 直接返回 `False`；HIP 下读取 GPU 的 `gcnArchName`，匹配是否含 `"gfx95"`。这就是「gfx95-only」的最终判据。

#### 4.4.4 代码实践

**实践目标**：亲手定位 `fused_allreduce_rmsnorm_quant_per_group` 在「非 AMD」或「ca_comm 不可用」时如何返回 `None`，并追踪回退路径。

**操作步骤**：

1. 打开 [parallel_state.py:810](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/distributed/parallel_state.py#L810)。这一行 `if not (is_hip() and is_gfx95_supported()): return None` 就是本讲实践任务要你找的「非 AMD 第一道关卡」。
2. 继续往下读 813-817 行：`ca_comm is None`、`ca_comm.disabled`、`hasattr(ca_comm, "custom_fused_ar_rms_per_group_quant")` 为假时也 `return None`——这是「ca_comm 不可用」的第二道关卡。
3. 打开 [layernorm.py:300-315](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/layernorm.py#L300-L315)，确认：当 `GroupCoordinator` 返回 `None`，layernorm 这层会改调 `tensor_model_parallel_fused_allreduce_rmsnorm`（只融 AR+RMSNorm），拿到 `bf16_out` 后再单独调 `_get_aiter_per_group_quant()` 做量化。这就是「分离的 fused-AR-RMSNorm + per-group-quant」回退路径。
4. 再看 [communicator.py:604-611](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/communicator.py#L604-L611)：若连 layernorm 的融合 quant 返回 `None`，`LayerCommunicator` 会退回 `forward_with_allreduce_fusion`（纯 AR+RMSNorm）。

**需要观察的现象**：你能在一张图上画出「4 层调用、3 级回退」的全貌：gfx95 单 kernel → 2-kernel（fused AR+RMSNorm + 单独 quant）→ 纯 AR+RMSNorm。

**预期结果**：用一句话回答实践任务——「`fused_allreduce_rmsnorm_quant_per_group` 在 `not (is_hip() and is_gfx95_supported())`（810 行）或 ca_comm 缺失/无该方法（813-817 行）时返回 `None`，调用方据此回退到 `fused_allreduce_rmsnorm` + 单独 per-group quant」。

> 这是一个**纯源码阅读型实践**（融合 quant 路径需要 AMD gfx95 硬件才能真跑），重点是把「逐层 None、逐层回退」的设计读通。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fused_allreduce_rmsnorm_quant_per_group` 的真正 kernel 调用要包在 `try/except` 里、且 except 里直接 `return None`（846-857 行）？

**参考答案**：因为这是「尽力融合」的快路径。哪怕所有前置门控都通过，运行期仍可能因 aiter 内部的形状/对齐限制抛异常；吞掉异常并返回 `None`，让上层透明地回退到「fused AR+RMSNorm + 单独 quant」，保证功能正确性永远优先于性能优化，不会因为融合失败而崩服务。

**练习 2**：`emit_bf16=True`（即 `keep_bf16=True`）那条路径是为了解决什么问题？

**参考答案**：GDN 风格的层（如 Qwen3.5 的注意力门控）有**两个**投影同时消费同一个 norm 输出：`in_proj_qkvz` 要 FP8 的 `(fp8, scale)`，`in_proj_ba` 要未量化的 bf16。如果融合 kernel 只产出 FP8，`in_proj_ba` 就得反向 dequant，有精度损失。`emit_bf16=True` 让融合 kernel **同一次**写出 FP8 和量化前的 bf16，避免额外 kernel 和精度损失。

**练习 3**：开关 `enable_fused_ar_quant` 由谁、依据什么打开？

**参考答案**：由 Qwen3.5 模型层（`qwen3_5.py:758-761`、`975-977`）依据 `_enable_qwen35_fused_ar_quant()`（须 `_use_aiter` 且开了 `--enable-aiter-allreduce-fusion` 且未被 `SGLANG_DISABLE_FUSED_AR_QUANT` 关闭）和 `_linear_accepts_fp8_tuple`（下游线性层确实能吃 FP8 tuple）共同决定，再传给 `LayerCommunicator`。

---

### 4.5 TpModelWorker：调度器与 GPU 计算的桥梁

#### 4.5.1 概念说明

回顾 u2-l1/u3-l1：SGLang 是多进程架构，Scheduler 是「每张 GPU 一个」的进程级单例，负责调度（决定算什么）。但 Scheduler 不直接碰 PyTorch 模型——中间隔着一层 `TpModelWorker`（每张 GPU 一个）。

`TpModelWorker` 的角色是：

- **持有** `ModelRunner`（u5-l1 讲过的「真正跑前向」的对象）、模型配置、tokenizer/processor、PP/World 进程组等；
- **暴露**一个统一的 `forward_batch_generation` 给 Scheduler 调用，把「调度批（ScheduleBatch）」翻译成「前向批（ForwardBatch）」、跑前向、再按场景编排采样；
- 还承担一堆**非前向**的运维操作：权重热更新（RL 场景常用）、LoRA 增删、内存池分配、CUDA Graph 捕获等。

为什么叫「Tp」ModelWorker？因为它「住在一张 GPU 上」，天然属于某个 TP rank，构造时接收 `ParallelState`（`ps`）以知晓自己的 tp_rank/tp_size。

#### 4.5.2 核心流程

`TpModelWorker.forward_batch_generation` 的核心逻辑（u5-l1 已详述，这里聚焦它和 TP 的关系）：

```
1. 若给了 ScheduleBatch：
   - ForwardBatch.init_new(batch, model_runner)  把调度批翻译成前向批
2. 跑前向：self.model_runner.forward(forward_batch)
3. 按位置分支：
   - pp_group.is_last_rank（流水线最后一张卡）：
       * 取 logits_output
       * 按 is_verify / overlap+文法 / prefill-only 决定是否/何时采样
       * 返回 GenerationBatchResult（带 next_token_ids）
   - 否则（流水线中间卡）：
       * 返回 pp_hidden_states_proxy_tensors（交给下一阶段，u9 会讲）
```

前向**内部**（`ModelRunner.forward` → 模型各层 → `LayerCommunicator`）才会发生 4.2/4.3/4.4 讲的那些跨卡通信。所以 `TpModelWorker` 本身不直接调 all-reduce，但它**驱动**了会触发 all-reduce的整条前向。

#### 4.5.3 源码精读

看构造函数，理解它持有哪些 TP 相关状态：

[python/sglang/srt/managers/tp_worker.py:274-360](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/managers/tp_worker.py#L274-L360) —— `TpModelWorker.__init__`。注意几个 TP 要点：

- 接收 `ps: ParallelState`（282 行），保存为 `self.ps`；
- 316-338 行：只有非 draft worker 且非 skip_tokenizer_init 时才建 tokenizer/processor；
- 342-343 行：取 `get_pp_group()`、`get_world_group()`；
- 345-356 行：**跨 TP worker 同步随机种子**——用 `broadcast_pyobj` 把 `server_args.random_seed` 在 world group 上广播，保证所有 TP rank 采样一致。这是 TP 正确性的隐含要求（各卡若用不同种子采样，结果会发散）。

看主入口 `forward_batch_generation`：

[python/sglang/srt/managers/tp_worker.py:530-630](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/managers/tp_worker.py#L530-L630) —— 正是上面流程图对应的代码。545-550 行做 ScheduleBatch→ForwardBatch 翻译；564 行 `if self.pp_group.is_last_rank:` 分流「最后一张卡要采样、中间卡只传 proxy」；582-595 行处理 overlap+文法场景的**延迟采样**（把采样函数挂成 `delay_sample_func`，让调度器在合适的时机再执行，见 u3-l4 的 CPU-GPU 重叠）；597-601 行是普通请求的采样。

> 这里的 `enable_overlap`、`enable_spec` 都来自 `server_args`（358-359 行）。注意本文件对配置的读取处于**迁移过渡期**：`_init_model_config`（403-420 行）已用 `get_model()`/`get_spec()` 命名空间袋，而构造函数里 `server_args.disable_overlap_schedule` 等仍直接读 `server_args`——这正是 u2-l5 提到的「server_args 退化为只读 RAW 留档、逐步迁向命名空间袋」在 TP 这层的体现。

`BaseTpWorker` 还定义了一堆运维抽象方法，理解 TP worker 的完整职责：

[python/sglang/srt/managers/tp_worker.py:109-262](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/managers/tp_worker.py#L109-L262) —— 权重从磁盘/分布式/tensor/IPC 更新（RL 热更新，u12-l3 会用）、LoRA 增删、权重导出等。这些方法大多转调 `self.model_runner.weight_updater/exporter`，体现 worker 的「薄编排」定位。

#### 4.5.4 代码实践

**实践目标**：把「调度器 → TpModelWorker → ModelRunner → all-reduce」这条链在源码里串起来。

**操作步骤**：

1. 从 `scheduler.py` 找到它调用 worker 的地方（搜 `worker.forward_batch_generation` 或 `tp_worker`），确认 Scheduler 持有 `TpModelWorker`。
2. 跟到 [tp_worker.py:530](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/managers/tp_worker.py#L530)，看 `forward_batch_generation` 如何调 `self.model_runner.forward`（565 行）。
3. 再跟到 `model_runner.py` 的 `forward`（u5-l1），最终进入模型各层，每层末尾的 `LayerCommunicator` 触发 all-reduce（4.2/4.3）。
4. 画一张调用栈图：`Scheduler.event_loop → worker.forward_batch_generation → ModelRunner.forward → LlamaDecoderLayer → LayerCommunicator.prepare_attn → ... → GroupCoordinator.all_reduce`。

**需要观察的现象**：你会清楚看到「all-reduce 不是凭空发生的，而是嵌在每一个 decoder layer 的通信协调者里」。

**预期结果**：能在图上标出一次 decode 迭代里，all-reduce 发生在**每个 Transformer block 的 attention 之后和 MLP 之后**（Rowwise 投影的必然产物）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `TpModelWorker.__init__` 要在 world group 上广播随机种子（345-356 行）？

**参考答案**：TP 下各卡看到的是同一个请求、各自的权重切片，前向的 all-reduce 让 hidden state 一致；但采样（multinomial）如果各卡用不同随机种子，会采到不同 token，导致结果发散。广播种子保证所有 TP rank 采样一致，于是「最后一张卡」采出的 token 对所有 rank 都相同。

**练习 2**：`forward_batch_generation` 里 `pp_group.is_last_rank` 的真假，分别返回什么？为什么不同？

**参考答案**：`is_last_rank=True`（流水线最后一张卡）才有完整 logits，返回带 `next_token_ids` 的 `GenerationBatchResult`；`is_last_rank=False`（中间卡）不产出 logits，而是把 hidden states 包成 `pp_hidden_states_proxy_tensors` 传给下一阶段（PP 把模型按层切到不同卡，中间卡只产出中间表示）。这是 PP 与 TP 协同的结果，u9 会展开。

---

## 5. 综合实践

把本讲的四个层面串成一个端到端的「TP 前向通信全图」任务。

**任务**：用 `--tp 2` 启动一个你能跑起来的小模型（若无可降级为纯源码阅读），完成下面这张「TP 通信档案表」：

| 层面 | 关键函数 / 类 | 文件:行 | 它做什么 | 失败/不满足时怎么办 |
|------|--------------|---------|----------|---------------------|
| 拓扑构建 | `initialize_model_parallel` | parallel_state.py:2108 | 按 tp_size 切进程组 | — |
| 组协调者 | `GroupCoordinator.__init__` | parallel_state.py:262 | 建 device/cpu 组 + 挂通信器 | — |
| 用户面 all-reduce | `GroupCoordinator.all_reduce` | parallel_state.py:621 | 多路择优通信 | world_size==1 直接返回 |
| 语义包装 | `tensor_model_parallel_all_reduce` | communication_op.py:18 | 定位 TP 组+调方法 | — |
| 融合 AR+RMSNorm | `fused_allreduce_rmsnorm` | parallel_state.py:727 | 融合通信+归一化 | ca_comm 不可用→None |
| 融合 AR+RMSNorm+quant（新） | `fused_allreduce_rmsnorm_quant_per_group` | parallel_state.py:788 | gfx95 单 kernel 融合量化 | 非 gfx95/ca_comm 无→None→回退 |
| 层级协调者 | `LayerCommunicator.prepare_attn` | communicator.py:561 | 触发融合 quant | quant_result None→退回 AR+RMSNorm |
| Worker 编排 | `TpModelWorker.forward_batch_generation` | tp_worker.py:530 | 翻译批+前向+采样 | — |

**进阶子任务**：

1. 在日志里确认你这次启动创建了几个进程组、`ca_comm` 是否成功初始化。
2. 定位本轮新增的 `fused_allreduce_rmsnorm_quant_per_group`，用一句话说明它在你的硬件上**会不会**被真正执行（关键看 `is_gfx95_supported()` 的结果），并说出它返回 `None` 后调用链如何逐层回退。
3. 对照 [qwen3_5.py:159-181](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/models/qwen3_5.py#L159-L181)，说清「想让融合 quant 真正生效」需要同时满足哪些条件（平台、环境变量、CLI 开关、下游线性层能力）。

> 如果没有 AMD gfx95 硬件，融合 quant 路径永远走不到真正的单 kernel，但**回退路径保证功能正确**——这正是本讲反复强调的「逐层 None、逐层回退」设计意图。请把这一点写进你的档案表备注。

## 6. 本讲小结

- **张量并行**通过 Colwise/Rowwise 切分权重，让大模型跨多卡；代价是每个 Transformer block 末尾必须有一次 all-reduce。`model_parallel.py` 的 `tensor_parallel` 提供了基于 DTensor 的声明式切分工具。
- **`parallel_state.py` 是分布式户籍管理处**：`initialize_model_parallel` 按 tp_size 切进程组，`GroupCoordinator` 封装一个子群及其多种通信器，`all_reduce` 在 ca_comm/qr_comm/pymscclpp/symm_mem/pynccl 之间按形状择优。
- **`communication_op.py` 是一层薄包装**，把「定位组+调方法」封装成 `tensor_model_parallel_all_reduce` 等语义函数，让模型层不直接耦合全局单例；自定义 all-reduce（ca_comm）专为小消息低延迟设计。
- **融合 collective** 把 all-reduce+加残差+RMSNorm（乃至 per-group FP8 量化）融进一个 kernel，省下多次 launch。本轮新增的 `fused_allreduce_rmsnorm_quant_per_group` 是 **ROCm/aiter/gfx95 专用**，其设计范式是「逐层 return None、逐层回退」：非 AMD（810 行）或 ca_comm 不可用（813-817 行）即返回 `None`，调用方退到 fused-AR-RMSNorm + 单独 quant，再不行退到纯 AR+RMSNorm。
- **`TpModelWorker`** 是调度器与 `ModelRunner` 之间的桥梁：持有 ModelRunner、翻译批、编排采样、并承担权重热更新/LoRA 等运维；它本身不直接 all-reduce，但驱动了会触发 all-reduce 的整条前向；构造时跨 TP worker 广播随机种子保证采样一致。
- 本讲多处可见**配置访问迁移过渡期**：`tp_worker.py` 部分读 `get_model()/get_spec()` 命名空间袋、部分仍直读只读 `server_args`，呼应 u2-l5 的「server_args 退化为只读 RAW 留档」。

## 7. 下一步学习建议

- **u8-l2 数据并行与 DP 控制器**：当一组 TP 不够、想复制多组 TP 提升吞吐时，看 `DataParallelController` 如何在多组 TP worker 间路由与负载均衡（cache-aware），以及 `dp_attention.py` 如何让 attention 维度做数据并行。
- **u8-l3 流水线/专家并行与 MoE**：本讲提到的 `pp_group.is_last_rank` 分流、`get_moe_tp_group/get_moe_ep_group` 将在那里展开；MoE 的 token 分发与 top-k 路由是 TP 之外的另一套并行通信。
- **回顾 u5-l1 / u5-l5**：把本讲的「通信」嵌回 `ModelRunner.forward` 与 `LlamaDecoderLayer` 的层结构里，你会看到 `LayerCommunicator` 正是「层」与「跨卡通信」的接缝。
- **延伸阅读源码**：想深挖融合 kernel 的可看 `python/sglang/srt/distributed/device_communicators/` 下各通信器实现（如 custom all-reduce、QuickAllReduce），以及 `test/sglang/perf/mi35x/test_qwen35_fp8_ar_fusion_mi35x.py`——它是本轮融合 quant 的性能验收测试，能帮你理解 gfx95 上的实测收益。
