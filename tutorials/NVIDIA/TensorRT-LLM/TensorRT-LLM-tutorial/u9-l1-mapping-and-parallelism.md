# 讲义 u9-l1：Mapping 与并行策略

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚大模型推理为什么需要「多卡并行」，以及 TensorRT-LLM 支持的几类并行（TP / PP / CP / EP / DP）各自解决什么问题。
- 看懂 `Mapping` 这个核心数据结构：它如何用**一个对象**完整描述「整张并行拓扑」——每张卡是谁、属于哪些通信组、负责哪些层、哪些专家。
- 掌握 `Mapping` 内部的 rank 计算公式与通信组划分算法，能手算一个 4 卡 TP=2、PP=2 的拓扑，说出每个 rank 的角色。
- 把「用户在 `llm_args` 里填的扁平参数」一路追到「真正驱动前向的 `Mapping` 对象」，理解 `_ParallelConfig.to_mapping()` 这条转换链路。
- 区分两条拓扑实现路径（`MpiTopology` 与 `DeviceMeshTopology`），并理解 `CpType` 等枚举的含义。

本讲是 u9（分布式与并行）单元的第一讲，重点放在「并行拓扑如何被描述与配置」；下一讲 u9-l2 会接着讲建立在这些拓扑之上的**通信原语**（allreduce / allgather / MoE alltoall）。

## 2. 前置知识

在进入本讲前，先建立两点直觉。

**第一，为什么 LLM 推理要切多卡。** 一个大模型有两类资源消耗：**权重显存**和**算力**。当模型放不进单卡显存，或者单卡算力达不到想要的吞吐/延迟时，就要把工作分摊到多张卡上。分摊方式不止一种：可以纵向「按层切」（Pipeline Parallel），可以横向「把每个权重矩阵切成几片」（Tensor Parallel），也可以按 MoE 的专家切（Expert Parallel），还可以把长上下文按段切（Context Parallel）。这些切法常常**组合使用**。并行策略文档开篇就说清楚了这两类动机：

[docs/source/features/parallel-strategy.md:3-13](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L3-L13) 中文说明：模型放不下单卡显存、或单卡达不到性能时就需要多卡并行，仓库支持 TP / PP / DP / EP / CP / Wide-EP 等多种策略。

**第二，「谁和谁通信」要靠通信组（process group）来表达。** 并行的本质是：每张卡只算一部分，算完之后要和「持有其它部分」的卡交换中间结果。比如 Tensor Parallel 下，每张卡算出自己那部分注意力，最后要 `allreduce` 求和。于是每张卡天然属于若干个**通信组**：TP 组、PP 组、CP 组、MoE-EP 组……「描述并行拓扑」就等价于「定义这些组，并告诉每张卡它属于哪些组、在组内是第几个」。

这正是 `Mapping` 要做的事。它不发明新概念，而是把「这张卡的角色」用一组整数和一串列表讲清楚。

> 名词提示：
> - **rank**：全局进程编号，0 到 `world_size-1`，每个进程管一张卡。
> - **world_size**：参与的总进程（卡）数。
> - **TP / PP / CP / EP**：见上，后面逐个展开。

## 3. 本讲源码地图

本讲涉及的文件很少，但每个都很关键：

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/mapping.py` | **核心**。定义 `MappingBase` / `Mapping` / `MpiTopology` / `DeviceMeshTopology` 四个类，以及 `CpType` 枚举。所有 rank 公式与通信组划分算法都在这里。 |
| `tensorrt_llm/llmapi/llm_args.py` | 定义用户侧的扁平并行参数（`tensor_parallel_size` 等）与 `_ParallelConfig` 子配置，并提供 `to_mapping()` 转换成 `Mapping`。 |
| `tensorrt_llm/_torch/device_mesh.py` | `DeviceMeshTopology` 的真正实现（基于 PyTorch `DeviceMesh` 建 mesh 与 process group）。 |
| `tensorrt_llm/executor/base_worker.py` | worker 启动时调用 `parallel_config.to_mapping()`，是「配置 → Mapping」的接入点。 |
| `tensorrt_llm/_torch/model_config.py` | `ModelConfig` 把 `Mapping` 当作字段携带到前向，前向各处通过 `mapping.tp_size` 等读拓扑。 |
| `docs/source/features/parallel-strategy.md` | 并行策略的用户文档，讲清各类并行的适用场景。 |

贯穿全讲的因果链是：

```text
用户 llm_args(扁平字段)
   └─ model_validator 聚合 ─>  _ParallelConfig
                                   └─ to_mapping()  ─>  Mapping 对象
                                                            └─ 注入 ModelConfig.mapping
                                                                    └─ 前向 / 通信 / KV cache 处处读取
```

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**Mapping 类**、**并行拓扑（rank 公式与通信组划分）**、**并行配置（参数到 Mapping 的链路）**。

### 4.1 Mapping：用一个对象描述整张并行拓扑

#### 4.1.1 概念说明

`Mapping` 是「一份并行部署的身份证」。给定一个 `Mapping` 实例，你就能回答关于某张卡的全部问题：

- 它在第几个流水线阶段（`pp_rank`）？
- 它在 TP 组内是第几个（`tp_rank`）？
- 它负责模型里的哪几层（`pp_layers`）？
- 它持有哪些专家（`ep_experts`）？
- 它要和哪些 rank 通信（`tp_group` / `pp_group` / …）？

为什么要把这些都塞进一个对象？因为分布式代码遍布前向、加载、KV cache、调度器各处，若每处都自己重新推算「我在哪、要和谁通信」，既容易写错又难维护。`Mapping` 把拓扑**算一次、到处读**，是整个分布式子系统的「单一事实源」。

`Mapping` 本身是个**描述层**：它存的是整数（各 size、各 rank）和列表（各 group 的成员）。真正「建立 NCCL/GLPX 通信」的动作发生在两条实现路径上（见 4.1.2），由 `Mapping` 的子类负责。

#### 4.1.2 核心流程

构造一个 `Mapping` 时，构造函数会做四件事：

1. **校验拓扑自洽**：`world_size == tp_size * pp_size * cp_size`，以及若干类似的不等式（MoE 的、注意力 TP/CP 的）。任何不自洽直接抛 `ValueError`。
2. **补全默认值**：用户只给了顶层并行度，构造函数推导出 MoE 的 `moe_tp_size` / `moe_ep_size`、注意力的 `attn_tp_size` / `attn_cp_size`。
3. **决定实现路径**：通过 `__new__` 在 `MpiTopology`（MPI 编排）和 `DeviceMeshTopology`（Ray 编排）之间二选一。
4. **建立通信组**（仅 `MpiTopology` 在构造时立即算出各组的 rank 列表；`DeviceMeshTopology` 则懒加载 PyTorch `DeviceMesh`）。

两条实现路径的选择逻辑很简洁：

```python
def __new__(cls, *args, **kwargs):
    if mpi_disabled():
        return super().__new__(DeviceMeshTopology)
    else:
        return super().__new__(MpiTopology)
```

[tensorrt_llm/mapping.py:549-553](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L549-L553) 中文说明：`Mapping.__new__` 按环境变量 `TLLM_DISABLE_MPI` 决定实例化哪个子类——禁用 MPI（Ray 编排）走 `DeviceMeshTopology`，否则走 `MpiTopology`。

而 `mpi_disabled()` 的判定很简单：

[tensorrt_llm/_utils.py:452-454](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_utils.py#L452-L454) 中文说明：`mpi_disabled()` 仅检查环境变量 `TLLM_DISABLE_MPI == "1"`。

这个二选一是理解后续代码的钥匙：`MpiTopology` **自己用纯 Python 算** rank 公式和通信组（4.2 会精读），因为它无法依赖 torch 的进程组；`DeviceMeshTopology` 则把活儿交给 PyTorch 的 `DeviceMesh`，自己只做映射。

#### 4.1.3 源码精读

先看 `MappingBase.__init__` 的参数签名，理解它能描述哪些维度：

[tensorrt_llm/mapping.py:43-62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L43-L62) 中文说明：`MappingBase` 的构造参数，含 `world_size` / `rank` / `gpus_per_node`，以及 TP / PP / CP / MoE（cluster/tp/ep）/ 注意力 TP / CP / DWDP 等全部并行维度。

注意一个关键设计：**只有 `tp_size` / `pp_size` / `cp_size` 是「顶层」并行度**，它们的乘积必须等于 `world_size`：

[tensorrt_llm/mapping.py:143-146](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L143-L146) 中文说明：强制 `world_size == tp_size * pp_size * cp_size`，否则报错——这是拓扑自洽的第一道关。

而 MoE 的 `moe_tp_size` / `moe_ep_size`、注意力的 `attn_tp_size` / `attn_cp_size` 是**派生量**，构造函数会根据 cp_type 推导。例如 MoE 的「世界」取决于 CP 类型：

[tensorrt_llm/mapping.py:93](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L93) 中文说明：ULYSSES 型 CP 下，MoE 的世界就是 `tp_size`；否则（如 HELIX）MoE 世界是 `tp_size * cp_size`——因为非 Ulysses 的 CP 在 FFN 层会被「复用」成 TP（见 `repurpose_helix_cp_to_tp`）。

补全 MoE 与注意力默认值的逻辑是一组「你给一个，我推另一个」的分支：

[tensorrt_llm/mapping.py:112-136](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L112-L136) 中文说明：当 MoE 的 tp/ep 都没指定时默认全 TP（`moe_tp_size = moe_world_size, moe_ep_size = 1`）；只指定一个则按乘积推另一个；注意力的 tp/cp 同理，ULYSSES 默认把 cp 折进 tp。

此外还有两道自洽校验，专门保护 MoE 与注意力维度：

[tensorrt_llm/mapping.py:157-168](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L157-L168) 中文说明：非 DWDP 时要求 `moe_tp_size * moe_ep_size * moe_cluster_size == moe_world_size`，且 `attn_tp_size * attn_cp_size == tp_size * cp_size`。

构造完成后，`Mapping` 暴露一大批**只读 property / 方法**供全代码库读取。最常用的几个：

| 接口 | 含义 |
|------|------|
| `mapping.tp_rank` / `pp_rank` / `cp_rank` | 本卡在各维度的局部编号 |
| `mapping.tp_group` / `pp_group` / `cp_group` | 本卡所属通信组的 rank 列表 |
| `mapping.has_tp()` / `has_pp()` / `has_cp()` | 是否启用了该并行 |
| `mapping.is_first_pp_rank()` / `is_last_pp_rank()` | 是否流水线首/末 stage（决定要不要做 embedding / lm_head） |
| `mapping.pp_layers(num_layers)` | 本卡负责哪几层 |
| `mapping.ep_experts(num_experts)` | 本卡持有哪几个专家 |
| `mapping.prev_pp_rank()` / `next_pp_rank()` | 流水线上/下游的 rank（PP 通信对象） |

几个典型实现：

[tensorrt_llm/mapping.py:318-325](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L318-L325) 中文说明：`is_last_pp_rank` / `is_first_pp_rank` 用 `pp_rank` 与 `pp_size-1` / `0` 比较——末 stage 才算 logits、才挂 lm_head。

[tensorrt_llm/mapping.py:330-340](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L330-L340) 中文说明：`prev_pp_rank` / `next_pp_rank` 在全局 rank 上加减 `tp_size*cp_size`（一个 PP stage 的卡数）并做环形回绕，用于找流水线邻居。

而 `pp_layers` 负责把模型的 `num_layers` 层按 PP 切分，支持自定义 `pp_partition`：

[tensorrt_llm/mapping.py:372-386](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L372-L386) 中文说明：若给了 `pp_partition`（每段层数的列表）就按它切；否则用 `torch.tensor_split` 均分，分不完时前几个 rank 各多分一层。

`CpType` 枚举定义了上下文并行的几种实现风格，本讲只需记住它存在、默认是 `ULYSSES`：

[tensorrt_llm/mapping.py:25-33](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L25-L33) 中文说明：`CpType` 含 `ULYSSES` / `STAR` / `RING` / `HELIX` 四种 CP 实现，默认 ULYSSES；构造函数里若 `cp_config["cp_type"]` 是字符串会转成该枚举。

#### 4.1.4 代码实践

**实践目标**：在单进程下实例化一个 `Mapping`，验证它的拓扑自洽校验与默认值推导。

**操作步骤**：

1. 写一段最小脚本（**示例代码**，非项目原有文件）：

   ```python
   from tensorrt_llm.mapping import Mapping

   # 单卡基线：什么都不切
   m = Mapping(world_size=1, rank=0, gpus_per_node=8)
   print("tp_size", m.tp_size, "pp_size", m.pp_size)
   print("attn_tp_size", m.attn_tp_size, "moe_tp_size", m.moe_tp_size)

   # 故意构造一个不自洽的拓扑
   try:
       bad = Mapping(world_size=4, tp_size=2, pp_size=1)  # 2*1 != 4
   except ValueError as e:
       print("caught:", e)
   ```

2. 在装好 `tensorrt_llm`（纯 Python 路径即可，`TRTLLM_USE_PRECOMPILED=1`，见 u1-l2）的环境里运行：`python demo.py`。

**需要观察的现象**：
- 单卡 `Mapping` 的 `moe_tp_size` 被自动设为 1（默认全 TP 的退化情形），`attn_tp_size` 也是 1。
- 第二段会抛 `ValueError: world_size must equal to tp_size * pp_size * cp_size ...`。

**预期结果**：第一段打印 `tp_size 1 pp_size 1`、`attn_tp_size 1 moe_tp_size 1`；第二段打印校验错误。若你看到 `ImportError` 说明 `tensorrt_llm` 没装好，按 u1-l2 复查。运行此脚本需要可 import `tensorrt_llm`，**实际打印数值待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Mapping` 为什么把 `moe_tp_size` / `attn_tp_size` 设计成「派生量」而非和 `tp_size` 平级的必填项？

> **参考答案**：因为用户通常只关心「顶层切几份」（tp/pp/cp），MoE 与注意力各自如何分配 TP/EP/CP 有合理的默认推导规则（如默认全 TP）。把它们设成派生量可以减少必填参数，同时仍允许高级用户显式覆盖。

**练习 2**：`Mapping.__new__` 在两个子类之间选择，这种「描述层相同、实现不同」的设计带来了什么好处？

> **参考答案**：上层代码（前向、加载、调度）只依赖 `Mapping` 的描述性接口（`tp_size`、`tp_group` 等），无需关心底层是 MPI 还是 Ray/DeviceMesh。换编排方式时，上层零改动。

---

### 4.2 并行拓扑：rank 公式与通信组划分

#### 4.2.1 概念说明

本模块回答两个具体问题：**给定全局 rank，怎么算出它的 pp/tp/cp 局部 rank？** 以及 **怎么列出每个通信组的成员？**

关键在于一个**全局 rank 的排布约定**。`MpiTopology` 把全局 rank 按如下维度嵌套编号（从外到内）：

```text
global_rank = pp_rank * (tp_size * cp_size) + tp_rank * cp_size + cp_rank
```

即 **CP 最内层（连续）、TP 居中（按 cp_size 跨步）、PP 最外层**。这个约定不是任意的——它决定了通信组的物理布局，进而影响 NVLink 拓扑效率（同一 TP 组的卡最好物理相邻）。`DeviceMeshTopology` 走的是同一套嵌套，只是交给 PyTorch 的 `init_device_mesh`：

[tensorrt_llm/_torch/device_mesh.py:121-140](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L121-L140) 中文说明：建 mesh 时维度顺序为 `["pp", ...("moe_tp","moe_ep" 或 "tp"), "cp"]`，注释明确写了「pp 最外、tp 居中、cp 最内（连续）」。

各类并行的语义（结合官方文档）：

| 并行 | 切的是什么 | 谁要通信 |
|------|-----------|---------|
| **TP**（Tensor Parallel）| 每个权重矩阵切片，所有卡处理同样的 token | TP 组内 allreduce |
| **PP**（Pipeline Parallel）| 按层切，各卡处理不同层 | PP 组内传递激活（p2p） |
| **CP**（Context Parallel）| 长序列按段切，各卡处理不同 token 段 | CP 组内交换注意力中间量 |
| **EP**（Expert Parallel）| MoE 专家分布到不同卡 | EP 组内 alltoall（派发/回收 token） |
| **DP**（Data Parallel，注意力 DP）| 模型复制，各卡跑不同请求 | 注意力 DP 不需通信，KV cache 分区 |

文档对每种并行的「最佳场景」有简明总结：

[docs/source/features/parallel-strategy.md:17-45](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/parallel-strategy.md#L17-L45) 中文说明：TP 适合小 batch / 显存受限；PP 适合放不进单卡的大模型；DP 适合大 batch 高吞吐；EP 适合专家数多的 MoE；CP 适合长上下文。

> 一个易混点：**注意力 DP（`enable_attention_dp`）** 与传统训练里的 DP 不同。它是把注意力当成数据并行（权重复制、各卡跑不同请求、KV cache 分区），但 FFN 层仍走 TP。`Mapping` 用 `dp_size` 属性反映这点：

[tensorrt_llm/mapping.py:294-296](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L294-L296) 中文说明：`dp_size` 在开启注意力 DP 时等于 `tp_size`，否则为 1——它表达「一条 TP 流被复制成几份来跑不同请求」。

#### 4.2.2 核心流程

`MpiTopology` 把上面那套 rank 公式直接写成 property：

```text
pp_rank = rank // (tp_size * cp_size)
tp_rank = (rank % (tp_size * cp_size)) // cp_size
cp_rank = rank % cp_size
```

通信组则用「对全局 rank 取等差数列」的方式批量生成。例如 TP 组：固定一个 `(pp_rank, cp_rank)`，让 `tp_rank` 从 0 走到 `tp_size-1`，对应的 rank 是 `pp_rank*(tp*cp) + tp_rank*cp + cp_rank`，公差为 `cp_size`。代码里就是三个嵌套循环 `range(start, end, stride)`。

`Mapping` 类的 docstring 里画了好几个具体拓扑的例子，是理解这套公式的最佳材料，下面精读时会引用。

#### 4.2.3 源码精读

先看 `MpiTopology` 的三个 rank 公式（`DeviceMeshTopology` 的对应实现是从 process group 读，见后）：

[tensorrt_llm/mapping.py:648-658](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L648-L658) 中文说明：`MpiTopology` 的 `tp_rank` / `pp_rank` / `cp_rank` 三个 property，正是上面三条公式的直接翻译。

通信组的生成算法全部集中在 `_init_parallel_groups`：

[tensorrt_llm/mapping.py:691-741](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L691-L741) 中文说明：用嵌套 `range(..., stride)` 批量生成 pp / cp / tp / moe_tp / moe_cluster / moe_ep 六类通信组的成员列表。其中 pp 组公差为 `tp_size*cp_size`、tp 组公差为 `cp_size`、cp 组为连续段。

以 TP 组为例（这段在 `691-741` 内）：

```python
# init tp group (interleaved ranks with stride of cp_size).
for i in range(self.pp_size):
    for j in range(self.cp_size):
        ranks = range(i * self.tp_size * self.cp_size + j,
                      (i + 1) * self.tp_size * self.cp_size + j,
                      self.cp_size)
        self.tp_groups.append(list(ranks))
```

而「本卡属于哪个组」通过 property 用下标取出：

[tensorrt_llm/mapping.py:660-670](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L660-L670) 中文说明：`tp_group` / `pp_group` / `cp_group` 三个 property，分别用 `(pp_rank*cp_size+cp_rank)` / `(tp_rank*cp_size+cp_rank)` / `(pp_rank*tp_size+tp_rank)` 作下标，从预生成的组列表里取本卡所属组。

这套公式的正确性靠 docstring 的具体例子佐证。以最经典的「8 卡 tp=4 cp=1 pp=2」为例：

[tensorrt_llm/mapping.py:455-467](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L455-L467) 中文说明：8 卡 tp=4 pp=2 的拓扑——2 个 TP 组 `[0,1,2,3]` `[4,5,6,7]`，4 个 PP 组 `[0,4] [1,5] [2,6] [3,7]`。

解读：rank 0-3 是 pp_rank=0（首 stage），4-7 是 pp_rank=1（末 stage）；TP 组把同一 stage 的 4 张卡编在一起切权重；PP 组把两个 stage 里 tp_rank 相同的卡串成流水线（如 rank0→rank4）。

再看一个带 CP 的例子，体会「CP 最内、连续」：

[tensorrt_llm/mapping.py:469-481](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L469-L481) 中文说明：8 卡 tp=4 cp=2 pp=1 的拓扑——4 个 CP 组 `[0,1] [2,3] [4,5] [6,7]`（相邻两卡一组），2 个 TP 组 `[0,2,4,6]` `[1,3,5,7]`（按 cp_size=2 跨步）。

注意 CP 组是**物理相邻的两张卡**，TP 组反而跨步——这正是「cp 内层连续」的直接体现。

> 纯阅读提示：MoE 的 `moe_tp` / `moe_ep` / `moe_cluster` 三类组的划分算法也在同一段代码里（`713-741` 行），公式更长但思路一致。本讲只需知道「MoE 也有自己的一套组」，细节留待 u10-l1（MoE）和 u9-l2（MoE alltoall 通信）展开。

`DeviceMeshTopology` 这一侧，rank 与组都从 PyTorch `DeviceMesh` / `ProcessGroup` 取，构造时不算公式：

[tensorrt_llm/_torch/device_mesh.py:44-72](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L44-L72) 中文说明：`DeviceMeshTopologyImpl` 的 `tp_group_pg` / `pp_group_pg` 等 property 都用 `@require_device_mesh` 装饰，首次访问时懒加载建 mesh，再按维度名 `'tp'`/`'pp'` 取子 mesh 对应的 process group。

注意 `require_device_mesh` 这个装饰器——它保证「第一次访问任意 group 时才真正 `build_mesh()`」：

[tensorrt_llm/_torch/device_mesh.py:17-25](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/device_mesh.py#L17-L25) 中文说明：`require_device_mesh` 在 `device_mesh is None` 时先调 `self.build_mesh()`，实现通信组的懒初始化。

#### 4.2.4 代码实践（手算拓扑）

**实践目标**：手算一个 4 卡 `TP=2, PP=2, CP=1` 的拓扑，说出每个 rank 的角色，并与源码公式对照。

**操作步骤**：

1. 套用 4.2.2 的公式（`tp_size=2, pp_size=2, cp_size=1`）：
   - `pp_rank = rank // (2*1) = rank // 2`
   - `tp_rank = (rank % 2) // 1 = rank % 2`

2. 逐个算 rank 0/1/2/3，填表：

   | rank | pp_rank | tp_rank | 角色（白话） |
   |------|---------|---------|-------------|
   | 0 | 0 | 0 | 首 stage、TP 切片 0 |
   | 1 | 0 | 1 | 首 stage、TP 切片 1 |
   | 2 | 1 | 0 | 末 stage、TP 切片 0 |
   | 3 | 1 | 1 | 末 stage、TP 切片 1 |

3. 推通信组（用 4.2.3 的生成算法）：
   - TP 组：`i∈{0,1}(pp)` × `j∈{0}(cp)`，公差 `cp_size=1`。
     - i=0: `range(0,2,1)=[0,1]`；i=1: `range(2,4,1)=[2,3]` → TP 组 = `{0,1}`、`{2,3}`
   - PP 组：`i∈{0,1}`，公差 `tp_size*cp_size=2`。
     - i=0: `range(0,4,2)=[0,2]`；i=1: `range(1,4,2)=[1,3]` → PP 组 = `{0,2}`、`{1,3}`

4. 解读：rank0、rank1 共同持有首 stage 的层（各拿一半权重，TP）；rank2、rank3 持有末 stage。两条独立流水线 `rank0→rank2` 与 `rank1→rank3` 并行跑，激活在 PP 组内 p2p 传递。

**需要观察的现象 / 预期结果**：TP 组成员必须同属一个 stage；PP 组成员必须同属一个 tp_rank。这正是「同一张卡同时参与 TP 与 PP 两个通信组」的体现。若你有 4 张卡，可写脚本 `Mapping(world_size=4, rank=r, gpus_per_node=8, tp_size=2, pp_size=2)`（`r=0..3`）打印 `.tp_group` / `.pp_group` 验证；**实际多卡打印待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：8 卡 `tp=4 cp=2 pp=1`（见 4.2.3 引用的 docstring）里，rank 5 属于哪个 TP 组、哪个 CP 组？

> **参考答案**：rank 5：`pp_rank = 5//(4*2)=0`；`tp_rank = (5%8)//2 = 2`；`cp_rank = 5%2 = 1`。CP 组（cp_rank=1，pp=0，tp=2）= `range(0*8+2*2, 0*8+3*2) = [4,5]`；TP 组（pp=0，cp=1，tp∈0..3）= `range(0+1, 8+1, 2) = [1,3,5,7]`。所以 rank 5 ∈ CP组`{4,5}`、TP组`{1,3,5,7}`。

**练习 2**：为什么 CP 组设计成「物理相邻」、TP 组反而跨步？

> **参考答案**：CP 组成员要在长序列上频繁交换注意力中间量，通信量大、对延迟敏感，安排在物理相邻（共享 NVLink）的卡上更高效；TP 组的 allreduce 虽也频繁，但与 CP 的排布是「嵌套」决定的——把 cp 放最内层连续编号后，tp 自然就跨步了。这是 NVLink 拓扑与编号约定的联合结果。

---

### 4.3 并行配置：从用户参数到 Mapping 的链路

#### 4.3.1 概念说明

前两模块讲的是「`Mapping` 长什么样」。本模块回答：**用户在 `llm_args` 里填的那些字段，怎么变成一个 `Mapping`？** 这条链路承接 u4-l1 讲过的配置层级（`BaseLlmArgs → TorchLlmArgs`，扁平字段聚合进子配置）。

设计上有两个层次：

1. **用户层（扁平字段）**：`tensor_parallel_size` / `pipeline_parallel_size` / `context_parallel_size` / `moe_tensor_parallel_size` / `moe_expert_parallel_size` / `enable_attention_dp` 等都摊平在 `TorchLlmArgs` 顶层，方便 YAML / CLI 直接写。
2. **内部层（`_ParallelConfig` 子配置 + `Mapping`）**：通过 `model_validator` 把扁平字段聚合成 `_ParallelConfig`，再用 `to_mapping()` 转成 `Mapping`。

为什么要分两层？因为 `TorchLlmArgs` 是面向用户的「大杂烩」，而 `_ParallelConfig` 是「只管并行」的纯结构，`Mapping` 又是「只描述拓扑」的运行时对象。三层逐步收窄职责，且 `_ParallelConfig` 还能独立计算 `world_size`、`devices` 等派生量。

#### 4.3.2 核心流程

转换链路是：

```text
TorchLlmArgs.tensor_parallel_size / pipeline_parallel_size / ...
        │  (model_validator "validate_parallel_config" 聚合)
        ▼
_ParallelConfig(tp_size, pp_size, cp_size, moe_tp_size, ...)
        │  (to_mapping())
        ▼
Mapping(world_size=tp*pp*cp, rank=mpi_rank(), tp_size=..., ...)
```

注意几个映射细节：

- **扁平名 → 内部名**有改名：`tensor_parallel_size`→`tp_size`、`pipeline_parallel_size`→`pp_size`、`context_parallel_size`→`cp_size`、`moe_tensor_parallel_size`→`moe_tp_size`、`moe_expert_parallel_size`→`moe_ep_size`、`moe_cluster_parallel_size`→`moe_cluster_size`。
- **None → -1**：用户没填的 MoE 参数被规范成 `-1`（`Mapping` 用 `-1` 表示「未指定，请自动推导」，见 4.1.3）。
- **rank 来自运行环境**：`to_mapping()` 里 `rank=mpi_rank()`，即从 MPI 或 torch.distributed 取本进程 rank，不由用户指定。

`Mapping` 一旦构造好，就被挂到 `ModelConfig.mapping` 字段，伴随模型一路传到前向；同时 `BaseWorker` 也存一份 `self.mapping` 供调度/KV cache 用。

#### 4.3.3 源码精读

先看用户层的扁平字段定义：

[tensorrt_llm/llmapi/llm_args.py:4155-4228](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4155-L4228) 中文说明：`TorchLlmArgs` 里的扁平并行字段——`tensor_parallel_size`、`pipeline_parallel_size`、`context_parallel_size`、`moe_tensor_parallel_size`、`moe_expert_parallel_size`、`enable_attention_dp`、`pp_partition`、`cp_config` 等，默认值都是 1 / None / False。

注意 `moe_cluster_parallel_size` 已标 `status="deprecated"`（第 4199 行），新代码不要用。

接着看把它们聚合成 `_ParallelConfig` 的校验器：

[tensorrt_llm/llmapi/llm_args.py:4499-4522](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llm_args.py#L4499-L4522) 中文说明：`validate_parallel_config` 这个 `model_validator(mode="after")` 把 MoE 的 None 规范成 `-1`，再用所有扁平字段构造 `_ParallelConfig` 存进私有属性 `_parallel_config`——这正是 u4-l1 讲过的「扁平字段聚合进子配置」模式。

聚合好的 `_ParallelConfig` 通过只读 property 暴露：

[tensorrt_llm/llmapi/llm_args.py:4443-4447](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4443-L4447) 中文说明：`parallel_config` 是只读 property，返回校验器装配好的 `_ParallelConfig`。

`_ParallelConfig` 自己也是个 `StrictBaseModel`，核心是 `world_size` 派生属性与 `to_mapping()`：

[tensorrt_llm/llmapi/llm_args.py:1627-1664](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L1627-L1664) 中文说明：`_ParallelConfig.world_size = tp*pp*cp` 是只读派生；`to_mapping()` 用这些字段构造 `Mapping`，其中 `rank=mpi_rank()` 来自运行环境，`cp_config` 被 `model_dump` 成 dict（注释标注 Mapping 仍用 dict，待迁移）。

转换的真正接入点在 worker 启动时：

[tensorrt_llm/executor/base_worker.py:194](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/executor/base_worker.py#L194) 中文说明：`BaseWorker` 启动时执行 `self.mapping = self.llm_args.parallel_config.to_mapping()`——这是「配置 → Mapping」在引擎里的落地点，之后 worker 全程用 `self.mapping`。

`Mapping` 被注入 `ModelConfig`，前向各处读取：

[tensorrt_llm/_torch/model_config.py:131-134](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/mapping.py#L131-L134) 中文说明：`ModelConfig` 用 dataclass 字段 `mapping: Mapping`（默认 `Mapping()`）携带拓扑到前向。

> 链接修正：上面这段实际位于 `tensorrt_llm/_torch/model_config.py`，正确链接为
> [tensorrt_llm/_torch/model_config.py:131-134](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L131-L134)。中文说明：`ModelConfig` 的 `mapping` 字段默认是一个单卡 `Mapping()`，前向通过它读取 `tp_size` / `attn_tp_size` 等。

例如注意力头数要按 `attn_tp_size` 切：

[tensorrt_llm/_torch/model_config.py:1228-1229](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L1228-L1229) 中文说明：计算注意力本地头数时，`attn_tp_size = mapping.attn_tp_size`（开了注意力 DP 则为 1），`attn_cp_size = mapping.attn_cp_size`——这是 `Mapping` 在前向配置计算中的典型消费。

KV cache 分配也依赖 `pp_layers`：

[tensorrt_llm/_torch/pyexecutor/resource_manager.py:187](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L187) 中文说明：`pp_layers = mapping.pp_layers(base_num_layers)` 决定本 rank 要为哪几层分配 KV cache——这正是 u7-l1 讲过的 KV cache 生命周期里「per-rank 层数」的来源。

#### 4.3.4 代码实践

**实践目标**：用一个真实的 curated 配置文件，追踪「YAML 字段 → `_ParallelConfig` → `Mapping`」的完整链路。

**操作步骤**：

1. 打开 [examples/configs/curated/deepseek-v4-pro-throughput.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/curated/deepseek-v4-pro-throughput.yaml)。已知它含（由前面检索确认）：

   ```yaml
   tensor_parallel_size: 8
   pipeline_parallel_size: 1
   moe_expert_parallel_size: 8
   enable_attention_dp: true
   ```

2. 在脑中跑一遍链路：
   - `validate_parallel_config` 把 `moe_tensor_parallel_size(None→-1)`、`moe_expert_parallel_size=8` 填进 `_ParallelConfig`。
   - `world_size = tp*pp*cp = 8*1*1 = 8`。
   - `to_mapping()` 构造 `Mapping(world_size=8, tp_size=8, pp_size=1, moe_ep_size=8, enable_attention_dp=True)`。
   - 进入 `Mapping.__init__`：因为开了 `enable_attention_dp`，注意力的 `attn_tp_size` 实际会被模型侧当 1 处理（权重复制、各卡跑不同请求）；MoE 走 EP=8（8 个专家各占一卡）。

3. 对照 4.1.3 的自洽校验，确认 `moe_tp_size * moe_ep_size * moe_cluster_size == moe_world_size`：这里 `moe_tp_size` 由 `-1` 推导为 `moe_world_size // (moe_ep_size * moe_cluster_size) = 8 // (8*1) = 1`，于是 `1*8*1 = 8 == moe_world_size(8)` ✓。

**需要观察的现象 / 预期结果**：你能口述出「这个配置等价于 8 卡全 EP（每卡 1 专家）+ 注意力 DP（KV cache 分区、权重复制）」。这正是 DeepSeek 类大 MoE 模型高吞吐的典型布局。如果你想真正跑起来，需要 8 张可用 GPU 与模型权重（按 u1-l2/u1-l3 的 `trtllm-serve --config` 方式），**完整运行待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：用户在 YAML 里写 `tensor_parallel_size: 4` 但不写 `moe_tensor_parallel_size` 和 `moe_expert_parallel_size`，最终 MoE 走什么并行？

> **参考答案**：两个字段都是 None，`validate_parallel_config` 把它们规范成 `-1`；`Mapping.__init__` 进入「两者都 -1」分支，令 `moe_tp_size = moe_world_size // moe_cluster_size = 4`、`moe_ep_size = 1`（见 mapping.py:112-114）。即默认**全 TP**，没有专家切分。

**练习 2**：为什么 `to_mapping()` 用 `rank=mpi_rank()` 而不让用户在 YAML 里指定 rank？

> **参考答案**：rank 是「本进程在全局里的编号」，由运行时编排（MPI/Ray/SLURM）决定，每个进程不同；它不是部署配置，而是运行时身份。让用户写会破坏「一份 YAML 多进程共用」的模型。

---

## 5. 综合实践

把三个模块串起来，完成一个小任务：**为一个假想的 8 卡部署，设计并行拓扑并验证它能在 `Mapping` 里自洽表达**。

**场景**：你想在单节点 8 卡上部署一个中等大小的稠密模型，目标是「单卡放不下、但又想尽量少 PP（PP 会引入流水线气泡）」。

**任务**：

1. 选择 `TP=4, PP=2, CP=1`，写出对应的 YAML 片段（`tensor_parallel_size: 4`、`pipeline_parallel_size: 2`）。
2. 手算每个 rank 的 `pp_rank` / `tp_rank`（用 4.2 的公式），列出 8 个 rank 的角色表。
3. 写出全部 TP 组与 PP 组的成员（套用 `_init_parallel_groups` 的生成规则）。
4. 解释：在这种布局下，模型的层（假设 32 层）如何被 `pp_layers(32)` 切分？哪几个 rank 持有第 0 层、哪几个持有第 31 层？
5. 进阶：若改用 `TP=2, PP=2, CP=2`（同样 8 卡），重新算 CP 组与 TP 组，对比两种布局的通信组差异。

**参考思路**（第 2、3 步）：

- 公式（tp=4, pp=2, cp=1）：`pp_rank = rank//4`、`tp_rank = rank%4`。
  - rank 0-3：pp_rank=0；rank 4-7：pp_rank=1。
- TP 组（公差 cp=1）：i=0→`[0,1,2,3]`，i=1→`[4,5,6,7]`。
- PP 组（公差 tp*cp=4）：i=0→`[0,4]`，i=1→`[1,5]`，i=2→`[2,6]`，i=3→`[3,7]`。

第 4 步：`pp_layers(32)` 在 `pp_partition=None` 时用 `torch.tensor_split` 均分——pp_rank=0 持层 0-15，pp_rank=1 持层 16-31。所以第 0 层在 rank 0-3（首 stage），第 31 层在 rank 4-7（末 stage）。

第 5 步（**待本地验证**）：`TP=2,PP=2,CP=2` 时，CP 组为相邻两卡（`{0,1}{2,3}{4,5}{6,7}`），TP 组跨步 cp=2（`{0,2}{1,3}{4,6}{5,7}`），PP 组公差 tp*cp=4（`{0,4}{1,5}{2,6}{3,7}`）。对比可见：引入 CP 后，原本「同 stage 4 卡一组」的 TP 组被拆成更小的 2 卡组，长上下文的注意力被分摊，但 TP allreduce 的组变小了——这正是 CP 与 TP 的权衡。

## 6. 本讲小结

- **`Mapping` 是并行拓扑的「单一事实源」**：一个对象同时描述了每张卡的 stage、TP/CP 局部 rank、负责的层、持有的专家，以及它属于哪些通信组。
- **全局 rank 的嵌套约定**：`global = pp*(tp*cp) + tp*cp + cp`，即 CP 最内层连续、TP 居中跨步、PP 最外层；这个约定决定了所有通信组的物理布局。
- **通信组靠 `range(start, end, stride)` 批量生成**：PP 组公差 `tp*cp`、TP 组公差 `cp`、CP 组为连续段；MoE 另有一套 tp/ep/cluster 组。
- **两条实现路径**：`MpiTopology`（MPI 编排，纯 Python 算公式 + 组）与 `DeviceMeshTopology`（Ray 编排，懒加载 PyTorch `DeviceMesh`），由 `TLLM_DISABLE_MPI` 选择，上层无感。
- **配置链路三段式**：用户扁平字段（`tensor_parallel_size` 等）→ `model_validator` 聚合成 `_ParallelConfig` → `to_mapping()` 生成 `Mapping` → 挂到 `ModelConfig.mapping` 与 `BaseWorker.mapping` 供前向/KV cache/调度读取。
- **派生与自洽**：MoE / 注意力的 tp/ep/cp 是从顶层 tp/pp/cp 推导的派生量，构造时有多道校验（`world_size==tp*pp*cp` 等）保证拓扑自洽。

## 7. 下一步学习建议

本讲只讲了「拓扑如何被描述」。接下来：

- **u9-l2 分布式通信原语**：讲 `Mapping` 之上真正收发数据的 `communicator` / `allreduce_helper` / `moe_alltoall`——你会看到 `mapping.tp_group` 等「描述」是如何变成实际 NCCL allreduce / alltoall 调用的。
- **u10-l1 MoE 架构与后端**：深入 `moe_tp` / `moe_ep` / `moe_cluster` 三类组的实际用法与 Wide-EP（`dwdp_size`）。
- **回看 u4-l1**：若对 `model_validator` 聚合扁平字段的模式还不熟，回头读 `_ParallelConfig` 与 `_ParallelConfig`/`SchedulerConfig` 等子配置的对照。
- **源码延伸阅读**：`tensorrt_llm/_torch/distributed/communicator.py` 看 `mapping.world_size` / `mapping.tp_size` / `mapping.pp_rank` 如何被通信层消费；`docs/source/features/parallel-strategy.md` 的 Wide-EP 章节了解大 MoE 的高级负载均衡。
