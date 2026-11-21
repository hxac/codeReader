# 数据并行：DDP、FSDP 与 HSDP

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 DDP、FSDP、HSDP 三种数据并行方式在「参数放在哪里、梯度怎么同步」上的本质差异。
2. 看懂 `DataParallelConfig` / `TransformerDataParallelConfig` 的每个字段，并知道改一个 `name` 枚举值会如何改变整条并行化链路。
3. 理解 `build_world_mesh` 如何把世界进程组组织成 `DeviceMesh`，HSDP 为什么会产生 `dp_replicate × dp_shard` 两个维度。
4. 跟踪 `parallelize_model` → `Transformer.apply_fsdp` / `apply_ddp` 这条把并行策略「贴」到模型上的调用链。
5. 解释梯度累积时梯度同步的时机控制：`no_sync()`、`set_is_last_backward()`、`set_requires_all_reduce()`。
6. 在 CPU 上用 gloo 后端实际跑一个双进程网格实验，验证你对 mesh 结构的预测。

## 2. 前置知识

### 2.1 数据并行的直觉

最朴素的加速思路：把同一个模型复制 N 份，每份喂不同的数据，各自算梯度，再把梯度「平均」之后各自更新。这就是 **DDP（Distributed Data Parallel）**。它的问题是：每个进程都持有一份**完整的参数和优化器状态**。模型一大，单卡就装不下了。

**FSDP（Fully Sharded Data Parallel）** 的思路是：参数、梯度、优化器状态都按进程**切片（shard）**保存，每个进程只存 1/N；前向时用 all-gather 临时拼出完整参数，用完立刻丢回分片状态；反向时再 all-gather 一次，算完梯度后用 reduce-scatter 把梯度归约并直接切成每进程一份。显存省了，代价是通信量变大、流水线更复杂。

**HSDP（Hybrid Sharded Data Parallel）** 是两者的折中：把 N 个进程划分成 R 个「副本组」，每组内 S 个进程做分片（R × S = N）。参数只在组内切片，组间保持完整副本。分片带来的显存收益只除以 S 而不是 N，但通信被拆成两个更小的子组集合通信。

### 2.2 三种集合通信原语

| 原语 | 作用 | 在数据并行中的角色 |
|---|---|---|
| all-reduce | 所有进程得到同一份求和结果 | DDP 归约梯度；HSDP 副本维归约 |
| reduce-scatter | 求和后按进程切分，每进程只拿自己那片 | FSDP 把归约和分片一步完成 |
| all-gather | 每个进程拼出完整张量 | FSDP 前向/反向临时物化参数 |

### 2.3 DeviceMesh 与进程组

PyTorch 的 `DeviceMesh` 是一个带名字的多维进程网格，例如形状 `(dp=2, tp=4)` 的 mesh 表示 8 个进程被组织成 2×4 的矩阵。mesh 的每个维度背后都对应一个**进程组（process group）**，集合通信就在这个子进程组内进行。本讲会看到 OLMo-core 如何用一个 mesh 维度布局表达 FSDP/HSDP。

### 2.4 与前一讲的衔接

u6-l2 已经讲过：`TransformerTrainModuleConfig` 上挂着 DP/TP/CP/EP/PP 六套并行配置，且并行化必须发生在**模型 build 之后、优化器 build 之前**——因为优化器要看到「已经被分片/复制」的参数，才能产生对应形态的优化器状态。本讲就打开 DP 这一路的完整实现。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/olmo_core/distributed/parallel/data_parallel.py` | `DataParallelType` 枚举、`DataParallelConfig` 基类、HSDP 的副本/分片度分解 |
| `src/olmo_core/distributed/parallel/__init__.py` | `build_world_mesh`（把世界进程组组织成多维 mesh）与 `get_dp_model_mesh` / `get_dp_mesh` 等子网格取用函数 |
| `src/olmo_core/train/train_module/transformer/config.py` | `TransformerDataParallelConfig`（DP 配置的 transformer 特化）与 `TransformerTrainModuleConfig.dp_config` 字段 |
| `src/olmo_core/train/train_module/transformer/common.py` | `parallelize_model()`：按固定顺序应用各种并行策略，DP 分支在这里分发到 FSDP 或 DDP |
| `src/olmo_core/nn/transformer/model.py` | `Transformer.apply_fsdp()` / `apply_ddp()`：把分片/复制真正贴到模型上 |
| `src/olmo_core/nn/transformer/block.py` | `TransformerBlock.apply_fsdp()`：FSDP 的逐层 wrap 策略 |
| `src/olmo_core/nn/transformer/config.py` | `TransformerDataParallelWrappingStrategy` 枚举（full / blocks / fine_grained） |
| `src/olmo_core/train/train_module/transformer/train_module.py` | 训练模块里 DP 的两个关键点：mesh 构建、梯度同步时机（`_train_microbatch_context`） |
| `src/olmo_core/train/train_module/transformer/ddp_train_module.py` | `OLMoDDPTrainModule`：MoE-v2 融合优化器路径的专用 train module，强制要求 `name="ddp"` |
| `src/test/distributed/parallel/parallel_test.py` | CPU（gloo）可跑的 mesh 结构测试，本讲实践的依据 |
| `src/olmo_core/testing/distributed.py` | `run_distributed_test`：多进程模拟分布式环境的测试工具 |

## 4. 核心概念与源码讲解

### 4.1 DataParallelConfig：一个枚举决定参数怎么放

#### 4.1.1 概念说明

`DataParallelConfig` 是所有数据并行策略的统一入口。它只回答两个问题：

1. **用哪种策略**（`name`: `fsdp` / `hsdp` / `ddp`）；
2. **HSDP 怎么分组**（`num_replicas` 与 `shard_degree`，二者共同决定 R × S 的划分）。

外加两个精度旋钮：`param_dtype`（参数物化时的 dtype，例如 bf16）与 `reduce_dtype`（梯度归约时的 dtype，默认 fp32 保证数值稳定）。

#### 4.1.2 核心流程

```text
DataParallelConfig(name=..., num_replicas=R?, shard_degree=S?)
        │
        ├── name == "fsdp" / "ddp"：整条 DP 维是一个一维网格，无需分解
        └── name == "hsdp"：调用 get_replicate_and_shard_degree(dp_world_size)
                ├── 两者都为 None → 默认：每个节点一个副本
                │       num_replicas = 节点数, shard_degree = 每节点 GPU 数
                ├── 只给 num_replicas → shard_degree = dp_world_size // num_replicas
                ├── 只给 shard_degree → num_replicas = dp_world_size // shard_degree
                └── 都给 → 校验 dp_world_size 必须能被两者整除
```

注意默认规则的设计意图：**HSDP 默认把「分片」限制在单节点内部**（节点内 NVLink 带宽高，适合频繁的 all-gather/reduce-scatter），「副本」按节点复制（节点间走较慢的网络，只做一次 all-reduce）。

#### 4.1.3 源码精读

先看枚举与网格维度名（HSDP 会用到的两个维度名就定义在这里）：

[data_parallel.py:L12-L30](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L12-L30)
上面定义了 `DPMeshDimName`（`dp_replicate` = 模型被复制的维度，`dp_shard` = 模型被切片的维度）和 `DataParallelType`（`fsdp` / `hsdp` / `ddp` 三个取值）。

[data_parallel.py:L33-L39](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L33-L39)
`DataParallelConfig` 的五个字段：`name` 必填；`param_dtype` 默认 `None`（沿用模型当前 dtype）；`reduce_dtype` 默认 `float32`；`num_replicas` / `shard_degree` 默认都为 `None`（交给自动分解）。

[data_parallel.py:L41-L63](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L41-L63)
`get_replicate_and_shard_degree()` 按上面流程图的四种组合计算 (num_replicas, shard_degree)。都为 None 时依赖 `get_num_nodes()`。

[distributed/utils.py:L338-L353](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L338-L353)
`get_num_nodes()`：优先读 `NUM_NODES` 环境变量，否则用 `world_size // local_world_size` 推断；**未初始化分布式时恒返回 1**——这就是后面实践中「单进程下默认分解为 (1, N)」的原因。

[data_parallel.py:L66-L81](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L66-L81)
两个校验函数：`dp_world_size` 必须能被 `num_replicas` / `shard_degree` 整除，否则抛 `OLMoConfigurationError`。配置错误在构建 mesh 之前就被拦下，这是 OLMo-core「尽早失败」风格的一例。

Transformer 侧的特化配置继承自它：

[train_module/transformer/config.py:L227-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L227-L268)
`TransformerDataParallelConfig` 在基类之上加了：`wrapping_strategy`（FSDP 逐层 wrap 粒度，见 4.3）、`prefetch_factor`（FSDP 预取强度）、`only_allreduce_last_microbatch`（只在梯度累积的最后一个 micro-batch 同步梯度，默认 True）、`reduce_grads_in_fp32` / `accumulate_grads_in_fp32`（给 `MultiGroupDistributedDataParallel` 用的精度开关）、`bucket_cap_mb`（梯度桶大小）、`use_reduce_scatter`（把普通参数梯度直接 reduce-scatter 进分布式优化器分片，MoE-v2 路径专用）。

[train_module/transformer/config.py:L386-L415](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L386-L415)
`TransformerTrainModuleConfig` 中的 `dp_config: Optional[TransformerDataParallelConfig] = None`（第 411 行）——这就是你在配置层面切换 DDP/FSDP/HSDP 的唯一入口。

真实用法示例（第 1 单元跑过的训练例子里就是这么写的）：

[examples/llm/train.py:L186-L190](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L186-L190)
示例脚本用 `dp_config=TransformerDataParallelConfig(name=DataParallelType.fsdp, param_dtype=DType.bfloat16, reduce_dtype=DType.float32)`：FSDP 分片、参数以 bf16 物化、梯度归约保持 fp32。

#### 4.1.4 代码实践：预测并验证 HSDP 的分组

**实践目标**：不启动任何分布式进程，验证你对 `get_replicate_and_shard_degree` 四种分支的理解。

**操作步骤**：在装好 olmo-core 的环境里进入 Python 交互式解释器，执行（示例代码）：

```python
from olmo_core.distributed.parallel.data_parallel import (
    DataParallelConfig, DataParallelType,
)

# 分支 1：都不给（未初始化分布式时 get_num_nodes()==1）
print(DataParallelConfig(name=DataParallelType.hsdp).get_replicate_and_shard_degree(16))

# 分支 2：只给 num_replicas
print(DataParallelConfig(
    name=DataParallelType.hsdp, num_replicas=2
).get_replicate_and_shard_degree(16))

# 分支 3：只给 shard_degree
print(DataParallelConfig(
    name=DataParallelType.hsdp, shard_degree=8
).get_replicate_and_shard_degree(16))

# 分支 4：给了不整除的组合
try:
    DataParallelConfig(
        name=DataParallelType.hsdp, num_replicas=5
    ).get_replicate_and_shard_degree(16)
except Exception as e:
    print(type(e).__name__, e)
```

**需要观察的现象**：四条输出分别是 `(1, 16)`、`(2, 8)`、`(2, 8)`、以及一个 `OLMoConfigurationError`。

**预期结果**：与源码逐分支对照——分支 1 中 `get_num_nodes()` 在未初始化分布式时返回 1，所以退化成「1 个副本、16 路分片」（等价于纯 FSDP）；分支 4 中 16 % 5 != 0，被 `_check_num_replicas` 拦截。以上结论直接来自源码逻辑；交互式解释器的具体打印格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：8 卡单机想用 HSDP 且每组 4 卡分片，`num_replicas` / `shard_degree` 该怎么填？不填会怎样？

答案：填 `num_replicas=2, shard_degree=4`。不填的话走默认分支：单机 `get_num_nodes()==1`，得到 (1, 8)，实际是纯 FSDP 而非 HSDP——**单机上 HSDP 的默认值不产生副本维**，这是一个容易踩的坑。

**练习 2**：`reduce_dtype` 默认为什么是 fp32 而不是跟 `param_dtype` 一样用 bf16？

答案：梯度归约是对 N 份梯度求和，求和的数值误差会随卡数放大；用 fp32 归约再落到低精度参数，可以避免归约本身引入的精度损失。这是混合精度训练的常见惯例（参数低精度、归约高精度）。

**练习 3**：`DataParallelConfig` 里没有 `dp_world_size` 字段，分解函数为什么需要把它作为参数传入？

答案：DP 世界大小在 mesh 构建时才确定——它等于总 world size 除去 PP/TP/CP 等其他并行维度占用的进程数（见 4.2）。所以 `get_replicate_and_shard_degree` 只能作为方法在拿到 `dp_world_size` 之后调用，而不能在配置对象里静态写死。

### 4.2 从 world mesh 到 DP 子网格

#### 4.2.1 概念说明

并行策略不是孤立生效的：同一组进程既要按 DP 切数据、又可能按 TP 切权重。OLMo-core 用一个全局 `DeviceMesh` 表达这一切——**mesh 的每个命名维度对应一种并行维度**。DP 策略的差异直接体现在 mesh 维度布局上：

- `fsdp` / `ddp`：一个一维 `dp` 维度；
- `hsdp`：`dp_replicate` 和 `dp_shard` 两个维度（FSDP2 拿到二维 mesh 就自动获得 HSDP 语义）。

此外，模型用的 DP mesh 和数据加载器用的 DP mesh **不总是同一个**（`get_dp_model_mesh` vs `get_dp_mesh`），这是本模块最容易混淆、也最能体现设计功力的地方。

#### 4.2.2 核心流程

`build_world_mesh` 的维度搭建顺序：

```text
names/dims = []
1. pp  存在 → 追加 ("pp", pp.degree)，dp_world_size //= pp.degree
2. cp  存在 → 校验并 dp_world_size //= cp.degree
3. tp  存在 → 校验并 dp_world_size //= tp.degree
4. ep  存在 → 只允许配合 HSDP，且 ep.degree 必须等于 shard_degree
5. dp 维度：
   ├── hsdp → ("dp_replicate", R), ("dp_shard", S)   ← 两个维度
   └── 其他 → ("dp", dp_world_size)                    ← 一个维度；EP 时再追加 ("ep", ep.degree)
6. cp 存在 → 追加 ("cp", cp.degree)
7. tp 存在 → 追加 ("tp", tp.degree)
最后 init_device_mesh(...) 并确保 DP 进程组被创建
```

之后不同消费者取自己关心的子网格：

- `get_dp_model_mesh`（给 FSDP/DDP 包装用）：取出所有以 `dp` 开头的维度。HSDP 下得到二维 `(dp_replicate, dp_shard)`；CP 维度会被**折进** DP 维（因为每个 CP 进程都持有完整参数，梯度必须跨 DP×CP 归约）；EP 维度也会被折平。
- `get_dp_mesh`（给数据加载器分配 rank 用）：同样取 `dp` 开头的维度，但 EP 维度折平进来（EP 组内各 rank 应拿到**不同**数据）、CP 维度**不**折进来（CP 组内各 rank 应拿到**相同**数据）。
- `get_dp_process_group`：在 `get_dp_mesh` 结果上取（多维则先 flatten）进程组。

#### 4.2.3 源码精读

[parallel/__init__.py:L116-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L116-L169)
`build_world_mesh` 的签名与两条前置规则：五套配置全为空时返回纯 1D `dp` mesh；只要用了任何一种其他并行（TP/CP/PP/EP），就必须同时给 DP 配置。同时注意第 157-158 行：mesh 是进程级单例，重复构建会直接 `RuntimeError`。

[parallel/__init__.py:L190-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L190-L206)
EP 的限制：非 HSDP 时先除以 `ep.degree`，然后无条件抛错——**专家并行目前只支持与 HSDP 搭配**（代码里留了 TODO，将来 DTensor 支持跨 mesh 操作后放开）。

[parallel/__init__.py:L218-L239](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L218-L239)
本讲最关键的一段：`name == hsdp` 时调用 `get_replicate_and_shard_degree` 得到 (R, S)，向 mesh 追加 `dp_replicate`、`dp_shard` 两个维度；EP 存在时要求 `ep.degree == shard_degree`（专家直接复用分片维度）；否则只追加一个 `dp` 维度。

[parallel/__init__.py:L241-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L241-L259)
维度收集完成后 `init_device_mesh` 一次性创建 mesh（每个维度自动建好进程组），再显式调用 `get_dp_process_group` 确保 DP 组就绪，最后把 mesh 存进模块级单例 `_WORLD_MESH`。

[parallel/__init__.py:L311-L348](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L311-L348)
`_get_model_mesh`：模型视角的维度折叠——EP 维折进 DP；CP 维折进紧邻的 DP 维，注释写得很清楚：「对参数同步而言，一个 CP 组就像一组额外的 DP 副本，梯度要跨 DP ∪ CP 归约」。

[parallel/__init__.py:L351-L365](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L351-L365)
`get_dp_model_mesh`：取所有 `dp` 开头的维度组成子网格，交给 FSDP/DDP。HSDP 下它天然是二维的。

[parallel/__init__.py:L368-L406](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L368-L406)
`get_dp_mesh`：数据加载视角。EP 维同样折平（EP 组内 rank 拿不同数据），但若发现已折平的 `dp_cp` 维度直接报错——数据侧 mesh 必须从**原始** world mesh 上取，保证 CP 组内 rank 拿到同一份数据。

训练模块在哪里触发这一切：

[train_module/transformer/train_module.py:L171-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L171-L187)
`TransformerTrainModule.__init__` 中：只要处于分布式环境就用 `dp_config` 等构建 world mesh；反之若给了任何并行配置却不是分布式运行，直接抛配置错误。

[train_module/transformer/train_module.py:L244-L246](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L244-L246)
`dp_process_group` 属性：从 world mesh 取 DP 进程组，供数据加载器等组件使用。

#### 4.2.4 代码实践：跑通 CPU 上的 mesh 测试

**实践目标**：用仓库自带的测试在 CPU（gloo 后端）上验证 fsdp / hsdp 两种配置生成的 mesh 结构。

**操作步骤**：

```bash
# 只跑 dp 维度的两组用例（2 进程 gloo，无需 GPU）
pytest -v "src/test/distributed/parallel/parallel_test.py::test_build_world_mesh_cpu" -k "dp_only"
```

对照测试断言阅读 [parallel_test.py:L80-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L80-L123)：第 94-98 行断言 HSDP 下 `dims_map["dp_replicate"] * dims_map["dp_shard"] == expected_dp_world_size`；第 112-123 行断言 `get_dp_mesh` / `get_dp_model_mesh` 取出的子网格形状正确。

测试的多进程机制来自 [testing/distributed.py:L111-L147](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L111-L147)：`run_distributed_test` 用 `torch.multiprocessing` 起N 个子进程，每个子进程设置好 rank 环境变量后调用 `init_distributed(backend="gloo")`，再执行你给的函数——这就是「CPU 多进程模拟分布式」的标准工具，也是第 5 节综合实践的基础。

**需要观察的现象**：测试在两个子进程中各自完成 mesh 构建与断言，全部通过；`-k "dp_only"` 会分别以 `fsdp` 和 `hsdp` 两种 `dp_type` 参数化各跑一次。

**预期结果**：`dp_only` 组合只命中 `PARALLEL_MESH_TESTCONFIGS` 中的一条参数（2 进程纯 DP），配合 `dp_type` 的 fsdp/hsdp 两档共 2 个用例，全部通过。具体用例数量以本地输出为准，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_dp_model_mesh` 要把 CP 维折进 DP，而 `get_dp_mesh` 不折？

答案：模型参数在每个 CP rank 上都是完整的副本，反向时梯度必须跨 DP×CP 全体归约，所以模型视角 CP 等价于一组 DP 副本；而数据视角下，同一 CP 组的 rank 处理的是同一条序列的不同片段，必须拿到**同一份**输入数据，因此数据加载的 DP mesh 不能包含 CP 维。

**练习 2**：`build_world_mesh` 为什么把 PP 维放在 mesh 最前面、TP 放最后？

答案：mesh 维度的顺序决定了进程编号的切分粒度：前面的维度变化慢。PP 放最前使同一 PP stage 的进程编号连续（便于 stage 间点对点通信），TP 放最后使同一 TP 组的进程编号紧邻（通常在单节点内、共享高带宽互联）。这类布局直接影响集合通信的实际带宽。

**练习 3**：`_WORLD_MESH` 单例（重复构建会报错）对写测试有什么影响？

答案：一个进程只能构建一次 world mesh。想在同一段代码里对比多种并行配置的 mesh，必须每种配置单独起一组进程（例如多次调用 `run_distributed_test`），而不能在一个进程里连续 `build_world_mesh`。

### 4.3 FSDP 与 HSDP：fully_shard 分片与两段式梯度归约

#### 4.3.1 概念说明

OLMo-core 的 FSDP 走的是 PyTorch 官方 **FSDP2** 路线：不用 `FSDP(module)` 包装器类，而是调用 `torch.distributed.fsdp.fully_shard(module, mesh=...)` 原地改造模块——模块参数变成 `DTensor` 分片，前向/反向自动插入 all-gather / reduce-scatter。

HSDP 不需要单独的实现：把一个**二维 mesh** `(dp_replicate, dp_shard)` 传给 `fully_shard`，FSDP2 自动沿 shard 维 reduce-scatter、沿 replicate 维 all-reduce，这正是 HSDP 的语义。所以「HSDP 是一种 FSDP」在这套实现里体现得非常直白。

三种策略的每卡显存与每步通信量对比（设参数量 \(\Psi\) 个元素、DP 世界大小 \(N\)、HSDP 划分 \(N = R \times S\)、bf16 参数 + fp32 AdamW 状态；数字为量级估算）：

\[ \text{每卡参数+优化器状态} \approx \begin{cases} 14\,\Psi & \text{DDP（与 } N\text{ 无关）} \\ 14\,\Psi / N & \text{FSDP} \\ 14\,\Psi / S & \text{HSDP} \end{cases} \]

（\(14\Psi\) = bf16 参数 \(2\Psi\) + fp32 主参数 \(4\Psi\) + 一/二阶矩各 \(4\Psi\) 字节。）

每步通信量（bf16 梯度/参数、ring 集合通信，每卡收发约为数据量的 2 倍）：

\[ \text{DDP}: \;\; \approx 4\Psi \;\; \text{（反向一次 all-reduce）} \qquad \text{FSDP}: \;\; \approx 3 \times \text{all-gather/reduce-scatter 覆盖 } \Psi \text{ 的通信} \]

FSDP 通信更多但可以被计算重叠；HSDP 则把大通信限制在 shard 子组内、副本间只对已分片的小梯度做一次 all-reduce，**用「分片收益变小」换「跨节点通信变少」**。

#### 4.3.2 核心流程

`parallelize_model` 中 DP 是**最后一个**被应用的策略（顺序：PP → FP8 → CP → TP → EP → 激活检查点 → compile → DP → init_weights），因为 `fully_shard` 会原地改写模块类与参数形态，必须等其它策略都就位：

```text
parallelize_model(model, dp_config=...)
    └── dp_config.name ∈ {fsdp, hsdp}
        ├── (MoE 模型先 prepare_experts_for_fsdp)
        └── 对每个 model part 调 model.apply_fsdp(dp_mesh, ...)
                ├── 构造 MixedPrecisionPolicy(param_dtype, reduce_dtype)
                ├── 每个 block 调 block.apply_fsdp(...)   ← wrap 策略在此生效
                ├── 未 tie 权重时单独分片 embeddings / lm_head
                ├── fully_shard(self, ...)                 ← 根模块，兜住其余参数
                └── prefetch_factor > 0 时配置前向预取链
```

梯度同步时机（训练循环侧，见 4.3.3 最后一条引用）：梯度累积的**非最后** micro-batch 上，FSDP 调 `set_is_last_backward(False)`；HSDP 额外调 `set_requires_all_reduce(False)` 把副本维的 all-reduce 推迟到累积窗口末尾——两个开关都只在最后一个 micro-batch 放行完整归约。

#### 4.3.3 源码精读

[train_module/transformer/common.py:L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145)
`parallelize_model` 的 DP 分发处：`dp_config.name in (fsdp, hsdp)` 走 `apply_fsdp`（两种策略共用同一条 FSDP 路径，差异完全由 mesh 形状承载）；`ddp` 走 `apply_ddp`；其他值抛 `NotImplementedError`。注意第 118 行取的是 `get_dp_model_mesh`——模型包装永远用模型视角的子网格。

[model.py:L1174-L1204](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1174-L1204)
`Transformer.apply_fsdp` 开头：构造 `MixedPrecisionPolicy(param_dtype, reduce_dtype)`（参数低精度物化 + 梯度高精度归约）；`reshard_after_forward=False if pp_enabled else True`——开流水线时前向用完不把参数丢回分片态，避免每个 micro-batch 都付一次 all-gather。

[model.py:L1206-L1233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1206-L1233)
分片三步：① 逐 block 调 `block.apply_fsdp`（wrap 策略下发到块级）；② 权重未 tie 时单独分片 `embeddings`（并设 `set_unshard_in_backward(False)`，embedding 参数反向不需要重新物化）与 `lm_head`；③ `fully_shard(self, ...)` 分片根模块，兜住 norm 等散落参数。tie 权重的 embedding 与 lm_head 共享同一个参数，必须留在同一个 FSDP 组（根）里，注释明确解释了这一点。

[block.py:L221-L236](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L221-L236)
`TransformerBlock.apply_fsdp`：`fine_grained` 策略额外把 `attention`、`feed_forward` 各自 `fully_shard`（分片单元更小、流水线更细，预取链也按 att→mlp 配置）；其他策略只分片整个 block。

[nn/transformer/config.py:L42-L60](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L42-L60)
`TransformerDataParallelWrappingStrategy` 三档：`full`（每个 block + lm_head 等都包）、`blocks`（像 full 但 lm_head 不单独包）、`fine_grained`（block 内部再包 attention / feed_forward）。分片单元越小，单次 all-gather 越小、越容易与计算重叠，但元数据与调度开销越多。

[model.py:L1241-L1248](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1241-L1248)
`prefetch_factor > 0` 时为每个 block 配置「下一个 block 的参数预取」，最后一个 block 预取 lm_head——用提前的 all-gather 掩盖通信延迟。

[train_module/transformer/train_module.py:L636-L655](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L636-L655)
`_train_microbatch_context`——梯度同步的时机控制：模型是 `FSDPModule` 时每个 micro-batch 调 `set_is_last_backward(is_last_mb)`；HSDP 再加 `set_requires_all_reduce(is_last_mb)` 延迟副本维归约；DDP 则在非最后 micro-batch 进入 `no_sync()` 上下文。三种策略在这里汇合成同一条规则：**梯度累积窗口内只在最后一步做完整同步**。

[train_module/transformer/train_module.py:L683-L696](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L683-L696)
`_clip_grad_norm`：梯度裁剪必须感知分片——FSDP 路径走 FSDP 自己的 `clip_grad_norm_`（内部跨分片归约全局范数），不能对本地分片直接算范数，否则裁剪阈值就错了。

#### 4.3.4 代码实践：数一数 wrap 策略产生多少个分片单元

**实践目标**：把 `wrapping_strategy` 三档翻译成「模型里有多少个 FSDP 分片单元」，验证你对 wrap 链的理解。

**操作步骤**（源码阅读 + 推算，不依赖 GPU）：

1. 读 [common.py:L115-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L137) 与 [model.py:L1206-L1233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1206-L1233)，对一个 `n_layers=4`、未 tie 权重、含 `embedding_norm` 的 `Transformer`，分别列出 `full` / `blocks` / `fine_grained` 三档下 `fully_shard` 被调用的位置清单。
2. 有 GPU 的读者可运行（示例代码，待本地验证）：

```python
from torch.distributed.fsdp import FSDPModule
# 假设 model 已在 2 进程 FSDP 环境下 parallelize_model 完成
n_units = sum(1 for m in model.modules() if isinstance(m, FSDPModule))
```

**需要观察的现象**：三档的分片单元数分别约为：`full` → 4(block) + embeddings + embedding_norm + lm_head + 根 = 7；`blocks` → 4(block) + embeddings + 根 = 6（norm 与 lm_head 不单独包，留在根里）；`fine_grained` → 每个 block 再多出 attention、feed_forward 两个单元，4×3 + embeddings + 根 = 15。

**预期结果**：分片单元数随档位递增；`blocks` 档的 lm_head 参数随根模块一起分片。精确计数依赖模型组件是否存在（如 `embedding_norm` 为 None 时少一个），请以你构建的配置为准。

#### 4.3.5 小练习与答案

**练习 1**：HSDP 没有独立的 `apply_hsdp` 方法，实现差异在哪里体现？

答案：在 mesh。`build_world_mesh` 为 hsdp 生成 `(dp_replicate, dp_shard)` 二维网格，`get_dp_model_mesh` 原样把它交给 `fully_shard`；FSDP2 对多维 mesh 的解释（沿最后一维 reduce-scatter、沿其余维 all-reduce）天然就是 HSDP。

**练习 2**：为什么 tie_word_embeddings 时不能把 embeddings 和 lm_head 各自 `fully_shard`？

答案：tie 时二者共享同一份参数张量，属于同一个参数；若分别放进两个 FSDP 单元，同一张量会被两套 all-gather/分片逻辑管理，状态互相冲突。所以必须让它们留在同一个（根）FSDP 组里。

**练习 3**：`set_requires_all_reduce(is_last_mb)` 只在 HSDP 分支调用，FSDP 分支为什么不需要？

答案：纯 FSDP 只有 reduce-scatter 一种归约（随反向流式进行），由 `set_is_last_backward` 控制收尾即可；HSDP 在 reduce-scatter 之外还有一次跨副本的 all-reduce，这次 all-reduce 对整个累积窗口只需要做一次，因此要显式推迟到最后一个 micro-batch。

### 4.4 DDP：replicate 包装、梯度桶归约与 OLMoDDP 路径

#### 4.4.1 概念说明

`DataParallelType.ddp` 对应经典的复制式数据并行：每个进程持有完整参数，反向时框架把梯度切进「桶（bucket）」，桶满即对 DP 进程组做 all-reduce，与反向计算重叠。OLMo-core 用的是 PyTorch 新的 `torch.distributed._composable.replicate` 组合式 API（而非老的 `DistributedDataParallel` 包装类），同样是原地改造模块。

另外要澄清一个命名冲突：仓库里还有一族 `OLMoDDP*` 类（`OLMoDDPModel` / `OLMoDDPOptimizer` / `OLMoDDPTrainModule`），那是 **MoE-v2 融合分布式优化器**的专用路径，虽然名字里带 DDP，但它是一套独立的实现，本讲只在最后带一眼。

#### 4.4.2 核心流程

```text
parallelize_model(dp_config.name == "ddp")
    └── (MoE 模型先 prepare_experts_for_ddp)
    └── model.apply_ddp(dp_mesh, compile_enabled, param_dtype)
            ├── 需要时先把整个模型 cast 到 param_dtype
            └── replicate(model, device_mesh=dp_mesh, bucket_cap_mb=100)

训练循环（TransformerTrainModule）：
    for micro_batch in micro_batches:
        with (no_sync() 若非最后 micro-batch):
            forward + backward          ← 桶 all-reduce 只在最后一步真正执行
    → optim_step（梯度已是跨 rank 平均后的）
```

关键点：DDP 的梯度归约是「隐式」的——`backward()` 触发桶归约，库代码必须用 `no_sync()` 把中间 micro-batch 的归约关掉，否则同一个梯度会被归约多次（累积窗口内多次 all-reduce 会把梯度放大）。

#### 4.4.3 源码精读

[model.py:L1252-L1283](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1252-L1283)
`Transformer.apply_ddp`：先把模型 cast 到目标 dtype（第 1265-1267 行），再调用 `replicate(self, device_mesh=dp_mesh, bucket_cap_mb=100)`（第 1277 行）原地挂上梯度桶归约；`compile_enabled` 时还会按是否编译反向调整 `torch._dynamo.config.optimize_ddp`（代码注释标明改编自 torchtitan）。

[common.py:L138-L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L138-L143)
`parallelize_model` 的 DDP 分支：MoE 模型先 `prepare_experts_for_ddp`（专家参数的归约路径与普通参数不同），再 `apply_ddp`。DDP 分支**不消费** `wrapping_strategy` / `prefetch_factor` 这些 FSDP 专属字段。

[train_module/transformer/train_module.py:L636-L655](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L636-L655)
（同 4.3.3 引用的片段，DDP 视角看第 650-653 行）模型是 `DDP` 时，非最后 micro-batch 进入 `self.model.no_sync()`——把桶归约推迟，实现「累积一次、归约一次」。

再看 MoE-v2 的 OLMoDDP 路径对 DDP 配置的强约束：

[ddp_train_module.py:L244-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L244-L268)
`OLMoDDPTrainModule.__init__` 的校验：DP 配置必填且**必须** `name == "ddp"`；`use_reduce_scatter=True` 时还有一串额外限制（不支持 CP、必须只在最后 micro-batch 同步、开 PP 时要求自定义 stage 实现）。

[ddp_train_module.py:L270-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L270-L279)
最有趣的一条硬性规则：`only_allreduce_last_microbatch` 必须为 True，否则直接抛错。注释解释了原因——`MultiGroupDistributedDataParallel` 把梯度原地累加进扁平桶视图，一个桶在一个累积窗口内只能被 all-reduce 一次；若每个 micro-batch 都归约，会把「已归约的梯度」再归约一遍，轻则梯度未归约、重则数值损坏。这与 4.4.2 的 `no_sync()` 逻辑是同一个道理的「强制版」。

#### 4.4.4 代码实践：跟踪一条「为什么必须只在最后一步归约」的证据链

**实践目标**：通过纯源码阅读，把「每个 micro-batch 都 all-reduce 会发生什么」的因果链写出来。

**操作步骤**：

1. 读 [train_module.py:L650-L653](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L650-L653)（经典 DDP 的 `no_sync`）。
2. 读 [ddp_train_module.py:L270-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L270-L279)（MoE-v2 路径直接把错误配置拒绝在构造期）。
3. 假设 2 个 micro-batch、DP 世界大小为 2、梯度张量 g 在两个 rank 上分别是 \(g_1, g_2\)，手算以下两种策略在 optim_step 时本地看到的梯度值（设每个 micro-batch 对梯度的贡献分别为 a 和 b，两个 rank 各自独立）：

   - 错误策略（每步都 all-reduce 求和）：第一次归约后本地是 \((a_1+a_2)\)，累加 b 后第二次归约得到 \((a_1+a_2+b_1)+(a_1+a_2+b_2) = 2(a_1+a_2)+(b_1+b_2)\)——a 的归约贡献被计了两次。
   - 正确策略（最后一步才归约）：本地累积 \(a_i+b_i\)，一次归约得 \((a_1+b_1)+(a_2+b_2)\)。

**需要观察的现象**：错误策略的结果中 \(a_1+a_2\) 的系数是 2，梯度被系统性放大且随 micro-batch 数恶化。

**预期结果**：这解释了两处源码为何都把「只在最后 micro-batch 归约」作为不变量——一处用上下文管理器保证，一处用构造期断言强制。

#### 4.4.5 小练习与答案

**练习 1**：`apply_ddp` 里 `replicate(..., bucket_cap_mb=100)` 的桶大小调大/调小各有什么影响？

答案：桶越大，单次 all-gather... 更准确地说：桶越大，归约次数越少、每次通信越大，与反向重叠的机会越多，但需要先把更多梯度攒齐、首个桶的等待时间变长；桶越小则通信更频繁、调度开销更大。100 MiB 是 torchtitan 风格的大桶默认值。`TransformerDataParallelConfig.bucket_cap_mb` 字段允许覆盖（`None` = 后端默认）。

**练习 2**：DDP 路径为什么不消费 `wrapping_strategy`？

答案：wrap 策略描述的是「哪些模块各自成为一个分片单元」，这是分片式（FSDP）才有的概念；DDP 复制完整参数，没有分片单元可言，只需要把整个模型挂上 `replicate` 的桶归约。

**练习 3**：`OLMoDDPTrainModule` 与 `TransformerTrainModule` 都接受 `dp_config`，二者对它的使用有何本质区别？

答案：`TransformerTrainModule` 把 `dp_config.name` 当作三选一的策略开关（fsdp/hsdp 走 `apply_fsdp`，ddp 走 `apply_ddp`）；`OLMoDDPTrainModule`（MoE-v2 融合优化器路径）则把它当作 DDP 专属的调参对象——name 必须是 `ddp`，其余字段（如 `use_reduce_scatter`、`reduce_grads_in_fp32`）控制其自研 `MultiGroupDistributedDataParallel` 的归约行为。

## 5. 综合实践

设计一个「双进程 CPU 实验」：分别以 `fsdp`、`hsdp`、`ddp` 三种配置构建 world mesh，打印 DP 子网格结构，再回答 HSDP 与 FSDP 的取舍问题。全程不需要 GPU。

**任务**：编写脚本 `dp_mesh_compare.py`（示例代码，放在仓库外任意目录运行均可）：

```python
"""对比 fsdp / hsdp / ddp 三种 DataParallelType 的 mesh 结构（CPU + gloo）。"""
import sys

import torch
from olmo_core.distributed.parallel import (
    build_world_mesh, get_device_mesh_info, get_dp_mesh, get_dp_model_mesh,
)
from olmo_core.distributed.parallel.data_parallel import (
    DataParallelConfig, DataParallelType,
)
from olmo_core.testing import run_distributed_test


def main(dp_name: str, num_replicas=None, shard_degree=None):
    dp_cfg = DataParallelConfig(
        name=DataParallelType(dp_name),
        num_replicas=num_replicas,
        shard_degree=shard_degree,
    )
    mesh = build_world_mesh(dp=dp_cfg, device_type="cpu")
    print(f"[rank {mesh.get_rank()}] world mesh: {get_device_mesh_info(mesh)}")
    print(f"[rank {mesh.get_rank()}] dp model mesh: {get_device_mesh_info(get_dp_model_mesh(mesh))}")
    print(f"[rank {mesh.get_rank()}] dp data  mesh: {get_device_mesh_info(get_dp_mesh(mesh))}")


if __name__ == "__main__":
    dp_name = sys.argv[1] if len(sys.argv) > 1 else "fsdp"
    if dp_name == "hsdp":
        # 4 进程、2 副本 × 2 分片：显式指定，避免单机默认 (1, N) 退化成纯 FSDP
        run_distributed_test(
            main, world_size=4, backend="gloo",
            func_args=("hsdp", 2, 2),
        )
    else:
        run_distributed_test(
            main, world_size=2, backend="gloo", func_args=(dp_name,),
        )
```

**操作步骤**：

1. 分别运行 `python dp_mesh_compare.py fsdp`、`python dp_mesh_compare.py ddp`、`python dp_mesh_compare.py hsdp`。
2. 记录三种配置下 `dp model mesh` 与 `dp data mesh` 的维度名和形状。
3. 回答三个问题（答案见源码，可对照 4.2/4.3）：
   - fsdp 与 ddp 的两种 DP 子网格形状分别是什么？（都应为 1D `dp=进程数`——二者 mesh 相同，差异在后续 `fully_shard` vs `replicate`）
   - hsdp 的 model mesh 为什么是二维？`num_replicas=2, shard_degree=2` 时形状是什么？（`dp_replicate=2, dp_shard=2`）
   - 单机不显式指定 `num_replicas` 时 hsdp 会退化成什么？（`(1, 4)`，等价纯 FSDP——因为 `get_num_nodes()` 返回 1）
4. **注意单例陷阱**：`build_world_mesh` 一个进程只能调用一次（[parallel/__init__.py:L157-L158](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L157-L158)），所以三种配置必须分三次进程运行，不能在同一进程里循环构建——上面的脚本正是为此设计成每次运行只处理一种配置。

**进阶（可选，待本地验证）**：若你的 PyTorch 版本支持 CPU 上的 FSDP2/DTensor（gloo），可在此脚本中继续调用 `parallelize_model`（[common.py:L34-L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L34-L49)）对 `TransformerConfig.olmo2_1M()` 构建的小模型分别施加 fsdp 与 ddp，然后遍历 `model.parameters()` 对比 `p.numel()` 与 `type(p)`：FSDP 下参数应变为 `DTensor` 且本地 numel 约为总量的 1/N，DDP 下保持普通张量且 numel 等于总量；再各自 `optim.build(model)` 后检查优化器状态的分片差异。CPU 上的可行性依赖具体 torch 版本，无法在无 GPU 环境保证，故标注待本地验证。

**预期结果**：mesh 部分的输出形状可从源码完全推出（见步骤 3 的参考答案）；模型/优化器分片对比部分在有 GPU 的环境下结论同样成立（此时建议直接改 `src/examples/llm/train.py` 第 188-190 行的 `name` 字段做 2 卡小实验）。

## 6. 本讲小结

- **一个枚举三种策略**：`DataParallelConfig.name`（fsdp/hsdp/ddp）是数据并行的唯一开关；fsdp 与 hsdp 共用同一条 `fully_shard` 路径，差异完全由 mesh 维度承载——hsdp 产生 `dp_replicate × dp_shard` 二维网格，FSDP2 对二维 mesh 的解释天然就是 HSDP。
- **mesh 是并行策略的骨架**：`build_world_mesh` 按 pp → dp（→ replicate+shard）→ cp → tp 的顺序组织进程；模型视角的 `get_dp_model_mesh` 会把 CP/EP 折进 DP 维（参数同步需要），数据视角的 `get_dp_mesh` 折 EP 不折 CP（数据分配需要），二者职责分明。
- **并行化顺序是契约**：`parallelize_model` 中 DP 最后应用、`init_weights` 在其后、优化器 build 更在其后——优化器看到的参数形态决定了优化器状态的形态（分片或完整）。
- **梯度同步时机是隐式契约**：三种策略都遵守「梯度累积窗口内只在最后一个 micro-batch 归约」——FSDP 用 `set_is_last_backward`，HSDP 再加 `set_requires_all_reduce`，DDP 用 `no_sync()`，MoE-v2 的 OLMoDDP 路径则把这条规则升级为构造期硬断言。
- **显存与通信的取舍**：DDP 每卡约 \(14\Psi\) 字节与 N 无关、通信最少；FSDP 除以 N、通信约三倍；HSDP 除以 S、大通信限制在 shard 子组内。默认「每节点一副本」正是为了把 all-gather/reduce-scatter 留在节点内高带宽互联上。
- **细节里藏着正确性**：tie 权重必须留在同一 FSDP 组、梯度裁剪必须走分片感知的 `clip_grad_norm_`、`bucket_cap_mb` 可调桶大小——这些点都直接来自源码注释与校验逻辑。

## 7. 下一步学习建议

下一讲 **u7-l2（张量、流水线、上下文与专家并行）** 将打开 `distributed/parallel/` 的其余维度：`tensor_parallel.py`、`pipeline_parallel.py`、`context_parallel.py`、`expert_parallel.py`，看它们如何与本讲的 DP 维组合成多维 mesh（本讲已经见过两个衔接点：EP 目前只允许搭配 HSDP、CP 维在模型 mesh 中被折进 DP）。之后再进入 **u7-l3（分布式工具与测试基础设施）**，深入 `init_distributed`、进程组管理与 `run_distributed_test` 的多进程机制——本讲综合实践已经用到了它，届时会理解其实现细节。

建议继续阅读的源码（按优先级）：

1. `src/olmo_core/nn/moe/mlp.py` 第 102-112 行附近——MoE 专家在 HSDP 下如何选择在哪个 mesh 维上 `fully_shard`；
2. `src/olmo_core/distributed/parallel/expert_parallel.py`——EP 的配置与 `build_expert_parallel_mesh`；
3. `src/test/nn/parallel/distributed_test.py`——分布式原语层面的更多 CPU 可跑测试。
