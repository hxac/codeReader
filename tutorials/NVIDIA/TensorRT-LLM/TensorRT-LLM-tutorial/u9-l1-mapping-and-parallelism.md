# Mapping 与并行策略

> 本讲属于「进阶层 · 分布式与并行（u9）」单元的第 1 讲，承接 [u4-l1 TorchLlmArgs 与配置层级](u4-l1-llm-args-hierarchy.md)。
> 在 u4-l1 里我们见过 `_ParallelConfig` 和它那个只读 property `parallel_config`，但刻意没展开「它到底描述了什么、又去了哪里」。本讲就把这个配置对象翻译成运行时真正干活的东西——`Mapping`。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出大模型推理**为什么**要并行，以及 TensorRT-LLM 支持**哪几种**并行（TP / PP / CP / EP / DP / Wide-EP），并区分它们各自适合解决「显存放不下」还是「算得不够快」。
2. 看懂 [`Mapping`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py) 这个类如何用**一个对象**同时描述「整个集群的并行拓扑」和「我这个 rank 在拓扑里的坐标」，并能手算给定 `tp_size`/`pp_size`/`cp_size` 下每个 rank 的角色。
3. 复述从用户侧扁平字段（`tensor_parallel_size` 等）→ `_ParallelConfig` 聚合 → `to_mapping()` 产出 `Mapping` → 被各模块消费的完整链路，并指出代码里 `Mapping` 被读取的几个关键位置。
4. 理解 `CpType`（ULYSSES / STAR / RING / HELIX）等枚举的含义，以及 MoE / Attention 子并行度在缺省时如何被**自动推导**。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的以下概念，这里只做最小回顾：

- **`TorchLlmArgs` 是一个巨型 Pydantic 配置对象**，贯彻「一次配置、全程生效」。它把高频旋钮摊平到顶层（如 `tensor_parallel_size`），把成族参数打包成子配置。
- **`StrictBaseModel` + `extra="forbid"`**：拼写错误立即报错。
- **`model_validator(mode="after")`**：构造完成后触发的校验/聚合钩子。u4-l1 里它把扁平的并行字段聚合成私有 `_parallel_config`，再以只读 property 暴露。

此外，你需要两个分布式推理的基础直觉：

| 概念 | 一句话解释 |
|------|-----------|
| **rank** | 一个参与分布式计算的进程（通常绑一张 GPU）的编号，从 0 开始。 |
| **process group（通信组）** | 一组需要互相通信的 rank。TP 组内的 rank 要做 all-reduce，PP 组内的 rank 要前后传 activation。 |

并行的本质就是：**把「一张卡干不完的活」拆给多张卡，再约定好谁和谁通信**。`Mapping` 就是这份「分工 + 通信约定」的契约。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/mapping.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py) | **本讲主角**。`Mapping` 类及其基类 `MappingBase`、两套拓扑实现 `MpiTopology` / `DeviceMeshTopology`、`CpType` 枚举。一个对象装下整个并行拓扑。 |
| [docs/source/features/parallel-strategy.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md) | 官方并行策略文档：六种并行的动机、模块级（Attention / FFN / MoE）策略选择，以及 `trtllm-serve` 的 YAML 配置示例。 |
| [tensorrt_llm/llmapi/llm_args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py) | 用户侧配置入口：扁平并行字段、`_ParallelConfig` 子配置、`to_mapping()`、`validate_parallel_config` 聚合钩子、`CpConfig`。 |
| [tensorrt_llm/llmapi/llm_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_utils.py) | `ModelLoader` 在加载期调用 `parallel_config.to_mapping()` 拿到 `Mapping`（L65），是配置→运行时的第一座桥。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor_creator.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py) | 执行器装配入口：每个 worker 进程都用自己的 rank 重新构造一份 `Mapping`（`_get_mapping`，L197），再分发给模型引擎与通信层。 |
| [tensorrt_llm/_torch/model_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py) | `ModelConfig` 携带一个 `mapping: Mapping` 字段（L134），让模型前向随时能查「我该切哪些权重、属于哪个 PP 段」。 |
| [tensorrt_llm/_torch/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py) | 模型侧消费 `Mapping` 的典型场景：`__pp_init__` 用 `mapping.pp_layers(...)` 决定本 rank 保留哪几层（L283–L323）。 |
| [tensorrt_llm/_torch/device_mesh.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py) | `DeviceMeshTopologyImpl`：基于 PyTorch `DeviceMesh` 的拓扑实现，`build_mesh()` 按固定维度顺序建通信组（L109–L146）。 |

> 💡 **关于两种拓扑实现**：`Mapping.__new__` 会在 `MpiTopology`（MPI 编排）和 `DeviceMeshTopology`（Ray 编排，PyTorch 后端默认）之间二选一（[mapping.py:L549-L553](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L549-L553)）。两者对外暴露的属性（`tp_rank`、`tp_group` 等）同名，差别只在通信组怎么建出来——这点会在 4.2 展开。

## 4. 核心概念与源码讲解

### 4.1 并行拓扑全景：为什么要并行，有哪些并行

#### 4.1.1 概念说明

单张 GPU 跑大模型推理，迟早撞上两堵墙之一：

- **显存墙**：模型权重 + KV cache 放不下。例：一个 70B 模型 bf16 权重就要 ~140 GB，单张 H100（80 GB）装不下。
- **算力墙**：放得下但太慢。例：高并发下单卡吞吐打不满业务需求。

分布式并行的目的就是翻越这两堵墙。TensorRT-LLM 支持**六种**并行策略，官方文档 [parallel-strategy.md:L7-L13](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L7-L13) 一句话各表：

| 并行 | 全称 | 切的是什么 | 主要解决 |
|------|------|-----------|---------|
| **TP** | Tensor Parallel | 把**权重矩阵**切开，每卡持有一部分 | 显存 + 小 batch 算力 |
| **PP** | Pipeline Parallel | 把**层**切到不同卡，activation 在卡间流动 | 显存（超大模型） |
| **CP** | Context Parallel | 把**长序列**切到不同卡并行算 | 长上下文显存/算力 |
| **EP** | Expert Parallel | 把 MoE 的**专家**分到不同卡 | MoE 显存 |
| **DP** | Data Parallel | **复制**整个模型，各卡处理不同请求 | 高吞吐 |
| **Wide-EP** | Wide Expert Parallel | EP + 专家复制 + 负载均衡 | 超大规模 MoE（DeepSeek-V3/R1、LLaMA4、Qwen3） |

理解这六种的关键是分清「**切权重**」（TP/EP，每卡只有一部分，要通信合并）和「**复制权重**」（DP，每卡都有全部，各干各的）。CP 比较特殊，它切的是「序列维度」而非权重。

更进一步，**同一个模型的不同模块可以用不同并行**。文档 [parallel-strategy.md:L49-L99](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L49-L99) 把它拆得很细：

- **Attention 模块**：小 batch 用 TP（权重切分），大 batch 用 **attention DP**（权重复制、KV cache 分区，因为不同请求路由到不同 DP rank）。
- **MoE 模块**：三种执行模式——纯 TP（每个专家权重都切片）、纯 EP（每个专家完整地住在一卡上）、混合 ETP（先 EP 再对子集 TP）。约束是 `moe_tensor_parallel_size * moe_expert_parallel_size == tensor_parallel_size`（[parallel-strategy.md:L121-L123](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L121-L123)）。

这就是为什么我们需要一个能同时表达「主并行（TP/PP/CP）」+「MoE 子并行」+「Attention 子并行」的对象——这正是 `Mapping` 要装下的信息量。

#### 4.1.2 核心流程

把六种并行落到一个具体部署上，有一个**铁律**（主并行维度）：

\[
\text{world\_size} = \text{tp\_size} \times \text{pp\_size} \times \text{cp\_size}
\]

也就是说，TP/PP/CP 三者**正交地瓜分**总卡数。这条等式在源码里被强制校验（见 4.1.3）。在此之上：

- **DP** 不是独立维度，而是 TP 的「复用」：`enable_attention_dp=True` 时，attention 在 TP 组内做数据并行。
- **EP/ETP** 是 TP 维度内部的进一步细分：`moe_tp_size * moe_ep_size (* moe_cluster_size) == moe_world_size`。
- **Attention 子并行** `attn_tp_size * attn_cp_size == tp_size * cp_size`，允许 attention 用与 FFN 不同的 TP/CP 划分。

一个 4 卡部署的拓扑可以同时是「TP=2, PP=2」「TP=4, PP=1」「TP=2, CP=2」等等，全看你怎么组合这三个乘数。下面的心智图概括了「总卡数如何被层层瓜分」：

```text
world_size (总卡数)
 ├── pp_size 段   (PP：层分段，stage 0..pp-1)
 │    ├── tp_size 路   (TP：权重切片)
 │    │    ├── moe_tp_size × moe_ep_size (MoE 内部再分)
 │    │    └── attn_tp_size × attn_cp_size (Attention 内部再分)
 │    └── cp_size 路   (CP：序列切片，连续 rank)
 └── (DP = TP 组内复制，由 enable_attention_dp 开关)
```

#### 4.1.3 源码精读

**铁律校验**就在 `MappingBase.__init__` 里。如果 `tp*pp*cp != world_size`，直接抛错：

[mapping.py:L143-L146](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L143-L146) —— 校验主并行维度乘积等于 world_size。

类似地，MoE 子并行和 Attention 子并行也各有乘积校验：

[mapping.py:L148-L168](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L148-L168) —— 校验 `moe_tp*moe_ep*moe_cluster == moe_world_size` 与 `attn_tp*attn_cp == tp*cp`。

`CpType` 枚举定义了上下文并行的四种算法，注意它同时影响 MoE 世界大小（`moe_world_size` 在 ULYSSES 下等于 `tp_size`，其它类型下等于 `tp_size*cp_size`）：

[mapping.py:L25-L33](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L25-L33) —— `CpType` 四值：ULYSSES（默认）、STAR、RING、HELIX。

[mapping.py:L93](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L93) —— `moe_world_size` 随 cp_type 变化，这是 HELIX 等策略能「在 FFN 层把 CP 复用为 TP」的前提。

> 术语小释：**ULYSSES**（来自 DeepSpeed-Ulysses）把 head 维度切给不同 CP rank，用 all-to-all 交换；**HELIX** 把序列分块做链式注意力，对 FFN 层则把 CP rank 复用成 TP（见 4.2.3 的 `repurpose_helix_cp_to_tp`）；**STAR** 是 Star-Attention；**RING** 是 Ring-Attention。

#### 4.1.4 代码实践

**实践目标**：把文档里的六种并行和模块级策略对上号，并验证乘积约束。

**操作步骤**：

1. 打开 [parallel-strategy.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md)，通读 L7–L123。
2. 仿照文档 L101–L119 的 YAML 例子，**在纸上**为一个 8 卡、纯 EP 的 MoE 部署写一份 `parallel_config.yaml`，并验算 `moe_tensor_parallel_size * moe_expert_parallel_size == tensor_parallel_size`。
3. 再写一份「混合 ETP：TP-4 × EP-2」的 YAML，确认乘积仍为 8。

**需要观察的现象**：两份 YAML 的 `tensor_parallel_size` 都是 8，但 `moe_tensor_parallel_size` / `moe_expert_parallel_size` 的拆法不同——这就是「同样的主并行、不同的 MoE 子并行」。

**预期结果**（示例代码，非项目原有文件）：

```yaml
# 纯 EP（8 卡）
tensor_parallel_size: 8
moe_tensor_parallel_size: 1
moe_expert_parallel_size: 8

# 混合 ETP（TP-4 × EP-2，8 卡）
tensor_parallel_size: 8
moe_tensor_parallel_size: 4
moe_expert_parallel_size: 2
```

两份都满足 `1*8==8` 与 `4*2==8`。

#### 4.1.5 小练习与答案

**练习 1**：一个模型单卡放不下权重，但单卡算力对延迟够了。你会优先选 TP 还是 DP？为什么？

> **答案**：优先 TP。DP 是「复制模型」，每卡仍要装下整个模型，解决不了显存墙；TP 切权重，把显存摊到多卡，正对「放不下」。DP 适合「放得下但不够快」的高吞吐场景。

**练习 2**：为什么文档强调 `moe_tensor_parallel_size * moe_expert_parallel_size` 必须等于 `tensor_parallel_size`？

> **答案**：因为 MoE 子并行是对「TP 这一维」的再细分，不能凭空占用 PP 或 CP 的卡。乘积等于 `tensor_parallel_size` 才保证 MoE 层和 dense 层用的是同一组卡、同一套 TP 通信域，只是内部专家权重换了一种切法。

---

### 4.2 Mapping 类：并行拓扑的单一真相源

#### 4.2.1 概念说明

光知道「有几种并行」还不够，运行时每个进程都要回答三个问题：

1. **我是谁？**——我的 `rank` 是几？我在哪个 PP 段、TP 路、CP 路？
2. **我和谁一组？**——我的 TP 通信组里有谁？PP 组里有谁？
3. **我该切什么？**——我要加载哪几层？哪些专家？权重矩阵切几分之几？

`Mapping` 就是回答这三个问题的**单一真相源（single source of truth）**。它同时承载：

- **拓扑的「尺寸」**：`tp_size` / `pp_size` / `cp_size` / `moe_tp_size` / `moe_ep_size` / `moe_cluster_size` / `attn_tp_size` / `attn_cp_size` / `world_size` / `gpus_per_node`。
- **本 rank 的「坐标」**：`rank` / `tp_rank` / `pp_rank` / `cp_rank` / `moe_tp_rank` / `moe_ep_rank` / `moe_cluster_rank`。
- **通信组**：`tp_group` / `pp_group` / `cp_group` / `moe_tp_group` / `moe_ep_group`（rank 列表）或对应的 `*_group_pg`（ProcessGroup 对象）。
- **一堆便捷判断**：`is_first_pp_rank()` / `is_last_pp_rank()` / `has_pp()` / `has_cp()` / `is_multi_node()` ……
- **层分配能力**：`pp_layers(num_layers)` 返回本 rank 该保留的层号列表；`ep_experts(num_experts)` 返回本 rank 该保留的专家号列表。

一个 `Mapping` 实例 = 「全局拓扑快照 + 本 rank 视角」。每个进程各持一份，`rank` 不同、拓扑尺寸相同。

#### 4.2.2 核心流程

构造一个 `Mapping` 分三步：

```text
1. __new__：按编排方式选实现
     ├── mpi_disabled() 为真（Ray / Slurm PMIx，TLLM_DISABLE_MPI=1）→ DeviceMeshTopology
     └── 否则（MPI 编排）→ MpiTopology
2. __init__（MappingBase）：
     a. 推导缺省的 MoE / Attention 子并行度（用户只给主并行也能跑）
     b. 三条乘积校验（tp*pp*cp / moe / attn）
     c. 存下所有尺寸字段
3. __init__（具体子类）：
     ├── MpiTopology._init_parallel_groups()：纯算术，立即算出所有通信组的 rank 列表
     └── DeviceMeshTopology：延迟到 build_mesh()，用 torch.distributed.DeviceMesh 建组
```

**rank → 坐标** 的分解公式（以 `MpiTopology` 为例，[mapping.py:L649-L658](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L649-L658)）：

\[
\text{pp\_rank} = \text{rank} // (\text{tp\_size} \cdot \text{cp\_size})
\]
\[
\text{tp\_rank} = (\text{rank} \bmod (\text{tp\_size} \cdot \text{cp\_size})) // \text{cp\_size}
\]
\[
\text{cp\_rank} = \text{rank} \bmod \text{cp\_size}
\]

关键直觉：**rank 编号是按「PP 段 → TP 路 → CP 路」由粗到细编码的**——CP rank 连续相邻，TP rank 按 `cp_size` 跨步，PP rank 跨过整个 `tp*cp` 块。源码顶部那段长 docstring（[mapping.py:L453-L547](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L453-L547)）正是用具体 rank 列表把这套编码讲清楚，它本身就是最好的「习题答案」。

#### 4.2.3 源码精读

**构造与校验**——`MappingBase.__init__` 的完整签名，注意所有并行度参数：

[mapping.py:L43-L62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L43-L62) —— `world_size` / `rank` / `gpus_per_node` 为位置参数，其余并行度都是关键字参数，缺省 `-1` 表示「待推导」。

**MoE / Attention 子并行度的自动推导**：用户常常只给 `tp_size`，不给 `moe_tp_size`/`moe_ep_size`。构造器按「优先级 + 乘积守恒」把它们补全：

[mapping.py:L112-L136](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L112-L136) —— MoE 缺省退化为 TP（`moe_tp_size = moe_world_size, moe_ep_size = 1`）；Attention 在 ULYSSES 下默认 `attn_tp=tp*cp, attn_cp=1`，其它 cp_type 下默认 `attn_tp=tp, attn_cp=cp`。

**两个特别值得注意的设计**：

1. **HELIX 的「CP 复用为 TP」**：HELIX 下 CP 只对 attention 层有意义，到了 FFN 层这些 rank 应该被当成 TP 用。`repurpose_helix_cp_to_tp()` 就造一个**新的** `Mapping`，把 `cp` 折叠进 `tp`：

   [mapping.py:L594-L614](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L594-L614) —— 返回 `cp_size=1, tp_size=tp*cp` 的新 Mapping，专供 FFN 层使用。

2. **`__new__` 的实现分发**：同一个 `Mapping(...)` 调用，根据 `mpi_disabled()` 返回不同子类的实例：

   [mapping.py:L549-L553](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L549-L553) —— 这是「对用户透明、对内可切换通信后端」的关键。

**通信组建模（MpiTopology）**——`_init_parallel_groups` 纯用 `range` 算术生成每组的 rank 列表，不依赖真实分布式环境：

[mapping.py:L691-L711](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L691-L711) —— PP 组按 `tp*cp` 跨步、CP 组连续、TP 组按 `cp_size` 跨步。

**层分配**——`pp_layers` 把 `num_layers` 层均分（或按 `pp_partition` 自定义）给各 PP 段：

[mapping.py:L372-L386](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L372-L386) —— 无 `pp_partition` 时用 `torch.tensor_split`，不能整除时前几个 rank 各多分一层。

**序列化**——`to_dict` / `from_dict` 让 `Mapping` 能在 worker 进程间传递（Ray 把配置序列化下发到每个 worker）：

[mapping.py:L428-L450](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L428-L450) —— `to_dict` 导出全部尺寸字段（不含通信组，因为组是按 rank 现算的），`from_dict` 原样还原。

#### 4.2.4 代码实践

**实践目标**：构造一个 **4 卡、TP=2、PP=2** 的 `Mapping`，推理出每个 rank 的角色（PP 段、TP 路、所属通信组），并与源码 docstring 对照。

**操作步骤**：

1. 阅读顶部 docstring 例子 [mapping.py:L453-L547](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L453-L547)，找到「8 卡 tp=4 pp=2」的 TP/PP 组例子（L455–L467），理解编码规律。
2. 套用 4.2.2 的三个分解公式，**手算** rank 0/1/2/3 的 `pp_rank`、`tp_rank`。
3. 套用 `_init_parallel_groups`（[mapping.py:L691-L711](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L691-L711)）算出 `tp_groups` 和 `pp_groups`。
4. （可选，需 MPI 模式）写一段脚本，构造 `Mapping(world_size=4, rank=r, tp_size=2, pp_size=2)`，打印 `m.tp_group` / `m.pp_group`，验证手算结果。

**需要观察的现象**：TP 组把「同一 PP 段内的两张卡」绑在一起做权重切片后的 all-reduce；PP 组把「跨段、同一 TP 位置的两张卡」绑成一条流水线，负责前后传 activation。

**预期结果**（手算，TP=2, PP=2, CP=1, world_size=4）：

| rank | pp_rank（流水线段） | tp_rank | tp_group | pp_group | 角色 |
|------|------|------|----------|----------|------|
| 0 | 0（首段，含 embedding） | 0 | [0, 1] | [0, 2] | stage0 / TP 路 0 |
| 1 | 0 | 1 | [0, 1] | [1, 3] | stage0 / TP 路 1 |
| 2 | 1（末段，含 lm_head） | 0 | [2, 3] | [0, 2] | stage1 / TP 路 0 |
| 3 | 1 | 1 | [2, 3] | [1, 3] | stage1 / TP 路 1 |

其中 `tp_groups = [[0,1],[2,3]]`、`pp_groups = [[0,2],[1,3]]`。

> ⚠️ **运行验证说明**：在 Ray 编排（PyTorch 后端默认，`TLLM_DISABLE_MPI=1`）下，`Mapping` 实例是 `DeviceMeshTopology`，它的 `.tp_group` 等**不会**在构造时填充，而是延迟到 `build_mesh()`（需要已初始化的真实 `torch.distributed` 进程组）。要在单机上**立即**看到上面的组列表，需用 MPI 编排（不设 `TLLM_DISABLE_MPI`）跑多进程。若你手边只有单卡，本实践以「读 docstring + 套公式手算」为准，运行验证标记为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：同样的 4 卡，改成 **TP=4, PP=1**。`pp_groups` 长什么样？`is_first_pp_rank()` 和 `is_last_pp_rank()` 在哪些 rank 上为真？

> **答案**：PP=1 时没有流水线，`pp_groups` 只有 1 个组 `[0,1,2,3]`（实际上 `pp_size==1` 时 `has_pp()` 为假）。所有 rank 的 `pp_rank==0`，因此 `is_first_pp_rank()` 与 `is_last_pp_rank()` 对全部 4 个 rank **同时为真**——意味着每张卡既持有 embedding 也持有 lm_head。

**练习 2**：为什么 `Mapping` 要把「拓扑尺寸」和「本 rank 坐标」放进同一个对象，而不是分成两个类？

> **答案**：因为通信组的计算、层/专家的分配都**同时**依赖两者——比如 `tp_group` 既要知道 `tp_size`（组的大小）又要知道 `rank`（我在组里的位置）。把它们放一起，让任何一个进程凭「自己这一份 Mapping」就能就地算出全部所需信息，无需跨进程查询，这正是它能作为单一真相源的前提。

---

### 4.3 并行配置：从 TorchLlmArgs 扁平字段到 Mapping

#### 4.3.1 概念说明

u4-l1 讲过 `TorchLlmArgs` 是「扁平与嵌套共存」的巨型配置。并行相关的字段就是这套设计的典型样本：

- **顶层扁平字段**（用户最常碰）：`tensor_parallel_size`、`pipeline_parallel_size`、`context_parallel_size`、`gpus_per_node`、`moe_tensor_parallel_size`、`moe_expert_parallel_size`、`enable_attention_dp`、`pp_partition`、`cp_config`。
- **内部聚合对象** `_ParallelConfig`：把这些字段收拢成一个子配置，并暴露 `world_size`（= `tp*pp*cp`）、`is_multi_gpu` 等便捷属性。
- **桥到运行时** `to_mapping()`：把 `_ParallelConfig` 翻译成一个 `Mapping` 实例。

这条链路的终点是 `Mapping`，但**起点是用户**——用户写 YAML 或传 kwargs 时只关心扁平字段，聚合与翻译全由框架自动完成。

#### 4.3.2 核心流程

```text
用户 YAML / kwargs
   │  tensor_parallel_size: 2
   │  pipeline_parallel_size: 2
   │  ...
   ▼
TorchLlmArgs（Pydantic 校验）
   │  model_validator("after") validate_parallel_config
   │  → 把扁平字段聚合进私有 _parallel_config: _ParallelConfig
   ▼
parallel_config: _ParallelConfig   （只读 property）
   │  to_mapping()
   ▼
Mapping(world_size=4, rank=mpi_rank(), tp_size=2, pp_size=2, ...)
   │  每个 worker 进程用自己的 rank 各造一份
   ▼
被消费：
   ├── ModelLoader.mapping      （加载期：切权重、定 PP 层）
   ├── ModelConfig.mapping       （前向期：随时查拓扑）
   ├── py_executor_creator       （建通信层 Distributed、建引擎）
   └── Communicator.build_mesh() （真正建出 ProcessGroup）
```

注意一个要点：`to_mapping()` 里 `rank=mpi_rank()`——也就是说，**同一份 `_ParallelConfig` 在不同进程里会产出 `rank` 不同的 `Mapping`**。这正是「每个进程各持一份、坐标不同」的实现机制。`py_executor_creator._get_mapping` 还会显式用本进程的 rank 覆盖一次（[py_executor_creator.py:L197-L206](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L197-L206)），双保险。

#### 4.3.3 源码精读

**扁平字段**就摊在 `BaseLlmArgs` 顶层，三个主并行字段连读：

[llm_args.py:L4155-L4207](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4155-L4207) —— `tensor_parallel_size` / `pipeline_parallel_size` / `context_parallel_size` / `gpus_per_node` / `moe_*_parallel_size` / `enable_attention_dp` 全在此。注意 MoE 三个字段默认 `None`（标 beta/prototype），后续会被规整成 `-1` 以触发 `Mapping` 的自动推导。

**聚合钩子** `validate_parallel_config`——构造完成后把扁平字段装进 `_ParallelConfig`，并把 `None` 规整为 `-1`：

[llm_args.py:L4499-L4522](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4499-L4522) —— 典型的 `model_validator(mode="after")`：写私有 `_parallel_config`，对应 u4-l1 讲过的「扁平字段聚合进私有属性、只读 property 暴露」模式。

**`_ParallelConfig` 本体**与 `world_size` 推导：

[llm_args.py:L1596-L1642](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L1596-L1642) —— `world_size` 是只读 property `tp*pp*cp`，写它时若不等于乘积会报错——这与 `Mapping` 的铁律校验互为呼应。

**桥** `to_mapping()`：

[llm_args.py:L1648-L1664](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L1648-L1664) —— 逐字段搬进 `Mapping(...)`，`rank` 取自 `mpi_rank()`，`cp_config` 从 Pydantic `CpConfig` model_dump 回 dict（`Mapping` 暂时仍用 dict 形态）。

**`gpus_per_node` 的智能默认**——不填则用本机 GPU 数：

[llm_args.py:L4469-L4478](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4469-L4478) —— Ray 下取 `tensor_parallel_size`，否则取 `torch.cuda.device_count()`。这是「多节点感知」的起点：`Mapping.is_multi_node()` 用 `world_size > gpus_per_node` 判断。

**`CpConfig`**——上下文并行的子配置，`cp_type` 默认 ULYSSES，其余字段（`tokens_per_block`、`use_nccl_for_alltoall` 等）按 cp_type 适用：

[llm_args.py:L1561-L1583](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L1561-L1583) —— `cp_type` 字符串会被 `validate_cp_type` 转大写后映射成 `CpType` 枚举（[llm_args.py:L1585-L1593](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L1585-L1593)）。

#### 4.3.4 代码实践

**实践目标**：跟踪一份 YAML 配置一路走到 `Mapping`，并列出代码里 `Mapping` 被消费的关键位置。

**操作步骤**：

1. 在 [llm_args.py:L4499-L4522](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4499-L4522) 确认：用户给的 `tensor_parallel_size=2, pipeline_parallel_size=2` 是怎么进入 `_ParallelConfig` 的。
2. 跟到 [llm_utils.py:L65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_utils.py#L65)：`ModelLoader.__init__` 里 `self.mapping = llm_args.parallel_config.to_mapping()`——这是加载期的第一份 `Mapping`。
3. 再跟到 [py_executor_creator.py:L303](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L303)：执行器装配时每个 worker 重新 `to_mapping()` 并用本进程 rank 覆盖（`_get_mapping`）。
4. 最后看消费侧：
   - **模型配置**：[model_config.py:L134](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L134) `ModelConfig.mapping` 字段。
   - **PP 层分配**：[modeling_utils.py:L283-L323](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L283-L323) `__pp_init__` 用 `mapping.pp_layers(...)` 决定本 rank 保留哪几层、跳过 embed/lm_head。
   - **通信组建模**：[device_mesh.py:L109-L146](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L109-L146) `build_mesh()` 按 `[pp, tp, cp]`（或含 `moe_tp/moe_ep`）维度顺序建 DeviceMesh；实际触发点在 [communicator.py:L817](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/distributed/communicator.py#L817) `mapping.build_mesh()`。

**需要观察的现象**：同一组配置在「加载期（ModelLoader，rank 来自 mpi_rank）」和「执行器装配期（每个 worker，rank 被显式覆盖）」各产出一份 `Mapping`；两份的拓扑尺寸相同、`rank` 可能不同。`Mapping` 一路向下传，最终在两处真正「落地」——模型用它切层、通信层用它建组。

**预期结果**：能画出 4.3.2 的那张流程图，并标注每个箭头对应的源码行。

**运行说明**：本实践为**源码阅读型实践**，单机单卡即可完成（无需 GPU/多进程）；若想运行时打印 `mapping.to_dict()`，可在 `ModelLoader.__init__` 之后插一条日志观察（修改源码仅用于本地学习，勿提交）。

#### 4.3.5 小练习与答案

**练习 1**：用户只填了 `tensor_parallel_size: 4`，没填任何 `moe_*` 和 `attn_*` 字段。最终 `Mapping` 里的 `moe_ep_size`、`attn_tp_size`、`attn_cp_size` 分别是多少？

> **答案**：`moe_*` 默认 `None` → 被规整为 `-1`（[llm_args.py:L4504-L4508](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4504-L4508)）→ 在 `MappingBase` 里触发自动推导（[mapping.py:L112-L120](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L112-L120)）：`moe_tp_size=4, moe_ep_size=1`。`cp_size` 未填默认 1、`cp_type` 默认 ULYSSES，于是 Attention 自动推导（[mapping.py:L122-L130](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L122-L130)）得 `attn_tp_size=4, attn_cp_size=1`。即「没给 MoE/Attention 子并行，就全部退化为跟主 TP 一致」。

**练习 2**：为什么 `to_mapping()` 里要写 `rank=mpi_rank()`，而 `py_executor_creator._get_mapping` 还要再用本进程 rank 覆盖一次？两次取 rank 是冗余吗？

> **答案**：不是冗余而是双保险。`to_mapping()` 在「构造配置对象」时填一个 rank（单进程场景就够用）；但多 worker 场景下，配置对象可能被序列化下发到多个进程，每个进程的 `mpi_rank()` 才是它自己的真实身份。`_get_mapping`（[py_executor_creator.py:L197-L206](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L197-L206)）在 worker 侧显式 `mapping.rank = mpi_rank()`，确保「这一份 Mapping 的坐标 = 这个 worker 的真实坐标」。

---

## 5. 综合实践

**任务**：为一个 **2 节点 × 8 卡 = 16 卡**的集群，设计一份部署 DeepSeek-V3（MoE 模型）的并行配置，要求「PP=2、TP=2、EP=4（混合 ETP）」，并验证它自洽，最后写出每个 rank 的角色表。

**步骤**：

这道题故意埋了两个坑，逐步排查：

1. **第一坑（MoE 子并行乘积）**：naive 地写成 `tensor_parallel_size=2, moe_expert_parallel_size=4` 行不行？不行——[parallel-strategy.md:L121-L123](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L121-L123) 要求 `moe_tp*moe_ep == tp_size`，而 `1*4=4 ≠ 2`。说明 **EP 不能超过 TP**，想用 EP=4，TP 至少得 4。
2. **第二坑（CP + EP 的兼容性）**：那把 `tp=4, pp=2, cp=2`（`4*2*2=16`）配上 `moe_ep=4` 行不行？也不行——[mapping.py:L170-L172](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L170-L172) 明确：`moe_ep_size != 1 且 cp_size > 1` 时，**必须**用 `CpType.HELIX`，否则抛 `NotImplementedError`。默认的 ULYSSES 不支持 EP+CP 组合。
3. **正解**：避开 CP，用纯 TP 容纳 EP。取 `pp_size=2, tp_size=8, cp_size=1`（`8*2*1=16`），MoE 子并行 `moe_tensor_parallel_size=2, moe_expert_parallel_size=4`（`2*4=8=tp_size` ✓，无 CP 故不触发第二坑）。

**写 YAML**（示例代码，非项目原有文件）：

```yaml
tensor_parallel_size: 8
pipeline_parallel_size: 2
context_parallel_size: 1
gpus_per_node: 8          # 16 卡 / 8 = 2 节点
moe_tensor_parallel_size: 2
moe_expert_parallel_size: 4
```

**推理 rank 角色**：套用 4.2.2 的公式（`pp_rank = rank//(tp*cp) = rank//8`，`tp_rank = rank%8`，`moe_ep_rank = tp_rank % moe_ep_size = tp_rank % 4`），三个代表 rank 的答案如下：

| rank | pp_rank | tp_rank | moe_tp_rank | moe_ep_rank | is_first_pp_rank | is_last_pp_rank | node_rank | is_multi_node |
|------|---------|---------|-------------|-------------|------------------|-----------------|-----------|---------------|
| 0    | 0       | 0       | 0           | 0           | True             | False           | 0         | True          |
| 5    | 0       | 5       | 1           | 1           | True             | False           | 0         | True          |
| 15   | 1       | 7       | 1           | 3           | False            | True            | 1         | True          |

（`moe_tp_rank = tp_rank // (moe_ep_size*moe_cluster_size) = tp_rank // 4`。）可见 rank 0–7 是 PP 第 0 段（节点 0），rank 8–15 是 PP 第 1 段（节点 1）；每个节点内 8 张卡被切成 2 个 moe_tp 路 × 4 个 moe_ep 槽。

**核对消费点**：确认这份配置会经 `validate_parallel_config` → `_ParallelConfig` → `to_mapping()` 进入 `Mapping`，再被 `__pp_init__`（切层）和 `build_mesh()`（建含 `moe_tp/moe_ep` 维度的 DeviceMesh，见 [device_mesh.py:L126-L134](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L126-L134)）消费。

**自检要点**：

- `world_size (16) == tp*pp*cp (8*2*1)` ✓
- `moe_tp*moe_ep (2*4) == tp_size (8)` ✓
- `cp_size==1`，故 `moe_ep>1` 不触发 [mapping.py:L170-L172](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L170-L172) 的 EP+CP 限制 ✓
- `is_multi_node()`：`world_size(16) > gpus_per_node(8)` → True ✓
- DeviceMesh 维度顺序：因 `moe_ep_size>1`，dims = `["pp","moe_tp","moe_ep","cp"]`，shape = `[2,2,4,1]`，`tp` 由 `(moe_tp,moe_ep)` 展平为 8 ✓

> 本实践为**设计 + 推理型实践**，无需真实 16 卡集群即可完成全部验算；真实运行验证标记为**待本地验证**。

## 6. 本讲小结

- 并行是为翻越「显存墙」和「算力墙」：**TP/PP/CP/EP 切权重或序列**（要通信合并），**DP 复制模型**（各干各的）；主并行铁律是 `world_size = tp*pp*cp`。
- `Mapping` 是并行的**单一真相源**：一个对象同时装下「拓扑尺寸 + 本 rank 坐标 + 通信组 + 层/专家分配」，每个进程各持一份、坐标不同。
- `rank` 编码遵循「PP 段 → TP 路 → CP 路」由粗到细：`pp_rank = rank//(tp*cp)`，`cp_rank = rank%cp_size`，CP 连续、TP 跨步。
- `Mapping.__new__` 在 `MpiTopology`（MPI，立即算组）与 `DeviceMeshTopology`（Ray，延迟建组）间透明切换；对外属性同名。
- 用户侧只需写扁平字段（`tensor_parallel_size` 等）→ `model_validator` 聚合成 `_ParallelConfig` → `to_mapping()` 翻译成 `Mapping`；缺省的 MoE/Attention 子并行由 `MappingBase` 按「乘积守恒」自动推导。
- `Mapping` 被消费于：`ModelLoader`/`py_executor_creator`（造实例）、`ModelConfig.mapping`（前向查询）、`__pp_init__` 的 `pp_layers`（切层）、`Communicator.build_mesh()`（建 ProcessGroup）。

## 7. 下一步学习建议

本讲只讲了「**拓扑怎么描述、怎么配置**」，但刻意没碰「**通信具体怎么做**」——TP 组里的 all-reduce、EP 组里的 all-to-all 到底是怎么发的？这正是下一讲的主题：

- **[u9-l2 分布式通信原语](u9-l2-distributed-communication.md)**：拆解 `communicator` 抽象、`allreduce_helper` / `symm_mem_allreduce` 的实现选择，以及 `moe_alltoall` 在专家并行 forward 里的数据搬运方向。本讲里反复出现的 `*_group_pg`（ProcessGroup）和 `build_mesh()`，会在那里真正「通电」。

此外推荐结合阅读：

- [docs/source/features/parallel-strategy.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md) 的 Wide-EP 一节（L125–L182），作为 u10-l1（MoE 架构与后端）的预习。
- `Mapping` 顶部 docstring（[mapping.py:L453-L547](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L453-L547)）的各种 rank 组合例子，是巩固「rank 编码」直觉的最佳练习册。
