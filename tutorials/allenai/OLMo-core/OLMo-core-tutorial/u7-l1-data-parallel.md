# 数据并行：DDP、FSDP 与 HSDP

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 DDP、FSDP、HSDP 三种数据并行在「参数放哪里、优化器状态放哪里、梯度用什么集合通信聚合」上的本质差异，并会算三种方案的显存与通信账。
2. 读懂 `DataParallelConfig` / `TransformerDataParallelConfig` 的每个字段，特别是 HSDP 的 `num_replicas`（副本数）与 `shard_degree`（分片度）如何从 world size 推导（默认每节点一副本）。
3. 跟踪 `build_world_mesh()` 如何把总 world size 逐级切分出 DP 维，HSDP 又如何把 DP 拆成 `dp_replicate × dp_shard` 两个 mesh 维度；并区分 `get_dp_model_mesh()`（给 DDP/FSDP 包装用）与 `get_dp_mesh()`（给数据加载用）。
4. 精读 `parallelize_model()` 中 FSDP/DDP 的应用代码，理解 FSDP 的包裹策略（wrapping strategy，等价于 FSDP1 时代的 wrap policy）与 `fully_shard` 的落点。
5. 理解「梯度只在最后一个 micro-batch 同步」这一跨模块约定在 FSDP（`set_is_last_backward` / `set_requires_all_reduce`）与 DDP（`no_sync()`）两侧分别如何实现。
6. 了解 `OLMoDDPTrainModule`（融合 MoE-v2 路线）对 `dp_config` 的特殊约束，以及 `use_reduce_scatter` 如何把普通参数梯度直接归约进分布式优化器分片。

本讲承接 u6-l2：那里我们知道了并行配置全部挂在 `TransformerTrainModuleConfig` 上、`parallelize_model()` 以固定顺序改造模型（PP→FP8→CP→TP→EP→激活检查点→compile→**DP**→初始化权重）；本讲就打开其中的「DP」这一步。

## 2. 前置知识

- **rank 与 world size**：`torchrun` 拉起的每个进程是一个 rank，进程总数是 world size。rank 之间通过进程组（process group）通信，底层后端常用 NCCL（GPU）或 gloo（CPU）。
- **DeviceMesh（设备网格）**：PyTorch 提供的多维 rank 网格。例如 8 个 rank 可以摆成 `(2, 4)` 的二维网格，每个维度各自对应一个进程组。OLMo-core 用 mesh 维度名字（`dp`、`tp`、`pp`……）标记每一维的含义。
- **数据并行的直觉**：每个 rank 拿到**不同的** batch，各自前向反向算出本地梯度；因为要更新的是「同一个」模型，各 rank 的梯度必须聚合成一致的平均梯度。聚合用的集合通信原语主要是：
  - **all-reduce**：所有 rank 各出一个梯度张量，结束后所有 rank 都拿到逐元素和（或平均）——DDP 的做法；
  - **reduce-scatter**：聚合之后不在每个 rank 上放完整结果，而是把结果**切成 D 份、每个 rank 只拿自己那份**——FSDP 的做法；
  - **all-gather**：每个 rank 各持一片，拼出完整张量发给所有人——FSDP 前向时临时物化整层参数用的就是它。
- **梯度累积（micro-batch）**：承接 u6-l2——一个 batch 按 `rank_microbatch_size` 切成多个 micro-batch 逐个前向反向，梯度在本地累加，最后一步才同步。
- **混合精度的两个 dtype**：`param_dtype` 是前向/反向时参数的 dtype（如 bf16），`reduce_dtype` 是梯度归约时的 dtype（通常 fp32，避免低精度累加误差）。
- **ZeRO 直觉**：优化器状态（AdamW 的一阶/二阶动量）通常是参数量的 2 倍，把它们也切到各 rank（每 rank 只为 1/D 的参数维护动量）就是 ZeRO 思想；FSDP 天然做到这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/distributed/parallel/data_parallel.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L1-L81) | `DataParallelType` 枚举（fsdp/hsdp/ddp）与 `DataParallelConfig` 配置类，HSDP 的副本/分片度推导 |
| [src/olmo_core/distributed/parallel/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L1-L615) | `build_world_mesh()` 世界网格构建、`get_dp_model_mesh()` / `get_dp_mesh()` / `get_dp_process_group()` 子网格提取 |
| [src/olmo_core/train/train_module/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L227-L268) | `TransformerDataParallelConfig`（FSDP 包裹策略、fp32 梯度归约等 transformer 专属字段）与 `TransformerTrainModuleConfig.dp_config` 字段 |
| [src/olmo_core/train/train_module/transformer/common.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L34-L158) | `parallelize_model()`：按 dp_config 选择 `apply_fsdp()` 或 `apply_ddp()` 包裹模型 |
| [src/olmo_core/nn/transformer/model.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1174-L1283) | `Transformer.apply_fsdp()`（fully_shard 落点）与 `Transformer.apply_ddp()`（replicate 落点） |
| [src/olmo_core/nn/transformer/block.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L221-L236) | `TransformerBlock.apply_fsdp()`：fine_grained 包裹策略在 block 内部的落点 |
| [src/olmo_core/nn/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L42-L60) | `TransformerDataParallelWrappingStrategy` 枚举（full / blocks / fine_grained） |
| [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L98-L729) | `TransformerTrainModule`：mesh 构建、micro-batch 梯度同步时机（`no_sync` 等）、FSDP 感知的梯度裁剪 |
| [src/olmo_core/train/train_module/transformer/ddp_train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L146-L409) | `OLMoDDPTrainModule`：融合 MoE-v2 模型的 DDP 训练模块（分布式优化器路线） |
| [src/test/distributed/parallel/parallel_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L30-L160) | mesh 构建的参数化测试（CPU + gloo 可跑），本讲实践的主要依据 |
| [src/olmo_core/testing/distributed.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L111-L147) | `run_distributed_test()`：单机多进程模拟分布式环境的测试工具 |

## 4. 核心概念与源码讲解

### 4.1 DataParallelConfig：三种数据并行的统一入口

#### 4.1.1 概念说明

「数据并行」这个词只约定了一件事：**各 rank 吃不同的数据，模型按同一种方式组织**。至于参数、梯度和优化器状态摆在哪里、梯度用什么集合通信聚合，有三种经典答案，OLMo-core 用一个三值枚举 `DataParallelType` 表达：

| 类型 | 参数存放 | 梯度聚合 | 优化器状态 | 每卡显存（相对量） |
| --- | --- | --- | --- | --- |
| `ddp` | 每卡完整复制 | 反向 all-reduce | 每卡完整 | \( 4P \) |
| `fsdp` | 全部 rank 切成 D 片 | reduce-scatter 直接入分片 | 切片（1/D） | \( 4P/D \) |
| `hsdp` | R 个副本组、每组内 S 片 | 组内 reduce-scatter + 组间 all-reduce | 组内切片（1/S） | \( 4P/S \) |

其中 \( P \) 是参数元素总量（按 fp32 计），\( 4P = P_{\text{参数}} + P_{\text{梯度}} + 2P_{\text{AdamW动量}} \)，\( D \) 是数据并行 world size，HSDP 满足 \( D = R \times S \)（R 份副本、每份 S 卡分片）。

为什么需要 FSDP/HSDP？DDP 每卡都放全量参数 + 全量 AdamW 动量，模型一大（如 32B）单卡根本放不下。FSDP 把参数、梯度、动量全部切片，代价是前向/反向要 all-gather 临时拼出整层参数。HSDP（Hybrid Sharded Data Parallel，混合分片）是两者的折中：**只在组内（通常是一个节点内）分片，组间保持复制**——all-gather/reduce-scatter 只在节点内高速互连（NVLink）上做，跨节点只做副本间的 all-reduce。

#### 4.1.2 核心流程

`DataParallelConfig` 的消费流程：

1. 用户在 `TransformerTrainModuleConfig.dp_config` 上填一个 `TransformerDataParallelConfig(name=...)`（继承自 `DataParallelConfig`）。
2. `build_world_mesh(dp=dp_config, ...)` 据此决定 mesh 中 DP 维的形状（HSDP 拆两维，见 4.2）。
3. `parallelize_model()` 据此选择 `apply_fsdp()`（fsdp/hsdp 共用）或 `apply_ddp()`（见 4.3/4.4）。

HSDP 度数推导规则（`get_replicate_and_shard_degree`）：

```text
输入: dp_world_size (D)
若 num_replicas 和 shard_degree 都没给:
    num_replicas  = 节点数          # 默认每节点一副本
    shard_degree  = D // 节点数     # 每节点内的 GPU 数
若都给了:  校验 D 能被两者整除后直接使用
若只给 num_replicas: shard_degree = D // num_replicas
若只给 shard_degree: num_replicas = D // shard_degree
约束: num_replicas × shard_degree == D
```

#### 4.1.3 源码精读

三种类型定义在一个三值 StrEnum 里（承接 u2-l1：StrEnum 可被 Config 系统直接序列化）：

[data_parallel.py:L27-L30](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L27-L30) —— 定义 `fsdp` / `hsdp` / `ddp` 三个取值。

[data_parallel.py:L33-L39](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L33-L39) —— `DataParallelConfig` 的五个字段：`name`（必填，选类型）、`param_dtype`（前向/反向参数精度，None 表示沿用模型当前 dtype）、`reduce_dtype`（梯度归约精度，默认 fp32）、`num_replicas` 与 `shard_degree`（仅 HSDP 使用，见下）。

[data_parallel.py:L41-L63](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L41-L63) —— `get_replicate_and_shard_degree()`：HSDP 度数推导的全部逻辑。注意注释写明的默认值：**一个节点一份副本，shard degree 等于每节点 GPU 数**。这个默认值隐含了「节点内 NVLink 快、节点间网络慢」的拓扑假设。

节点数怎么来？[distributed/utils.py:L338-L353](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L338-L353) —— `get_num_nodes()` 优先读 `NUM_NODES` 环境变量，否则用 world size 除以 local world size；**未初始化分布式时恒返回 1**（所以单机测试里 HSDP 默认退化为「1 副本 × D 分片」，即纯 FSDP 形状）。

[data_parallel.py:L66-L81](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L66-L81) —— 两个私有校验函数：`dp_world_size` 必须能被 `num_replicas` / `shard_degree` 整除，否则抛 `OLMoConfigurationError`。例如 8 卡设 `num_replicas=3` 会直接报错，而不是静默给出错误布局。

[data_parallel.py:L12-L24](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L12-L24) —— `DPMeshDimName`：预先登记 HSDP 在 mesh 中要用的两个维度名 `dp_replicate`（复制维）与 `dp_shard`（分片维），供 `build_world_mesh()` 引用。

#### 4.1.4 代码实践

**实践目标**：在不启动任何分布式环境的情况下，验证 HSDP 度数推导规则与整除校验。

1. 打开 Python 交互环境（已 `pip install -e '.[all]'`），执行（示例代码）：

```python
from olmo_core.distributed.parallel.data_parallel import DataParallelConfig
from olmo_core.exceptions import OLMoConfigurationError

cfg = DataParallelConfig(name="hsdp")           # 什么都不指定
print(cfg.get_replicate_and_shard_degree(8))    # 单机未初始化分布式 → (1, 8)

print(DataParallelConfig(name="hsdp", num_replicas=2)
      .get_replicate_and_shard_degree(8))       # 只给副本数 → (2, 4)
print(DataParallelConfig(name="hsdp", shard_degree=2)
      .get_replicate_and_shard_degree(8))       # 只给分片度 → (4, 2)

try:
    DataParallelConfig(name="hsdp", num_replicas=3).get_replicate_and_shard_degree(8)
except OLMoConfigurationError as e:
    print("报错:", e)                            # 8 不能被 3 整除
```

2. 需要观察的现象：第一条输出是 `(1, 8)`——因为交互环境没初始化分布式，`get_num_nodes()` 返回 1。
3. 预期结果：四条输出分别是 `(1, 8)`、`(2, 4)`、`(4, 2)`、一条整除性报错。若与预期不符，回读 [data_parallel.py:L41-L63](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L41-L63) 找原因。

#### 4.1.5 小练习与答案

**练习 1**：32B 参数模型（约 \( 3.2\times10^{10} \) 参数）用 AdamW 训练，fp32 记账。单卡 80GB 显存放不下 \( 4P \)（约 480GB 权重态），最少需要多少卡的全分片 FSDP 才能仅让「权重态」放进显存（忽略激活）？

**答案**：\( 4P \approx 4 \times 3.2\times10^{10} \times 4\text{B} = 512\text{GB} \)（参数 + 梯度 + 双动量）。\( 512/80 = 6.4 \)，向上取整至少 **7 卡**；实际还要留激活与通信缓冲，通常远多于 7 卡。

**练习 2**：8 卡单机改用 HSDP、`shard_degree=4`，每卡权重态显存是多少？和 FSDP、DDP 比？

**答案**：\( R = 8/4 = 2 \)，\( M_{\text{HSDP}} = 4P/4 = P \)。FSDP 是 \( 4P/8 = P/2 \)（更省），DDP 是 \( 4P \)（最费）。HSDP 介于两者之间，换来的是 all-gather 只在 4 卡小组内进行。

**练习 3**：为什么 `reduce_dtype` 默认 fp32 而不是跟随 `param_dtype`（如 bf16）？

**答案**：梯度归约是跨 rank 的逐元素求和，bf16 的尾数只有 8 位，跨几十个 rank 累加会引入明显舍入误差；fp32 归约保证聚合精度，参数本体仍可用低精度存放。这正是 `MixedPrecisionPolicy(param_dtype, reduce_dtype)` 把两个 dtype 分开的原因（见 4.3.3）。

### 4.2 world mesh：DP 维的切分与 HSDP 的二维结构

#### 4.2.1 概念说明

`build_world_mesh()` 是所有并行策略共享的地基：它把总 world size 组织成一个带名字的多维 `DeviceMesh`。本讲只关注 DP 相关的部分：

- 非 HSDP：DP 是**一个**维度 `dp`，大小等于从 world size 里扣掉 PP/CP/TP/EP 之后的余量。
- HSDP：DP 拆成**两个**维度 `dp_replicate × dp_shard`（模型复制维在前、分片维在后）。

还有一个容易踩的坑：**「给模型包装用的 DP mesh」和「给数据加载用的 DP mesh」不是同一个函数**。`get_dp_model_mesh()` 服务于 FSDP/DDP 包装；`get_dp_mesh()` 服务于数据加载器分配 batch。两者的差异在组合其他并行时显现——尤其上下文并行（CP）：CP 组内的各 rank 持有完整参数副本，梯度必须跨「DP × CP」一起归约，所以模型 mesh 会把 CP 折叠进 DP；但 CP 组内各 rank 处理的是**同一份**数据的不同序列段，所以数据 mesh 不包含 CP。专家并行（EP）则在两种 mesh 里都折叠进 DP（EP 组内各 rank 要吃不同数据）。

#### 4.2.2 核心流程

`build_world_mesh()` 的 DP 相关决策流程：

```text
dp_world_size = world_size
if pp: 校验整除, dp_world_size //= pp.degree
if cp: 校验整除, dp_world_size //= cp.degree
if tp: 校验整除, dp_world_size //= tp.degree
if ep: EP 目前只允许与 HSDP 搭配（EP 复用 dp_shard 维切专家）

mesh 维度列表 =
    [pp?]                       # 流水线最外层
    + (hsdp ? [dp_replicate, dp_shard] : [dp])   # ← 本讲核心
    + [cp?] + [tp?]

init_device_mesh(...)            # 创建 mesh，并为每个维度建进程组
get_dp_process_group(mesh)       # 确保 DP 进程组已建立
存入全局 _WORLD_MESH（只能建一次）
```

#### 4.2.3 源码精读

[parallel/__init__.py:L116-L124](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L116-L124) —— `build_world_mesh()` 签名：接收五种并行配置，返回 `DeviceMesh`。docstring 明确标注「若使用任何其他并行配置，则必须提供 DP 配置」——DP 是其余并行的基础维度。

[parallel/__init__.py:L166-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L166-L169) —— 五个配置全空时退化为单维 `dp` mesh；否则 dp 缺失直接抛错：其他并行必须有 DP 伴随。

[parallel/__init__.py:L171-L189](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L171-L189) —— PP→CP→TP 逐级整除校验并从 `dp_world_size` 里扣除。这解释了一个常见疑问：**DP 的 world size 是「剩下的」**，不是配置出来的。

[parallel/__init__.py:L190-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L190-L206) —— EP 的限制：目前「EP 只能搭配 HSDP」（HSDP 直接复用 `dp_shard` 维切专家，见 L199 注释与 L204-L206 的报错）。这是 u3-l5 讲过的 MoE 专家切分在 mesh 层的落点。

[parallel/__init__.py:L218-L234](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L218-L234) —— **本讲最关键的一段**：`dp.name == hsdp` 时调用 4.1 讲的 `get_replicate_and_shard_degree()`，把 DP 拆成 `dp_replicate`（大小 num_replicas）与 `dp_shard`（大小 shard_degree）两个 mesh 维度；否则追加单一 `dp` 维。若同时启用 EP，还要求 EP degree 与 shard_degree 相等（同一段 L226-L231）。

[parallel/__init__.py:L251-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L251-L259) —— `init_device_mesh()` 创建网格（每个维度自动获得进程组），随后显式调用 `get_dp_process_group()` 确保 DP 组存在，最后把 mesh 存入模块级 `_WORLD_MESH`——全局只允许构建一次，重复调用抛 `RuntimeError`（L157-L158）。

[parallel/__init__.py:L351-L365](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L351-L365) —— `get_dp_model_mesh()`：取出所有以 `dp` 开头的维度组成子 mesh，交给 FSDP/DDP 包装。它先经 [_get_model_mesh()（L311-L348）](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L311-L348) 做维度折叠：EP 维折进 DP；CP 维折叠进相邻 DP 维——L333-L336 的注释解释了原因（CP rank 各持完整参数、梯度需跨 DP×CP 归约）。

[parallel/__init__.py:L368-L406](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L368-L406) —— `get_dp_mesh()`：给数据加载用的 DP mesh。同样折叠 EP 维，但**拒绝**已折叠 CP 的 mesh（L400-L403），即数据视角的 DP 永远不含 CP 维。

[parallel/__init__.py:L409-L423](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L409-L423) —— `get_dp_process_group()`：数据加载所用的 DP 进程组（多维时先展平）。u6-l1 讲过的「数据按 DP rank 不重不漏切分」用的就是它。

测试对照：[parallel_test.py:L92-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L92-L123) 断言 HSDP 时 `dims_map["dp_replicate"] * dims_map["dp_shard"] == dp_world_size`、其余类型 `dims_map["dp"] == dp_world_size`，并逐一验证 `get_dp_mesh()` / `get_dp_model_mesh()` 的形状（含 CP/EP 折叠后的期望大小）。参数化矩阵见 [parallel_test.py:L136-L146](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L136-L146)。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上用 gloo 后端双进程直观看到三种类型的 mesh 差异。

1. 运行仓库自带的 mesh 测试（CPU 可跑、无需 GPU）：

```bash
pytest -v src/test/distributed/parallel/parallel_test.py -k cpu
```

2. 再自己写一个最小脚本（示例代码，保存为任意 `.py` 文件运行）：

```python
import torch
from olmo_core.distributed.parallel import (
    build_world_mesh, get_dp_mesh, get_dp_model_mesh,
)
from olmo_core.distributed.parallel.data_parallel import DataParallelConfig, DataParallelType
from olmo_core.testing import run_distributed_test

def worker(dp_name: str):
    dp = DataParallelConfig(name=dp_name)
    if dp.name == DataParallelType.hsdp:
        dp.num_replicas, dp.shard_degree = 2, 2   # 4 rank → 2 副本 × 2 分片
    mesh = build_world_mesh(dp=dp)
    print(f"[rank {torch.distributed.get_rank()}] {dp_name}: "
          f"mesh dims={dict(zip(mesh.mesh_dim_names, mesh.shape))}, "
          f"dp_model_mesh={get_dp_model_mesh(mesh).shape}, "
          f"dp_mesh={get_dp_mesh(mesh).shape}")

for name in ("ddp", "fsdp", "hsdp"):
    run_distributed_test(worker, backend="gloo", world_size=4,
                         start_method="spawn", func_args=(name,))
```

3. 需要观察的现象：`ddp` 只有 `dp=4` 一维；`fsdp` 同样是 `dp=4`（mesh 形状相同，差别在后面的包装方式）；`hsdp` 是 `dp_replicate=2, dp_shard=2` 两维。
4. 预期结果：三种类型下 `dp_model_mesh` 与 `dp_mesh` 形状均为 `(4,)` 或 `(2,2)`（对应总 rank 数）；若再次循环构建会因 `_WORLD_MESH` 重复而报错——`run_distributed_test` 每轮起新进程（spawn），所以不会触发。本脚本基于 [parallel_test.py:L149-L160](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L149-L160) 的同款模式，CPU 可直接验证。

#### 4.2.5 小练习与答案

**练习 1**：16 卡、PP=2、TP=2、剩余全给 DP。用 HSDP 且希望每个节点（8 卡/节点）一份副本，`num_replicas` 和 `shard_degree` 各是多少？

**答案**：dp_world_size = 16/(2×2) = 4。注意 DP 维已经很小：若 nodes=2，默认推导会得到 num_replicas=2、shard_degree=2——但 2×2=4 恰好成立。这里的陷阱是**副本/分片是作用在 DP world size 上，而不是总 world size 上**。

**练习 2**：为什么 `get_dp_model_mesh()` 要把 CP 维折叠进 DP，而 `get_dp_mesh()` 不折叠？

**答案**：模型视角——CP 各 rank 都持有完整参数、各自产生梯度，聚合梯度时必须把 CP 组当成一组成员，所以 DP×CP 要合并成一个大通信域；数据视角——CP 组内各 rank 消费同一 batch 的不同序列段，数据加载只需按 DP 维区分，折叠 CP 会让同一份数据被加载多份。

**练习 3**：`build_world_mesh` 为什么设计成「只能调用一次」？

**答案**：mesh 创建会建立进程组并登记到全局 `_WORLD_MESH`，重复创建既浪费通信资源又会产生不一致的组句柄；训练模块（4.4.3）与测试都依赖「全局唯一 mesh」这一不变量。

### 4.3 FSDP 路线：parallelize_model 与 apply_fsdp

#### 4.3.1 概念说明

`parallelize_model()` 是 u6-l2 讲过的「并行化固定顺序」的执行者，DP 是顺序中倒数第二步（最后一步才是 `init_weights()`）。`fsdp` 与 `hsdp` 共用同一条 FSDP 路线：都调用模型的 `apply_fsdp()`，唯一区别是传入的 mesh 不同——fsdp 传一维 `dp` mesh，hsdp 传 `dp_replicate × dp_shard` 二维 mesh（FSDP2 的 `fully_shard` 原生支持二维 mesh： shard 维分片、replicate 维复制，HSDP 语义由 PyTorch 侧解释）。

FSDP1 时代用 `auto_wrap_policy` 自动决定包裹哪些子模块；FSDP2 改为 **composable 的 `fully_shard(module)` 手动落点**。OLMo-core 把落点选择封装成 `TransformerDataParallelWrappingStrategy` 枚举——这就是本讲所说的「fsdp wrap policy 的配置」。

#### 4.3.2 核心流程

`apply_fsdp()` 的包裹流程：

```text
mp_policy = MixedPrecisionPolicy(param_dtype, reduce_dtype)
reshard_after_forward = False if 启用PP else True     # PP 时前向后不回收分片
for 每个 block:
    按 wrapping_strategy 落 fully_shard:
        fine_grained: 先 attention、再 feed_forward、最后整个 block
        其他:        只包整个 block
embeddings: fully_shard + set_unshard_in_backward(False)   # 反向不需要参数分片
             （tie_word_embeddings 时不包, 留在 root 组共享）
lm_head:    fully_shard(reshard_after_forward=False)
root(模型本体): fully_shard —— 收纳所有未被单独包裹的参数
prefetch_factor > 0 时: 建立前向预取链 block[i] → block[i+1...]
```

要点：FSDP2 的参数在包裹后变为 **DTensor**（Shard(0) 布局），`p.numel()` 是全局量、`p.to_local()` 才是本 rank 实际存储——这是本讲综合实践对比分片差异的原理。

#### 4.3.3 源码精读

[common.py:L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145) —— `parallelize_model()` 的 DP 段：先 `get_dp_model_mesh()` 取模型 DP mesh（4.2），再把 `param_dtype`/`reduce_dtype` 从 olmo 的 `DType` 枚举转成 `torch.dtype`；`name` 是 `fsdp` 或 `hsdp` 都走 `apply_fsdp()` 分支（MoE 模型先 `prepare_experts_for_fsdp()`），`ddp` 走 `apply_ddp()` 分支。包裹完成后的权重初始化在 [common.py:L147-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)（承接 u3-l6：`_apply_init` + `distribute_like` 保证分片一致性）。

[model.py:L1174-L1204](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1174-L1204) —— `apply_fsdp()` 开头：构造 `MixedPrecisionPolicy`（param/reduce 双 dtype 分离），并决定 `reshard_after_forward`——启用 PP 时置 False，避免每个 micro-batch 都重新 all-gather 整层参数。

[model.py:L1206-L1233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1206-L1233) —— 落点细节：逐 block 委托 `block.apply_fsdp()`（见下条）；embedding 单独分片且反向不物化（`set_unshard_in_backward(False)`）；**tie_word_embeddings 时 embedding 与 lm_head 共享参数，必须留在同一个（root）FSDP 组**（L1216-L1218 注释）；最后 `fully_shard(self)` 包住模型根，兜底收纳所有未单独包裹的参数。

[model.py:L1241-L1248](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1241-L1248) —— `prefetch_factor > 0` 时为每个 block 设置前向预取目标（下一个或多个 block、最后一个 block 预取 lm_head），用计算掩盖 all-gather 通信延迟。

[config.py:L42-L60](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L42-L60) —— `TransformerDataParallelWrappingStrategy` 三档：`full`（默认，包每个 block 与 lm_head）、`blocks`（不单独包 lm_head）、`fine_grained`（block 内再细分）。

[block.py:L221-L236](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L221-L236) —— `TransformerBlock.apply_fsdp()`：`fine_grained` 时依次 `fully_shard(self.attention)`、`fully_shard(self.feed_forward)`、再包 block 本身，并建立块内预取链。包裹粒度越细，单次 all-gather 的张量越小、流水越细，但通信次数与元数据开销越多——这正是 wrap policy 的调优空间。

[train/config.py:L227-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L227-L268) —— `TransformerDataParallelConfig` 在基类之上增加的 transformer 专属字段：`wrapping_strategy`（L233-L238）、`prefetch_factor`（L240）、`only_allreduce_last_microbatch`（L242-L248，见 4.5）、`reduce_grads_in_fp32` / `accumulate_grads_in_fp32`（L250-L254）、`bucket_cap_mb`（L256-L257）、`use_reduce_scatter`（L259-L268，见 4.5）。它在 [TransformerTrainModuleConfig](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L386-L415) 中通过 `dp_config` 字段（L411）挂载。

#### 4.3.4 代码实践

**实践目标**：通过修改 `wrapping_strategy`，观察 FSDP 包裹落点数量与日志变化。

1. 阅读 [common.py:L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145) 与 [model.py:L1206-L1233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1206-L1233)，数一数 `full` 策略下一个 N 层模型会调用多少次 `fully_shard`（答案：N 个 block + embedding + lm_head + root = N+3 次，tie 时 N+1 次）。
2. 若有 ≥2 张 GPU（无 GPU 则记「待本地验证」），仿照 [muon_test.py:L115-L124](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/muon_test.py#L115-L124) 的 `_run_fsdp_muon` 写法，把 `dp_config` 换成：

```python
TransformerDataParallelConfig(
    name=DataParallelType.fsdp,
    wrapping_strategy=TransformerDataParallelWrappingStrategy.fine_grained,
    prefetch_factor=1,
)
```

再用 `parallelize_model(model, world_mesh=..., device=..., dp_config=dp_config)` 包裹一个 `TransformerConfig.olmo2_30M(vocab_size=1024)` 模型。

3. 需要观察的现象：包裹后 `model.blocks["0"].attention` 与 `model.blocks["0"].feed_forward` 是否都变成了 `FSDPModule`；日志中 `Applied FSDP to the model with ...` 的 mesh 信息。
4. 预期结果：`fine_grained` 下块内 attention 与 feed_forward 各自成为独立 FSDP 单元；显存峰值进一步下降（单次物化的参数更小），但前向 all-gather 次数翻倍。GPU 行为**待本地验证**（本讲编写环境无 GPU，未实际运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 embedding 要 `set_unshard_in_backward(False)`？

**答案**：embedding 位于计算图最前端，反向传播到它之后就再也没有模块需要它的参数分片了，因此反向阶段无需物化（unshard）参数，省一次 all-gather。

**练习 2**：`wrapping_strategy=blocks`（不单独包 lm_head）的动机是什么？

**答案**：lm_head 不被单独包裹时，其参数并入 root FSDP 组，与 embedding/最终 norm 一起分片。对 vocab 很大的模型，lm_head 参数巨大，单独成组会让该组的 all-gather 成为长尾；并入 root 组可以平衡各组体积。取舍是 root 组变大、细粒度流水化程度降低。

**练习 3**：HSDP 与 FSDP 在 `apply_fsdp()` 代码路径上有何不同？

**答案**：**没有不同**——两者都调用同一个 `apply_fsdp()`，差异完全由传入的 mesh 形状表达（一维 `dp` vs 二维 `dp_replicate×dp_shard`），`fully_shard` 依据 mesh 维度数自动解释 HSDP 语义。这是「配置驱动、单一代码路径」设计的典型例子。

### 4.4 DDP 路线与梯度同步时机

#### 4.4.1 概念说明

`ddp` 类型走 PyTorch 的 composable DDP（`torch.distributed._composable.replicate`）：参数**完整复制**在每个 rank 上，反向传播时按桶（bucket）自动 all-reduce 梯度。它适合**模型放得下单卡**的场景——没有 all-gather 开销，实现最简单，通信只有反向的一轮 all-reduce。

本模块的第二个主题是**梯度同步时机**。u6-l2 讲过一个 batch 会被切成 K 个 micro-batch 逐个反向；由于 loss 已经按「全批有效 token 数 N」归一（sum/N 语义），本地梯度满足

\[ g_{\text{local}} = \sum_{i=1}^{K} g_i \]

即 K 次 micro-batch 的梯度**累加**等价于一次性全批反向。因此跨 rank 的聚合只需在**最后一个 micro-batch 反向后**做一次，中间各步同步是纯浪费。三种实现：

- DDP：非最后 micro-batch 用 `no_sync()` 上下文挂起归约；
- FSDP：`set_is_last_backward(True/False)` 告知 FSDP 何时等待并清理梯度归约；
- HSDP 额外有 `set_requires_all_reduce(is_last)`——把 replicate 维的 all-reduce 延迟到最后的 micro-batch（shard 维的 reduce-scatter 本来就发生在最后一个 backward）。

#### 4.4.2 核心流程

DDP 路径（`parallelize_model` → `apply_ddp`）：

```text
可选: 把模型 cast 到 param_dtype
可选: compile 时设置 torch._dynamo.config.optimize_ddp
replicate(model, device_mesh=dp_mesh, bucket_cap_mb=100)   # 原地改造, 不换对象
```

训练单步内的同步时机（`TransformerTrainModule.train_batch` 的 micro-batch 循环，见 u6-l2）：

```text
for i, micro_batch in enumerate(micro_batches):
    with _train_microbatch_context(i, K):    # ← 本模块主角
        forward; loss.backward()
# 循环外: optim_step 中按组 set_lr → optim.step()
```

`_train_microbatch_context(i, K)` 的内部分派：

```text
is_last_mb = (i == K - 1)
if 模型是 FSDPModule:
    set_is_last_backward(is_last_mb)
    if dp 类型是 hsdp: set_requires_all_reduce(is_last_mb)
elif 模型是 DDP:
    if not is_last_mb: 进入 no_sync() 上下文
```

#### 4.4.3 源码精读

[model.py:L1252-L1283](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1252-L1283) —— `Transformer.apply_ddp()`：先按 `param_dtype` 显式 cast 模型（L1264-L1267）；compile 场景下配置 dynamo 的 DDP 优化器（`ddp_optimizer` 或 `python_reducer_without_compiled_forward`，L1271-L1275）；核心一行是 L1277 的 `replicate(self, device_mesh=dp_mesh, bucket_cap_mb=100)`——composable DDP 原地注册反向钩子，**模型对象不换**，这与 FSDP2 的 `fully_shard` 风格一致。

[train_module.py:L171-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L171-L187) —— `TransformerTrainModule.__init__` 中：分布式环境下用 `dp_config` 等调用 `build_world_mesh()`（4.2）；**非分布式却给了并行配置则直接报错**（L179-L187）。随后 L198-L212 调用 `parallelize_model()`（4.3）。

[train_module.py:L636-L653](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L636-L653) —— `_train_microbatch_context()`：4.4.2 流程图的原文。FSDP 分支调用 `set_is_last_backward`；HSDP 分支多调 `set_requires_all_reduce`（注释原文："For HSDP we can delay the gradients all-reduce until the final micro-batch"）；DDP 分支对非最后 micro-batch 进入 `no_sync()`。

[train_module.py:L683-L710](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L683-L710) —— `_clip_grad_norm()`：梯度裁剪的 DP 感知实现。FSDP1 包装走模型自带 `clip_grad_norm_`；FSDP2 下各参数是 DTensor，`get_total_norm` 得到的是 `_NormPartial` 布局的 DTensor，必须 `full_tensor()` 归约成全局范数（L704-L707 注释解释了这与 u6-l2 所讲「DTensor 范数须 full_tensor 归约」是同一段代码）。这正是数据并行对训练循环的渗透：**连梯度裁剪都要知道参数是分片的**。

[train_module.py:L244-L246](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L244-L246) 与 [L280-L289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L280-L289) —— `dp_process_group` 属性（给 Trainer 的数据加载与指标归约用）与 `pre_train()` 里的整除校验：`global_batch_size` 必须能被 `rank_microbatch_size × DP world size` 整除——把 u6-l2 的 batch 尺寸契约与 DP 维度联系起来。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——验证「只在最后一个 micro-batch 同步」的正确性论证。

1. 精读 [train_module.py:L465-L493](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L465-L493) 的 micro-batch 循环：注意 `loss_div_factor=batch_num_tokens_for_loss`（全批有效 token 数）在循环**外**算好、对每个 micro-batch 相同。
2. 写下等价性论证：设第 i 个 micro-batch 的损失为 \( L_i = s_i / N \)（\( s_i \) 是该 micro-batch 的损失和，N 是全批 token 数），则 \( \nabla \sum_i L_i = \nabla (\sum_i s_i)/N = \nabla L_{\text{batch}} \)——即梯度累加天然等于全批平均损失梯度，中间不需要任何跨 rank 通信。
3. 回答：如果 `loss_div_factor` 误设为「每个 micro-batch 各自的 token 数」，这个论证还成立吗？
4. 预期结果：不成立——不同 micro-batch 的归一化系数不同，累加出来的不再是全批平均损失梯度，最后一次性 all-reduce 会得到与逐步同步不同的结果。这也解释了为什么 `batch_num_tokens_for_loss` 的计算被小心翼翼地放在循环外（且 SkipStepOptimizer 场景下还要先跨 rank 归约，见 u5-l3/u6-l2）。

#### 4.4.5 小练习与答案

**练习 1**：`no_sync()` 挂起归约后，DDP 的梯度桶里存的是什么？

**答案**：本地梯度在桶视图里**继续累加**（DDP 的 reducer 在 no_sync 下不触发 all-reduce，但 autograd 钩子仍把各参数梯度写进桶）。退出 no_sync 后的最后一个 backward 触发一次 all-reduce，通信量从 K 次降为 1 次。

**练习 2**：DDP 的 `bucket_cap_mb=100`（[model.py:L1277](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1277)）调大/调小各有什么影响？

**答案**：桶越大，单次 all-reduce 通信越大、次数越少，越容易与其他计算重叠，但梯度「就绪等待」的尾巴越长（桶要等慢的参数到齐）；桶越小则反之。100MiB 是偏大的吞吐优先取值。

**练习 3**：为什么 `_train_microbatch_context` 对 FSDP 与 DDP 用完全不同的机制（标志位 vs 上下文管理器）表达同一件事？

**答案**：这是两个 PyTorch API 的形状差异——FSDP2 通过 `FSDPModule` 的 setter 声明「最后一个 backward」，DDP（含 composable replicate）通过 `no_sync()` 上下文挂起归约。OLMo-core 的价值在于把两种机制收敛到同一个 hook 点，训练循环正文（u6-l2）完全不感知差异。

### 4.5 OLMoDDPTrainModule：融合 MoE-v2 的 DDP 变体与 reduce-scatter

#### 4.5.1 概念说明

读源码时千万别把两个「DDP」混为一谈：

1. **`DataParallelType.ddp`**：4.4 讲的标准路线，任何 `Transformer` 都能用，配 `TransformerTrainModule` 与普通 `OptimConfig`。
2. **`OLMoDDPTrainModule`**（本模块，位于 `ddp_train_module.py`）：一条**平行的高级路线**，专门训练融合 MoE-v2 Transformer（模型类 `OLMoDDPModel`，见 u3-l5），配 `OLMoDDPOptimizerConfig`——一个**分布式优化器**：普通参数（非专家参数）的 fp32 主参数与 AdamW 动量可以按 DP 维切片存放（ZeRO 风格），梯度同步由模型侧的 `MultiGroupDistributedDataParallel` 包装器拥有。

这条路线最有意思的开关是 `TransformerDataParallelConfig.use_reduce_scatter`：开启后**普通参数的梯度直接 reduce-scatter 进优化器分片**——反传结束的瞬间，每个 rank 手里的梯度恰好只是自己负责的那 1/D 片，优化器状态无需物化全量梯度。拥有复制优化器状态的参数（如某些共享缓冲）仍走 all-reduce。

#### 4.5.2 核心流程

`OLMoDDPTrainModule.__init__` 中与 DP 相关的构造流程：

```text
校验: dp_config 必须存在且 name == "ddp"
校验: only_allreduce_last_microbatch 必须为 True
       （MultiGroupDDP 的桶视图一个累积窗口只能被 reduce 一次）
校验: use_reduce_scatter 的三项附加约束
       （要求最后一步同步 / 不支持 CP / PP 时要求自定义 stage 实现）
构建自有 dense_mesh（维度 = [pp?, dp, cp?]）与可选 moe_mesh
parallelize_and_init_model(...): EP/CP/PP 改造 + DP 包裹 + 初始化
optim.build(model_parts, ...): 构建分布式优化器
if use_reduce_scatter:
    sharded = optim.normal_params_with_sharded_optimizer_state()
    对每个 model part: MultiGroupDDP.configure_reduce_scatter_params(本 part 的分片参数)
```

训练步内的配套约定：`train_batch` 末尾对每个 model part 调 `finalize_grad_reduce()`；`zero_grads` 用 `m.zero_grad(set_to_none=False)` 保持桶视图稳定（优化器不负责清模型梯度缓冲）。

#### 4.5.3 源码精读

[ddp_train_module.py:L146-L175](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L146-L175) —— `OLMoDDPTrainModule` 类定义与构造签名：模型必须是 `OLMoDDPModel`（L175 断言），优化器是 `OLMoDDPOptimizerConfig`，同样是 DP/TP/CP/EP/PP 六套配置齐挂。

[ddp_train_module.py:L244-L245](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L244-L245) —— 两条硬断言：dp_config 必填、且 `name` 必须是 `"ddp"`。也就是说这条路线**不支持 fsdp/hsdp**——参数复制、优化器状态分片，省显存的任务全交给分布式优化器而不是参数分片。

[ddp_train_module.py:L270-L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L270-L279) —— `only_allreduce_last_microbatch=True` 被强制要求，L270-L275 的注释给出理由：`MultiGroupDistributedDataParallel` 把梯度**原地累加进扁平桶视图**，一个累积窗口内每个桶只能被 all-reduce 一次；若每个 micro-batch 都 reduce，会重复归约正在写入的桶，导致梯度缺失或损坏。这是 4.4 「最后一步同步」约定在这条路线上的工程化表述。

[ddp_train_module.py:L246-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L246-L268) —— `use_reduce_scatter` 的三项约束：必须 `only_allreduce_last_microbatch=True`；不支持与 CP 同用（稠密梯度在 DP×CP 上归约而优化器状态只在 DP 上分片，两者不一致）；与 PP 同用时要求 `use_custom_stage_implementation=True`（非最终 stage 的本地反向要跑在 `no_sync()` 里）。字段语义见 [train/config.py:L259-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L259-L268)。

[ddp_train_module.py:L296-L309](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L296-L309) —— 与标准模块不同：这条路线**强制要求分布式环境**（`is_distributed()` 为假直接抛错），并自建 mesh（不走 `build_world_mesh()`）。

[ddp_train_module.py:L532-L548](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L532-L548) —— 自有 dense mesh 的维度拼装：`[pp?] + [dp] + [cp?]`——注意这里 DP 永远是单维（因为 name 锁死为 ddp，不存在 HSDP 拆维）。DP 进程组在 [L630](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L630) 取自 `self.dense_mesh["dp"].get_group()`；MoE 部分另有 `ep_dp`/`ep_mp` 二维 mesh（专家的数据并行与模型并行，详见 u7-l2）。

[ddp_train_module.py:L365-L400](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L365-L400) —— 优化器构建与 reduce-scatter 接线：`optim.build(model_parts, self, strict=False)` 后，若 `use_reduce_scatter`，取出「优化器状态分片的普通参数」集合，逐 model part 调 `configure_reduce_scatter_params()`，并确保每个分片参数都被某个 `MultiGroupDistributedDataParallel` part 管理（L396-L400 漏接即抛错）。L380-L382 的注释点明分工：MultiGroupDDP 拥有普通参数归约，优化器旧的 post-backward reducer 被停用。

[ddp_train_module.py:L1824-L1827](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L1824-L1827) 与 [L2290-L2295](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L2290-L2295) —— 配套契约：`train_batch` 尾声对每个 part 调 `finalize_grad_reduce()`（确保挂起的归约完成）；`zero_grads` 用 `set_to_none=False` 清梯度——桶视图地址必须保持稳定，不能置 None 重建（注释原文："Keep grad-buffer lifecycle in the model wrapper so bucket views stay stable"）。

配置入口：[train/config.py:L484-L539](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L484-L539) 的 `OLMoDDPTrainModuleConfig`（optim 字段类型锁为 `OLMoDDPOptimizerConfig`），其 `build()` 在 [L541-L576](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L541-L576)，还顺手把废弃的 `grad_accum_in_fp32` 映射到 dp_config 的 `accumulate_grads_in_fp32`（L565-L569，避免旧配置被静默忽略）。

#### 4.5.4 代码实践

**实践目标**：验证这条路线的配置约束，并观察 CPU 上可跑的构造测试。

1. 运行两个纯配置测试（无分布式、无 GPU，可直接跑）：

```bash
pytest -v src/test/train/train_module/transformer/ddp_train_module_test.py -k roundtrips
```

2. 对照阅读 [ddp_train_module_test.py:L44-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/ddp_train_module_test.py#L44-L57)：注意第二个 round-trip 测试里 `dp_config` 用了 `name=DataParallelType.hsdp` 也能通过——因为这只是**序列化**测试；而真正构造模块的测试（[L102-L120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/train_module/transformer/ddp_train_module_test.py#L102-L120)）用的是 `name=DataParallelType.ddp`，否则会撞上 L245 的断言。
3. 需要观察的现象：两个 round-trip 测试通过；构造测试（`_run_construct_no_ep`）在你的机器上若无可用的分布式/GPU 环境会被跳过或需要 `run_distributed_test`（其 CPU 构造路径**待本地验证**）。
4. 预期结果：round-trip 断言 `restored == config` 成立——这是 u2-l1/u2-l2 所讲 Config 序列化能力在并行配置上的体现。

#### 4.5.5 小练习与答案

**练习 1**：`OLMoDDPTrainModule` 路线里，普通参数的显存账和 4.1 表格里的 DDP 行一样吗？

**答案**：不一样。参数与梯度仍是复制的（\( P \) + \( P \)），但优化器的 fp32 主参数与动量按 DP 切片（\( 2P/D \)），合计 \( 2P + 2P/D \)——介于 DDP（\( 4P \)）与 FSDP（\( 4P/D \)）之间；开启 `use_reduce_scatter` 后全量梯度缓冲也不再需要常驻。

**练习 2**：为什么 `use_reduce_scatter` 与上下文并行（CP）暂不兼容？

**答案**：[ddp_train_module.py:L247-L252](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L247-L252) 的报错信息给出了答案：稠密参数的梯度要在 **DP×CP** 联合进程组上归约（CP rank 各持完整参数，见 4.2），而优化器状态只在 **DP** 上分片——两个通信域不重合，reduce-scatter 的接收分片无法与优化器分片对齐。

**练习 3**：`zero_grads` 为什么坚持 `set_to_none=False`？

**答案**：MultiGroupDDP 的梯度桶是对扁平缓冲的**视图**，`set_to_none=True` 会把 `p.grad` 置为 None、下次反向重建张量，视图地址失效；保持缓冲不重建（只清零）才能让桶布局在步与步之间保持稳定。

## 5. 综合实践

**任务**：亲手对比 DDP 与 FSDP 两种配置下的参数分片差异，并用显存公式解释 HSDP 的取舍。分三部分，难度递进。

### 第 1 步：CPU 双进程验证 mesh 与度数（必做，无 GPU 可跑）

把下面脚本存为 `dp_mesh_compare.py`（示例代码）并运行：

```python
"""对比 ddp/fsdp/hsdp 三种 DataParallelConfig 产生的 mesh 形状（CPU + gloo）。"""
import torch
from olmo_core.distributed.parallel import build_world_mesh, get_dp_model_mesh, get_dp_mesh
from olmo_core.distributed.parallel.data_parallel import DataParallelConfig, DataParallelType
from olmo_core.testing import run_distributed_test

def worker(dp_name: str):
    dp = DataParallelConfig(name=dp_name)
    if dp.name == DataParallelType.hsdp:
        dp.num_replicas, dp.shard_degree = 2, 2
    mesh = build_world_mesh(dp=dp)
    r = torch.distributed.get_rank()
    print(f"rank{r} {dp_name}: dims={dict(zip(mesh.mesh_diam_names if False else mesh.mesh_dim_names, mesh.shape))} "
          f"model_mesh={get_dp_model_mesh(mesh).shape} data_mesh={get_dp_mesh(mesh).shape}")

if __name__ == "__main__":
    for name in ("ddp", "fsdp", "hsdp"):
        run_distributed_test(worker, backend="gloo", world_size=4,
                             start_method="spawn", func_args=(name,))
```

预期：`ddp`/`fsdp` 输出 `dp=4`；`hsdp` 输出 `dp_replicate=2, dp_shard=2`。三种类型的 `data_mesh` 形状积都是 4。仿照的测试蓝本是 [parallel_test.py:L149-L160](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L149-L160)。

### 第 2 步：对比参数分片与优化器状态（需 ≥2 GPU，无 GPU 则做源码推演并记「待本地验证」）

仿照 [muon_test.py:L115-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/muon_test.py#L115-L137)（`_run_fsdp_muon`）写一个 worker，分别用 `name=DataParallelType.ddp` 与 `name=DataParallelType.fsdp` 走 `build_world_mesh` → `TransformerConfig.olmo2_30M(vocab_size=1024).build(init_device=device.type)` → `parallelize_model(...)`，然后：

```python
from torch.distributed.tensor import DTensor
total_local = total_global = 0
for _, p in model.named_parameters():
    total_global += p.numel()                      # DTensor 的 numel() 是全局量
    total_local += p.to_local().numel() if isinstance(p, DTensor) else p.numel()
print(f"rank{torch.distributed.get_rank()}: local={total_local:,} global={total_global:,}")
```

预期结果：

| 配置 | 每个 rank 的 local | global |
| --- | --- | --- |
| ddp | = 全模型参数量（复制） | = 全模型参数量 |
| fsdp | ≈ 全模型参数量 / 2（相邻 rank 拼起来等于全量） | = 全模型参数量 |

再各自 `optim_config.build(model)` 做一次 `optim.step()`，对比两边 `len(optim.state)` 与单个参数状态张量的形状——DDP 的优化器状态是全量的，FSDP 的状态跟随 DTensor 分片（约为一半）。这一步的具体数值**待本地验证**（本讲编写环境无 GPU；FSDP 模型级包裹在仓库测试中均标注 `requires_multi_gpu`，如 [muon_test.py:L95-L112](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/muon_test.py#L95-L112)）。

### 第 3 步：用公式解释 HSDP 取舍（纸面推演）

设 8 卡单机、模型参数量 \( P \)、AdamW。分别计算并填表：

| 方案 | 每卡权重态 | all-gather 范围 | 跨组通信 |
| --- | --- | --- | --- |
| DDP | \( 4P \) | 无 | — |
| FSDP（D=8） | \( 4P/8 \) | 8 卡 | — |
| HSDP（R=2, S=4） | \( 4P/4 = P \) | 4 卡（组内） | 组间 all-reduce |

**结论要点**（也是本实践的交付物）：HSDP 每卡显存是 FSDP 的 \( R \) 倍、DDP 的 \( 1/S \)；all-gather 被限制在 \( S \) 卡小组内。当 \( S \) 恰好等于单节点 GPU 数时（这正是 [data_parallel.py:L41-L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/data_parallel.py#L41-L49) 的默认值），大体积的 all-gather/reduce-scatter 全部落在节点内 NVLink 上，跨节点只剩小范围的副本间 all-reduce——这就是 HSDP 相对 FSDP 在多节点场景的核心收益。

## 6. 本讲小结

- `DataParallelType` 三值（ddp/fsdp/hsdp）决定的不是「是否数据并行」，而是**参数/梯度/优化器状态的摆放方式与梯度聚合原语**：DDP 全复制 + all-reduce（每卡 \( 4P \)）；FSDP 全分片 + reduce-scatter/all-gather（每卡 \( 4P/D \)）；HSDP 组内分片、组间复制（每卡 \( 4P/S \)，all-gather 限制在组内）。
- HSDP 的 `num_replicas × shard_degree` 必须等于 DP world size，默认「每节点一副本、节点内 GPU 数为分片度」，由 `get_replicate_and_shard_degree()` 推导并在 mesh 中展开为 `dp_replicate × dp_shard` 两个维度。
- `build_world_mesh()` 把 DP 作为「扣除 PP/CP/TP/EP 之后的余量」切出来；`get_dp_model_mesh()`（模型包装用，折叠 EP 与 CP）与 `get_dp_mesh()`（数据加载用，不折叠 CP）是两个不同的投影。
- `parallelize_model()` 里 fsdp/hsdp 共用 `apply_fsdp()`（`fully_shard` 逐 block/embedding/lm_head/root 落点，粒度由 `TransformerDataParallelWrappingStrategy` 控制，即 FSDP2 时代的 wrap policy）；ddp 走 `replicate()`。
- 「梯度只在最后一个 micro-batch 同步」是跨模块契约：FSDP 用 `set_is_last_backward`（HSDP 再加 `set_requires_all_reduce`），DDP 用 `no_sync()`；其正确性依赖 u6-l2 的 sum/N 损失归一。
- `OLMoDDPTrainModule` 是融合 MoE-v2 的平行路线：dp_config 锁死 `name="ddp"`，梯度归约由 `MultiGroupDistributedDataParallel` 拥有，`use_reduce_scatter` 可把普通参数梯度直接归约进分布式优化器分片。

## 7. 下一步学习建议

下一讲 **u7-l2（张量、流水线、上下文与专家并行）** 将补齐 mesh 的其余维度：TP 如何切注意力头、PP 的进程组划分与 train module、CP（ring attention）的负载均衡、EP 的 `ep_dp`/`ep_mp` 折叠——其中 EP「只允许搭配 HSDP、复用 `dp_shard` 维」的结论正是本讲 4.2 埋下的伏笔。阅读建议：

- 精读 [src/olmo_core/distributed/parallel/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L116-L259) 的 `build_world_mesh()` 全文，把「维度顺序 = [pp, (dp_replicate, dp_shard)|dp, cp, tp]」记牢。
- 跑一遍 [src/test/distributed/parallel/parallel_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L136-L160) 的全部 CPU 参数化用例，对照 [parallel_test.py:L30-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/parallel/parallel_test.py#L30-L43) 的期望 DP world size 公式。
- 有条件时在 2×GPU 上跑 [src/test/optim/muon_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/optim/muon_test.py#L68-L112) 的 HSDP/FSDP 参数化用例，把本讲综合实践第 2 步的「待本地验证」补上。
