# 并行策略：TP/SP/DP/CFG/PP/HSDP/VAE

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 vLLM-Omni 扩散（Diffusion）子系统支持哪几种并行维度（TP / SP / PP / CFG / DP / HSDP / VAE），以及它们各自「切的是什么」。
- 读懂 [`parallel_state.py`](../vllm_omni/diffusion/distributed/parallel_state.py) 里的 `initialize_model_parallel`，并解释它如何用一个 `RankGenerator` 按固定顺序 `tp-sp-pp-cfg-dp` 一次性生成所有正交并行组。
- 给定总 GPU 数和一组并行度配置，手算出每个 rank 属于哪个 TP/SP/CFG/PP/DP 组。
- 理解 CFG 并行、HSDP（权重分片）、VAE Patch 并行三者的设计动机与适用场景，以及它们与前几讲（u7-l1 注意力后端、u7-l2 序列并行）的关系。

本讲是 U7（Diffusion 加速）里偏「全局」的一讲：前几讲讲的是「单条注意力/单步去噪怎么加速」，本讲讲的是「整张 GPU 卡牌怎么编排」。它把 u7-l2 序列并行里提到的 `ring_degree × ulysses_degree = sequence_parallel_size` 放回更大的并行组框架里。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 为什么扩散模型需要这么多并行维度

扩散 Transformer（DiT）去噪时，单步前向要做的事情是：把一段 latent（潜变量）和一个文本 embedding 喂给一个大 Transformer，预测噪声。这条前向链上能「分」的地方有五个层面，对应五种并行：

| 层面 | 被切的对象 | 直觉比喻 |
|------|-----------|---------|
| **TP**（Tensor Parallel，张量并行） | 权重矩阵的行/列 | 一道大题，几个同学各做一半，最后对答案（all-reduce） |
| **SP**（Sequence Parallel，序列并行） | 输入序列的长度 | 一本厚书，几人各读几页 |
| **PP**（Pipeline Parallel，流水线并行） | 模型的「层」 | 工厂流水线，前一道工序做完传给下一道 |
| **CFG**（Classifier-Free Guidance Parallel） | 同一步里的正/负 prompt 双前向 | 同一时刻有「正面」和「反面」两个任务，分给两个人同时做 |
| **DP**（Data Parallel，数据并行） | 不同的请求/批 | 两台收银台，各服务各的顾客 |

前四种（TP/SP/PP/CFG）会让一组 rank **协作完成同一个请求**，因此它们之间需要通信；DP 让一组 rank **各跑各的请求**，只在少数地方同步。

本讲还会涉及两种「辅助」并行：

- **HSDP**（Hybrid Sharded Data Parallel，混合分片数据并行）：不是切计算，而是**切权重本身**——把权重分散存到多卡上，前向时按需 gather。用来让大模型（如 Wan2.2 14B）在显存小的卡上跑起来。
- **VAE Patch Parallel**：切的是 VAE 编解码时的**空间分块（tile）**，加速去噪之后那张大图的解码。

### 2.2 一个关键概念：正交并行组

把这五种并行叠在一起，难点是「怎么给每个 rank（每张卡）编号，让它清楚地知道自己属于哪个 TP 组、哪个 SP 组……」。

Megatron-LM 提出的经典做法是：**把 world_size 按固定顺序连乘分解，每个 rank 的全局编号唯一决定了它在各维度里的局部坐标。** 假设顺序是 `tp-sp-pp-cfg-dp`，则：

\[
\text{global\_rank} = \text{tp\_rank} + \text{sp\_rank}\cdot \text{tp\_size} + \text{pp\_rank}\cdot \text{tp\_size}\cdot \text{sp\_size} + \text{cfg\_rank}\cdot \text{tp\_size}\cdot \text{sp\_size}\cdot \text{pp\_size} + \text{dp\_rank}\cdot (\cdots)
\]

这张方程就是本讲源码的核心数学。一旦理解它，所有 `get_tp_group()` / `get_sp_group()` 查出来的组都能手算出来。

> 术语提示：**world_size** = 参与本次推理的总进程数（通常等于 GPU 数）；**rank** = 某个进程的全局编号（0 到 world_size-1）；**group（组）** = 一组需要互相通信的 rank 子集；**正交** = 五种并行的切分维度互不重叠，连乘恰好等于 world_size。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vllm_omni/diffusion/distributed/parallel_state.py](../vllm_omni/diffusion/distributed/parallel_state.py) | **本讲主角**。定义所有并行组的全局单例、`RankGenerator`、`initialize_model_parallel` 与所有 `get_*_group` 访问器。 |
| [vllm_omni/diffusion/distributed/group_coordinator.py](../vllm_omni/diffusion/distributed/group_coordinator.py) | `GroupCoordinator` / `SequenceParallelGroupCoordinator` / `PipelineGroupCoordinator` 的实现，是每个并行组的「通信大脑」。本讲只了解它的角色，不深入。 |
| [vllm_omni/diffusion/data.py](../vllm_omni/diffusion/data.py) | `DiffusionParallelConfig`：用户配置入口，把 `tensor_parallel_size`、`sequence_parallel_size`、`cfg_parallel_size`、`use_hsdp`、`vae_patch_parallel_size` 等字段聚合在一起。 |
| [docs/design/feature/tensor_parallel.md](../docs/design/feature/tensor_parallel.md) | TP 接入指南：如何把 `nn.Linear` 换成 `ColumnParallelLinear`/`RowParallelLinear`/`QKVParallelLinear`。 |
| [docs/design/feature/cfg_parallel.md](../docs/design/feature/cfg_parallel.md) | CFG 并行指南：`CFGParallelMixin`、`predict_noise_maybe_with_cfg`、N 分支 CFG。 |
| [docs/design/feature/hsdp.md](../docs/design/feature/hsdp.md) | HSDP 指南：`_hsdp_shard_conditions`、`apply_hsdp_to_model`。 |
| [docs/design/feature/vae_parallel.md](../docs/design/feature/vae_parallel.md) | VAE Patch 并行指南：`split/exec/merge` 与 spatial-shard 解码。 |

## 4. 核心概念与源码讲解

### 4.1 RankGenerator：用 mask 生成正交并行组

#### 4.1.1 概念说明

`RankGenerator` 是 vLLM-Omni 编排所有并行的「发牌器」。它改编自 Megatron-LM 与 xDiT（见文件头版权声明），解决一个问题：给定五种并行度和一个固定顺序，生成每种并行对应的「rank 列表」。

它的核心思想是：**用 mask（掩码）在同一个分解方程里挑出不同的子集**。比如顺序是 `tp-sp-pp-cfg-dp`，那么：

- 取 TP 组时，mask = `[True, False, False, False, False]`，意思是「只让 tp 维度变化，其余维度固定」，于是固定 (sp,pp,cfg,dp) 的 rank 凑成一组。
- 取 SP 组时，mask = `[False, True, False, False, False]`，让 sp 维度变化。
- 取 CFG 组时，mask = `[False, False, False, True, False]`，让 cfg 维度变化。

这种「一个方程 + 多个 mask」的好处是：五种并行组来自同一套数学，天然保证彼此**正交**（任意两个不同并行组的 rank 最多相交一个元素）。

#### 4.1.2 核心流程

`RankGenerator` 的工作分两步：

1. **构造期**（`__init__`）：接收 tp/sp/pp/cfg/dp/fs 六个并行度，校验 `order` 字符串里是否声明了所有「非 1」的维度，然后按 `order` 顺序排好 `ordered_size`。
2. **取组期**（`get_ranks(token)`）：把 `token`（如 `"tp"`、`"cfg"`、`"cfg-dp"`）翻译成 mask，调用 `generate_masked_orthogonal_rank_groups` 算出 rank 列表。

其内部数学函数 `generate_masked_orthogonal_rank_groups` 用「前缀积 + 取模分解」把 `group_index` 与 `rank_in_group` 拆成多维坐标，再分别乘上对应的 stride（步长）求和还原全局 rank。流程可概括为：

```text
ordered_size = [tp, sp, pp, cfg, dp]   # 按 order 排好
strides      = prefix_product(ordered_size)   # [1, tp, tp*sp, ...]
for 每个组编号 group_index in range(num_of_group):
    固定维坐标 = decompose(group_index, unmasked_shape)      # 非目标维度的坐标
    for 组内编号 rank_in_group in range(group_size):
        变化维坐标 = decompose(rank_in_group, masked_shape)  # 目标维度的坐标
        global_rank = dot(变化维坐标, masked_stride) + dot(固定维坐标, unmasked_stride)
```

`prefix_product`（前缀积）算 stride、`decompose`（取模分解）还原多维坐标、`inner_product`（内积）加权求和，这三个小函数就是整套机制的「算术核」。

#### 4.1.3 源码精读

先看 `RankGenerator.__init__` 如何接收并行度并校验顺序（这里只看关键片段）：

[parallel_state.py:177-221](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L177-L221) —— `RankGenerator` 把六个并行度存进 `name_to_size` 字典，并强制要求：**任何 `size != 1` 的维度都必须出现在 `order` 字符串里**，否则直接 `raise RuntimeError`。这保证你不会「配了 sp=2 却没把它写进 order」，从而算出错误的组。

`get_ranks` 把 token 翻译成 mask 并调用核心函数：

[parallel_state.py:235-265](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L235-L265) —— 注意 `token` 可以用连字符组合，例如 `"cfg-dp"` 表示「CFG 与 DP 联合的组」（后面讲 MoE 时会用到）。`get_mask` 会把组合 token 拆开，把对应位置都置 `True`。

核心数学函数（重点看它的 docstring 例子）：

[parallel_state.py:69-120](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L69-L120) —— 文档里给了 `parallel_size=[2,3,4]`、mask=`[False,True,False]`（取 dp 组）的完整推导：`dp_group[0] = [0,2,4]`、`dp_group[1] = [1,3,5]`……。读完这段 docstring 就能复现任意配置。算术实现在 L122-174 的三个内部函数。

`fs`（fully shard / HSDP 分片维）走特殊分支，因为它不参与正交分解：

[parallel_state.py:249-257](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L249-L257) —— 当 `independent_ranks=True` 且 `token=="fs"` 时，直接按 `world_size // fs` 切成若干连续块（如 world_size=8、fs=4 → `[[0,1,2,3],[4,5,6,7]]`），不走正交公式。这是 HSDP 的特殊性，下一节会展开。

#### 4.1.4 代码实践

> 实践目标：亲手用 `RankGenerator` 生成几种并行组，验证你对 mask 的理解。
>
> 操作步骤（这是**示例代码**，可直接在装好 vllm-omni 的环境里跑）：

```python
# 示例代码：不依赖真实分布式环境，纯逻辑验证
from vllm_omni.diffusion.distributed.parallel_state import RankGenerator

# 模拟 8 卡、tp=2/sp=2/cfg=2（pp=1, dp=1）
rg = RankGenerator(tp=2, sp=2, pp=1, cfg=2, dp=1, order="tp-sp-pp-cfg-dp")
print("TP 组:", rg.get_ranks("tp"))   # [[0,1],[2,3],[4,5],[6,7]]
print("SP 组:", rg.get_ranks("sp"))   # [[0,2],[1,3],[4,6],[5,7]]
print("CFG 组:", rg.get_ranks("cfg")) # [[0,4],[1,5],[2,6],[3,7]]
print("CFG-DP 组:", rg.get_ranks("cfg-dp"))
```

> 需要观察的现象：TP 组里相邻两个 rank 成对（因为 tp stride=1）；SP 组里两个 rank 差 2（sp stride=2）；CFG 组里两个 rank 差 4（cfg stride=4）。
>
> 预期结果：与注释里的列表完全一致。这一步只需 import `RankGenerator`，不会真正建 `torch.distributed` 进程组，所以单机单卡也能跑。
>
> 若运行报错（例如缺少分布式初始化），请确认只 import 了 `RankGenerator` 类本身，不要触发 `get_world_group()`。

#### 4.1.5 小练习与答案

**练习 1**：在 `tp=2/sp=2/cfg=2`（8 卡）下，rank 6 同时属于哪几个 TP/SP/CFG 组？

**参考答案**：rank 6 = `tp=0 + sp=1*2 + cfg=1*4`，即 `(tp=0, sp=1, cfg=1)`。它的 TP 组是固定 `(sp=1,cfg=1)` 让 tp 变化 → `{6,7}`；SP 组固定 `(tp=0,cfg=1)` 让 sp 变化 → `{4,6}`；CFG 组固定 `(tp=0,sp=1)` 让 cfg 变化 → `{2,6}`。

**练习 2**：如果把 `dp` 也设成 2（即 16 卡、tp=2/sp=2/cfg=2/dp=2），DP 组会长什么样？

**参考答案**：DP 让一个 rank 各跑各的请求，所以 DP 组里应是「其余维度都相同、只 dp 维不同」的 rank。dp stride = `tp*sp*pp*cfg = 2*2*1*2 = 8`，所以 DP 组两两差 8：`[[0,8],[1,9],…,[7,15]]`，共 8 个组。

---

### 4.2 initialize_model_parallel：把发牌器装进全局单例

#### 4.2.1 概念说明

`RankGenerator` 只是算术工具，真正把这些组「注册」成可被全局访问的 `GroupCoordinator` 对象的，是 `initialize_model_parallel`。它是每个 worker 进程启动时的必经之路（见 u5-l3 讲的 `DiffusionWorker.init_device` 流程）。

它的职责有三：
1. 校验配置自洽（SP 三种模式互斥、world_size 够大、SP 尺寸与 ulysses/ring 度数匹配）。
2. 用一个 `RankGenerator` 实例生成所有并行组的 rank 列表。
3. 为每种并行创建对应的 `GroupCoordinator`，挂到模块级全局变量（`_DP`/`_CFG`/`_SP`/`_PP`/`_FS`）上，并把 TP/PP 同步给上游 vLLM 的 `vllm_parallel_state`（因为扩散的 TP 组要复用 vLLM 的并行层实现）。

#### 4.2.2 核心流程

`initialize_model_parallel` 的主流程可概括为：

```text
1. 解析 SP 模式：
   - AllGather-KV 模式：要求 ulysses=1 且 ring=1
   - 否则：sequence_parallel_size 必须 == ring_degree * ulysses_degree
2. 算 dit_parallel_size = dp * cfg * sp * pp * tp   # 协作完成同一请求的总卡数
3. 判定是否 standalone HSDP（所有 DiT 维度都为 1 且 fs>1）
4. 构造 RankGenerator(tp, sp, pp, cfg, dp, fs, order="tp-sp-pp-cfg-dp")
5. 依次为 dp / cfg / pp / sp / tp / fs 各创建一个 GroupCoordinator
   - 其中 SP 还要额外调用 set_seq_parallel_pg 拆出 ulysses/ring/allgather 子组
   - TP/PP 同时注册一份给 vllm_parallel_state
6. 创建 DiT 总组（init_dit_group），用于跨所有 DiT rank 的 all-reduce
```

这里有一个与 u7-l2 强相关的铁律：SP 的尺寸校验在 [parallel_state.py:800-809](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L800-L809)——`expected_sequence_parallel_size = ring_degree * ulysses_degree`（AllGather-KV 时为 `allgather_degree`），与用户传的 `sequence_parallel_size` 不一致就 `raise ValueError`。这正是 u7-l2 讲的 `ring_degree × ulysses_degree = sequence_parallel_size` 的来源。

#### 4.2.3 源码精读

函数签名与默认顺序 `tp-sp-pp-cfg-dp`：

[parallel_state.py:728-746](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L728-L746) —— 注意 `order` 参数默认就是 `"tp-sp-pp-cfg-dp"`，这是 vLLM-Omni 的固定编排顺序。docstring（L762-787）给了一个 16 卡、`dp=2/cfg=2/sp=2/pp=2` 的完整例子，列出了每种组的具体 rank 列表，是手算的最佳参照。

dit_parallel_size 与 standalone HSDP 判定：

[parallel_state.py:811-836](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L811-L836) —— `dit_parallel_size = dp * cfg * sp * pp * tp` 表示「协作完成同一请求的卡数」；当它等于 1 且 `fully_shard_degree > 1` 时，进入 standalone HSDP 分支（把 `dit_parallel_size` 临时改成 `fully_shard_degree * hsdp_replicate_size`，让正交分解对纯 HSDP 也成立）。

RankGenerator 构造与各组的创建：

[parallel_state.py:838-901](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L838-L901) —— 这里依次 `_DP`、`_CFG`、`_PP`、`_SP` 全局单例。SP 组特殊：它先调用 `set_seq_parallel_pg`（L885-892）把每个 SP 组再拆成 ulysses / ring / allgather 三个子进程组，再连同这三个子组一起交给 `SequenceParallelGroupCoordinator`。这把 u7-l2 的 Ring/Ulysses 通信原语「挂」到了正确的 rank 子集上。

TP 组与上游 vLLM 的对接：

[parallel_state.py:915-930](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L915-L930) —— TP 组被同时挂到 omni 自己的 `init_model_parallel_group(parallel_mode="tensor")` 与上游 `vllm_parallel_state._TP`。原因是扩散 Transformer 复用了 vLLM 的 `ColumnParallelLinear` / `QKVParallelLinear` 等并行层（见 tensor_parallel.md），这些层内部会查 `vllm_parallel_state._TP` 来做 all-reduce，所以两边必须指向同一组 rank。

#### 4.2.4 代码实践

> 实践目标：阅读型实践——跟踪一个 worker 进程里 `initialize_model_parallel` 的调用链，理解它在哪里被调用、参数从哪来。
>
> 操作步骤：
> 1. 打开 `vllm_omni/diffusion/worker/diffusion_worker.py`，找到 `init_device` 方法。
> 2. 在其中搜索 `initialize_model_parallel` 的调用点，观察它传入的 `tensor_parallel_size`、`sequence_parallel_size`、`cfg_parallel_size` 等参数来自哪个配置对象（应是 `DiffusionParallelConfig` / `current_omni_platform`）。
> 3. 对照 [data.py 的 DiffusionParallelConfig](../vllm_omni/diffusion/data.py) 字段，确认每个并行度都有一个对应的 CLI 参数（如 `--tensor-parallel-size`、`--cfg-parallel-size`）。
>
> 需要观察的现象：`initialize_model_parallel` 在 `init_distributed_environment`（建立 NCCL/HCCL 通信域）**之后**、模型加载**之前**被调用——因为并行层在构造期就需要知道自己属于哪个 TP 组。
>
> 预期结果：能画出 `init_device → init_distributed_environment → initialize_model_parallel → load_model` 的顺序链路。

#### 4.2.5 小练习与答案

**练习**：`initialize_model_parallel` 为什么要把 TP 组同时注册给 omni 和 `vllm_parallel_state`，而 CFG 组只注册给 omni？

**参考答案**：因为扩散 Transformer 复用了 vLLM 的并行层（`QKVParallelLinear` 等），这些层在前向时通过 `vllm_parallel_state._TP` 触发 all-reduce，所以 TP 必须双写。CFG 是 omni 专属的并行维度（上游 vLLM 没有这个概念），扩散代码通过 `CFGParallelMixin` 自己用 omni 的 `_CFG` 组做 `all_gather`，所以只在 omni 侧注册。

---

### 4.3 各 get_*_group 访问器与 DiT 总组

#### 4.3.1 概念说明

并行组一旦注册成全局单例，模型代码（transformer、attention、pipeline）就通过一组 `get_xxx_group()` 访问器拿到自己所属的 `GroupCoordinator`，再在其上做 `all_reduce` / `all_gather` / `send_recv`。这是「查询层」——把「我是谁、我和谁一组」封装成一行调用。

除了五种正交组，还有一个**DiT 总组**：它包含所有协作完成同一请求的 rank（即 `tp*sp*pp*cfg*dp`），用于需要跨全部 DiT 卡同步的场景（比如 VAE 解码前的汇总）。

#### 4.3.2 核心流程

每个访问器的模式都一样：`assert 全局变量 is not None`，然后返回它。关键是要知道每个组「代表哪些 rank 一起通信」：

| 访问器 | 对应并行 | 典型通信 |
|--------|---------|---------|
| `get_world_group()` | 全部 rank | 退化场景 |
| `get_sp_group()` → `get_ulysses/ring/allgather_*` | 序列并行 | all-to-all / P2P（u7-l2） |
| `get_pp_group()` | 流水线 | send/recv（首末阶段判定） |
| `get_cfg_group()` | CFG 并行 | all_gather 正负预测 |
| `get_dp_group()` | 数据并行 | 极少同步 |
| `get_fs_group()` | HSDP 分片 | 权重 gather |
| `get_dit_group()` | DiT 总组 | 全 DiT all-reduce |

#### 4.3.3 源码精读

几个有代表性的访问器：

[parallel_state.py:275-303](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L275-L303) —— SP 组的访问器。`get_sp_group()` 返回 `SequenceParallelGroupCoordinator`，而 `get_ulysses_parallel_rank()` / `get_ring_parallel_rank()` / `get_allgather_parallel_rank()` 则从它身上取 ulysses / ring / allgather 三个子组的局部 rank——这正是 u7-l2 讲的「SP 内部再细分子组」的查询入口。

[parallel_state.py:320-342](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L320-L342) —— PP 组访问器，额外提供 `is_pipeline_first_stage()` / `is_pipeline_last_stage()`，用于流水线里判断「我是不是第一段/最后一段」，决定要不要接收/发送激活。

[parallel_state.py:346-374](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L346-L374) —— CFG 组与 DP 组访问器。注意 docstring 里 CFG 注释为 `classifier_free_guidance parallel group`，DP 注释（沿用了模板）写的是 `pipeline model parallel group`（笔误，实为 data parallel），以源码行为为准。

DiT 总组与 `is_dp_last_group`：

[parallel_state.py:393-410](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L393-L410) —— `get_dit_world_size()` 返回 `dp * cfg * sp * pp * tp`（即一个请求占用的总卡数）。`is_dp_last_group()` 判断当前 rank 是否是 SP/CFG/PP 三个维度的「最后一个」，常用于决定「该由谁来发起下一步的汇总通信」。

DiT 总组的创建：

[parallel_state.py:537-547](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L537-L547) —— `init_dit_group` 用前 `dit_parallel_size` 个 rank 建一个普通 `torch.distributed` 进程组（注意它不是 `GroupCoordinator`，而是裸 `ProcessGroup`），在 `initialize_model_parallel` 末尾被调用。

#### 4.3.4 代码实践

> 实践目标：在 transformer / pipeline 源码里找到这些访问器的真实使用点，理解它们「在哪一步触发通信」。
>
> 操作步骤：
> 1. 在 `vllm_omni/diffusion/models/` 下任选一个 transformer（如 `qwen_image_transformer.py`），搜索 `RowParallelLinear`，确认它内部隐式使用 TP 组做 all-reduce。
> 2. 在 `vllm_omni/diffusion/distributed/cfg_parallel.py` 里搜索 `get_cfg_group`，找到 CFG 并行做 `all_gather` 的代码。
> 3. 在 `vllm_omni/diffusion/distributed/hsdp.py` 里搜索 `get_fs_group`，看 HSDP 如何 gather 权重。
>
> 需要观察的现象：访问器调用总是出现在「需要跨 rank 同步」的瞬间——all-reduce 在 RowParallel 之后、all_gather 在两条 CFG 前向之后、权重 gather 在 FSDP 前向之前。
>
> 预期结果：能说出每个访问器对应的通信原语及其触发时机。

#### 4.3.5 小练习与答案

**练习**：`get_dit_world_size()` 和 `get_data_parallel_world_size()` 有什么区别？

**参考答案**：前者是「协作完成同一请求的总卡数」= `dp*cfg*sp*pp*tp`；后者只是 DP 组的 size（不同请求各跑各的副本数）。一个请求内部的所有 rank 都在同一个 DiT 组里，但分散在不同的 DP 组里。

---

### 4.4 CFG 并行的设计意图

#### 4.4.1 概念说明

CFG（Classifier-Free Guidance，无分类器引导）是扩散模型提升生成质量的标配技巧：每一步去噪要跑**两次** transformer 前向——一次用正向 prompt（条件），一次用空/负 prompt（无条件），再用公式合并：

\[
\hat{\varepsilon} = \varepsilon_{\text{neg}} + s\cdot(\varepsilon_{\text{pos}} - \varepsilon_{\text{neg}})
\]

其中 \(s\) 是 guidance scale。问题在于：两次前向让每步耗时翻倍。**CFG 并行**的做法是——既然这两次前向输入不同、彼此独立，那就把它们分给不同的 GPU rank **同时跑**，再用 `all_gather` 合并。这就把「串行两次前向」变成「并行一次前向」，在显存允许时几乎免费翻倍。

#### 4.4.2 核心流程

CFG 并行的实现不在 `parallel_state.py` 里造组，而在 `CFGParallelMixin` 里用 `_CFG` 组通信。流程是：

```text
do_true_cfg = True 且 cfg_world_size > 1 时：
  rank 0 跑 positive 前向 → ε_pos
  rank 1 跑 negative 前向 → ε_neg
  all_gather 把两个结果广播给所有 rank
  每个 rank 本地用 CFG 公式合并（确定性，结果一致）
  scheduler.step 也本地执行（无需再 broadcast，因为噪声预测已一致）
```

当 `cfg_world_size == 1` 时退化成「单卡串行跑两次前向再合并」。

#### 4.4.3 源码精读

CFG 组在 `parallel_state.py` 里的注册：

[parallel_state.py:865-872](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L865-L872) —— `_CFG = init_model_parallel_group(rank_generator.get_ranks("cfg"), ..., parallel_mode="classifier_free_guidance")`。注意 CFG 组的 rank 来自 `get_ranks("cfg")`，即「固定 (tp,sp,pp,dp)，只让 cfg 维变化」的 rank——所以同一 CFG 组里的两张卡，权重完全相同（TP/SP 都一样），只是分工算正/负前向。

CFG 并行的使用侧（见 cfg_parallel.md）：

[cfg_parallel.md:46-62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/cfg_parallel.md#L46-L62) —— `predict_noise_maybe_with_cfg()` 自动检测 `cfg_world_size > 1`：是则分布双前向再 all_gather，否则单卡串行。`scheduler_step_maybe_with_cfg()` 让所有 rank 本地步进，因为 all_gather+本地合并已保证噪声预测一致。

N 分支 CFG（3+ 分支）：

[cfg_parallel.md:64-78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/cfg_parallel.md#L64-L78) —— 某些模型（BAGEL、OmniGen2 用 3 分支，DreamID Omni 用 4 分支）需要多于 2 个 CFG 分支。`predict_noise_with_multi_branch_cfg()` 用 round-robin（轮转）把 N 个分支派给 M 张卡：分支 `i` → rank `i % M`。当 N > M 时一个 rank 串行跑多个分支。

#### 4.4.4 代码实践

> 实践目标：用 CLI 启用 CFG 并行并对比耗时。
>
> 操作步骤（需多卡环境，**待本地验证**）：
> ```bash
> cd examples/offline_inference/text_to_image
> python text_to_image.py \
>     --model Qwen/Qwen-Image \
>     --prompt "a cup of coffee on the table" \
>     --negative-prompt "ugly, unclear" \
>     --cfg-scale 4.0 --num-inference-steps 50 \
>     --output "cfg_off.png"
> # 同样的命令加 --cfg-parallel-size 2，记录日志里的 e2e_time_ms
> python text_to_image.py ... --cfg-parallel-size 2 --output "cfg_on.png"
> ```
>
> 需要观察的现象：开 `--cfg-parallel-size 2` 后日志应出现「CFG parallel activated」，`e2e_time_ms` 相比单卡 CFG 明显下降。
>
> 预期结果：两张图视觉质量一致（CFG 并行不改变数值，只改执行顺序），但带 CFG 并行的版本更快。若 `guidance_scale <= 1.0` 或没给负 prompt，CFG 不启用，自然也不会触发并行。

#### 4.4.5 小练习与答案

**练习**：为什么 CFG 并行在 `scheduler_step` 阶段不需要 broadcast？

**参考答案**：因为 `predict_noise_maybe_with_cfg` 在 all_gather 之后，让每个 rank 都拿到了完整的 (ε_pos, ε_neg) 并本地合并出相同的 ε̂。既然所有 rank 的噪声预测完全一致，scheduler 步进又是确定性的，本地各自算就能得到相同的 latent，无需再通信。

---

### 4.5 HSDP 与 VAE 并行的设计意图

这两种并行都不在 `tp-sp-pp-cfg-dp` 的正交主链上，而是「侧挂」的优化，本节一并讲。

#### 4.5.1 HSDP：切权重而非切计算

**概念**：HSDP（Hybrid Sharded Data Parallel）用 PyTorch FSDP2 把**模型权重本身**分片存到多卡上，前向时按需 gather 权重再算。它解决的是「模型太大单卡装不下」的问题（如 Wan2.2 14B），而非加速。

**关键约束**（见 hsdp.md）：
- HSDP **不能与 TP 同时用**（两者都管权重切分，会冲突）。
- standalone HSDP（无其他并行）时必须显式指定 `hsdp_shard_size`。

**源码精读**：HSDP 在 `parallel_state.py` 里体现为 `fs`（fully shard）维度，走 `independent_ranks=True` 的特殊分支，不参与正交分解：

[parallel_state.py:941-948](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L941-L948) —— `_FS = init_model_parallel_group(rank_generator.get_ranks("fs", independent_ranks=True), ..., parallel_mode="fully_shard")`。结合 4.1.3 讲的 `get_ranks("fs", independent_ranks=True)`（L249-257），FS 组就是按 `fs` 大小连续切块。

standalone HSDP 的判定与 dit_parallel_size 调整：

[parallel_state.py:816-821](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L816-L821) —— 当 `dit_parallel_size == 1` 且 `fully_shard_degree > 1` 时，把 `dit_parallel_size` 临时设成 `fully_shard_degree * hsdp_replicate_size`，使正交分解对纯 HSDP 也成立。模型侧通过 `_hsdp_shard_conditions` 声明哪些模块（通常是 transformer block）要分片，由 `apply_hsdp_to_model` 执行 FSDP2 包装（见 hsdp.md）。

#### 4.5.2 VAE Patch 并行：切解码空间

**概念**：去噪结束后，latent 要经 VAE 解码成最终图像/视频。VAE 解码对大图很慢且吃显存。**VAE Patch 并行**把 latent 切成多个带重叠的空间 tile（瓦片），分给多个 rank 并行解码，再在 rank 0 上拼接（blend）消除接缝。

**核心抽象**（见 vae_parallel.md）是 `DistributedVaeExecutor`，它接受三个函数参数：
- `split`：把 latent 切成 `TileTask` 列表 + `GridSpec`（网格规格）；
- `exec`：解码单个 tile；
- `merge`：把解码后的 tile 拼回完整输出。

**为什么必须带 overlap（重叠）**：VAE 的卷积有感受野，每个输出像素依赖邻域像素，所以 tile 必须重叠，merge 时做 blending 避免接缝。

**两种变体**：
- **Decode Parallel**（默认 `vae_parallel_mode="tile"`）：tile 并行解码，文生图/文生视频用。
- **Spatially-Sharded Decode**（`"spatial_shard_height"`/`"spatial_shard_width"`）：Wan VAE 专有，把整张特征图沿 H/W 切分，用 halo exchange（边界行/列 P2P）替代 gather tile，仅 decode 可用。

**Encode Parallel**（I2V 模型用）：把图像经 VAE 编码成 latent 时也并行，但 `broadcast_result=True`（所有 rank 都需要 latent 做后续扩散）。

**配置入口**：在 `DiffusionParallelConfig` 里由 `vae_patch_parallel_size` 与 `vae_parallel_mode` 控制（见 [data.py:192-218](../vllm_omni/diffusion/data.py)）。当 `vae_patch_parallel_size` 大于 DiT 组大小时，自动回退为 DiT 组大小。

#### 4.5.3 代码实践

> 实践目标：阅读型实践——对比 HSDP 与 VAE 并行的开关位置与生效条件。
>
> 操作步骤：
> 1. 在 [data.py](../vllm_omni/diffusion/data.py) 的 `DiffusionParallelConfig` 里找到 `use_hsdp`、`hsdp_shard_size`、`hsdp_replicate_size`、`vae_patch_parallel_size`、`vae_parallel_mode` 五个字段，记下默认值。
> 2. 打开 `vllm_omni/diffusion/distributed/hsdp.py`，找到 `apply_hsdp_to_model`，确认它读取模型的 `_hsdp_shard_conditions` 来决定切哪些模块。
> 3. 打开 `vllm_omni/diffusion/distributed/autoencoders/` 下的任一分布式 VAE（如 `autoencoder_kl_qwenimage.py`），找到 `tile_split`/`tile_exec`/`tile_merge` 三个方法。
>
> 需要观察的现象：HSDP 在模型加载阶段（`load_model`）就完成权重分片；VAE 并行则在每次 `tiled_decode` 调用时按需切 tile。
>
> 预期结果：能解释为什么 HSDP 不能和 TP 共存（都切权重），而 VAE 并行可以和 TP/SP 自由组合（切的是不同对象——VAE 的空间维度）。

#### 4.5.4 小练习与答案

**练习 1**：HSDP 和 TP 都切权重，为什么不兼容？

**参考答案**：TP 把权重按行/列切到 TP 组的各卡上，前向时通过 all-reduce 拼回；HSDP（FSDP2）把整个权重参数分片到 FS 组的各卡上，前向时 gather 整个权重。两种切分策略对「权重在哪些卡上、怎么拼」的假设完全不同，同时启用会双重切分导致形状错乱。代码里通过 standalone HSDP 判定（要求 `dit_parallel_size==1`）从配置层面隔离了两者。

**练习 2**：VAE Patch 并行里，为什么 encode 通常 `broadcast_result=True` 而 decode 常为 `False`？

**参考答案**：encode 产出 latent，是后续所有扩散步的输入，所有 rank 都要拿到完整的 latent，所以广播；decode 产出最终图像/视频，通常只有 rank 0 负责收集并保存输出，其余 rank 不需要完整结果，所以不必广播（省通信）。

---

## 5. 综合实践

把本讲的「rank 手算」与「各并行设计意图」串起来，完成下面这个贯穿任务。

**任务背景**：假设你有 8 张 GPU，要部署一个扩散模型，配置为 `tp=2, sp=2, cfg=2`（PP=1, DP=1）。模型是一个标准文生图 DiT + CFG。

**要求**：

1. **手算 rank 映射表**。按 order `tp-sp-pp-cfg-dp`，填出下表（每个 rank 对应的 `(tp_rank, sp_rank, cfg_rank)` 三元组）：

   | rank | tp_rank | sp_rank | cfg_rank |
   |:---:|:---:|:---:|:---:|
   | 0 | 0 | 0 | 0 |
   | 1 | ? | ? | ? |
   | 2 | ? | ? | ? |
   | 3 | ? | ? | ? |
   | 4 | ? | ? | ? |
   | 5 | ? | ? | ? |
   | 6 | ? | ? | ? |
   | 7 | ? | ? | ? |

2. **列出各并行组的 rank 列表**：TP 组、SP 组、CFG 组各有几个、分别包含哪些 rank？

3. **画出 rank 到 `(tp, sp, cfg)` 的「立方体」示意**：把 8 个 rank 摆成一个 2×2×2 的立方体，三个轴分别是 tp/sp/cfg，标注每个顶点的 rank 号。

4. **场景分析**：若该模型 CFG 关闭（`do_true_cfg=False`）但仍保留 `cfg_parallel_size=2`，这 8 张卡里 CFG 组的两两配对会怎样浪费？此时更合理的配置是什么？

**参考答案要点**：

1. 由 `rank = tp_rank + sp_rank*2 + cfg_rank*4`（pp=dp=1）：rank 1→(1,0,0)，rank 2→(0,1,0)，rank 3→(1,1,0)，rank 4→(0,0,1)，rank 5→(1,0,1)，rank 6→(0,1,1)，rank 7→(1,1,1)。

2. TP 组（固定 sp,cfg，tp 变）：`[0,1],[2,3],[4,5],[6,7]` 共 4 组。SP 组（固定 tp,cfg，sp 变）：`[0,2],[1,3],[4,6],[5,7]` 共 4 组。CFG 组（固定 tp,sp，cfg 变）：`[0,4],[1,5],[2,6],[3,7]` 共 4 组。

3. 立方体：tp 轴相邻差 1，sp 轴相邻差 2，cfg 轴相邻差 4。底面（cfg=0）四角是 0,1,2,3；顶面（cfg=1）四角是 4,5,6,7。

4. CFG 关闭时，同一 CFG 组的两张卡（如 rank 0 和 rank 4）会跑完全相同的前向（权重相同、输入相同），等于各算一遍冗余结果。更合理的配置是把 `cfg_parallel_size` 降到 1，把省下的卡挪给 DP（提高并发请求数）或更大的 SP（支持更长序列）。

> 完成后，建议用 4.1.4 的示例代码 `RankGenerator(tp=2,sp=2,pp=1,cfg=2,dp=1)` 打印 `get_ranks("tp"/"sp"/"cfg")` 核对你的手算结果。

## 6. 本讲小结

- vLLM-Omni 扩散子系统支持 TP / SP / PP / CFG / DP 五种正交并行，加上侧挂的 HSDP（切权重）与 VAE Patch（切解码空间）两种优化。
- `RankGenerator` 用「固定顺序 `tp-sp-pp-cfg-dp` + mask」在同一套连乘分解方程里生成所有正交并行组，保证各组两两正交；全局 rank 可由各维局部坐标加权求和还原。
- `initialize_model_parallel` 把 `RankGenerator` 算出的 rank 列表注册成 `_DP/_CFG/_SP/_PP/_FS` 全局单例，TP/PP 同步给上游 `vllm_parallel_state` 以复用 vLLM 并行层；SP 组内部再拆 ulysses/ring/allgather 子组。
- 模型代码通过 `get_sp_group()` / `get_cfg_group()` / `get_fs_group()` / `get_dit_group()` 等访问器拿到所属通信组，在「需要跨 rank 同步的瞬间」触发 all-reduce/all-gather/P2P。
- CFG 并行把每步的正/负双前从串行变并行（all_gather 后本地合并），HSDP 用 FSDP2 切权重让大模型装进小显存（与 TP 互斥），VAE Patch 并行把解码切成带 overlap 的 tile 分布执行。
- 本讲是 u7-l2（Ring/Ulysses 序列并行）的上层框架：`sequence_parallel_size = ring_degree × ulysses_degree` 的校验就发生在 `initialize_model_parallel` 里。

## 7. 下一步学习建议

- **结合 u7-l1 / u7-l2 回看**：现在你已掌握全局并行组框架，可重读 u7-l1（注意力后端）与 u7-l2（Ring/Ulysses），理解「SP 组如何从 `get_sp_group()` 拿到 ulysses/ring 子组并在 attention 层做 all-to-all / P2P」。
- **下一讲 u7-l5（Diffusion 批处理）**：将讲请求级批处理与连续批处理，与本讲的 DP 维度（不同请求各跑各的副本）紧密相关——DP 组里的多个副本正是批处理的执行单元。
- **继续阅读源码**：建议精读 [group_coordinator.py](../vllm_omni/diffusion/distributed/group_coordinator.py) 里 `SequenceParallelGroupCoordinator` 如何持有 ulysses/ring/allgather 三个子组，以及 [cfg_parallel.py](../vllm_omni/diffusion/distributed/cfg_parallel.py) 里 `CFGParallelMixin` 如何用 `_CFG` 组做 all_gather，把本讲的「组」落实到「通信」。
- **如果想做扩展开发**：参考 [tensor_parallel.md](../docs/design/feature/tensor_parallel.md) 与 [cfg_parallel.md](../docs/design/feature/cfg_parallel.md) 的 Step-by-Step 指南，为新模型接入 TP/CFG 并行，并在 `tests/diffusion/distributed/` 下找对应的多卡测试（如 `test_tensor_parallel.py`）作为验证范本。
