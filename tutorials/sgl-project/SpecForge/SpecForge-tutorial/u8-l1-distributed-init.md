# 分布式初始化与设备网格

## 1. 本讲目标

本讲是「分布式与并行」单元的第一讲。SpecForge 的训练面（trainer）默认就在多进程环境里运行：一个 `specforge train` 的 trainer 进程，要么是被 `torchrun` 拉起的若干 rank 之一，要么是单卡退化成「world_size=1」的孤进程。无论哪种情况，训练面都要先建立一套**进程组（process group）**与**设备网格（device mesh）**，后续的 FSDP 梯度归约、序列并行 attention、权重分片加载才有通信通道可用。

学完本讲，你应当能够：

- 说清 `init_distributed` 这一个函数如何从「三个并行度参数」推导出 TP/DP/SP 三类进程组与两张设备网格。
- 指出 `world_size` 必须满足的两个整除约束，以及它们分别来自哪段代码。
- 理解 `shard_tensor` / `gather_tensor` 这两个最朴素的集合通信算子在权重分片与序列并行中的角色。
- 知道 CUDA / ROCm / NPU（HCCL）三类硬件是如何在运行时被自动挑选的。

本讲只讲「初始化与拓扑建立」这一件事，至于 TP/SP 具体怎么用在 attention 与数据切分里，留给下一讲（u8-l2 并行拓扑）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **进程组（ProcessGroup）**：PyTorch 分布式通信的基本单位。一组 rank 加入同一个组后，才能在组内做 `all_reduce`、`all_gather` 等 collective。每个进程可以同时属于多个组。
- **rank 与 world_size**：`rank` 是进程在整个作业里的全局编号（从 0 开始），`world_size` 是作业进程总数。`LOCAL_RANK` 是本机内的编号，决定该进程绑定哪张卡。
- **torchrun rendezvous**：`torchrun` 通过 `MASTER_ADDR` / `MASTER_PORT` / `RANK` / `WORLD_SIZE` / `LOCAL_RANK` 这五个环境变量让各进程互相「会合」并建好默认进程组。SpecForge 复用 `env://` 初始化。
- **三类并行度**：
  - TP（tensor parallel，张量并行）：把同一层的权重切成多份，每张卡算一份，靠通信拼回完整结果。
  - DP（data parallel，数据并行）：每张卡拿不同数据各自算梯度，再 `all_reduce` 平均。
  - SP（sequence parallel，序列并行）：把一条长序列切成多段分给多张卡，各自只算自己那段再协同，SpecForge 用的是 **USP（Ulysses × Ring 复合序列并行）**。
- 来自 u6-l3 / u6-l4 的术语：**optimizer 边界信号**、**FSDP 后端**——本讲建立的进程组正是 FSDP 归约梯度的通道。

如果你对「为什么需要 DP/TP/SP」还不熟，可以把它想成：**TP 切模型、DP 切数据、SP 切序列**，三种切法正交，但都要先把世界划成小组。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `specforge/distributed.py` | 本讲主角。集中存放所有进程组、设备网格、集合通信算子的初始化与全局句柄。 |
| `specforge/cli.py` | 训练进程入口。决定「何时初始化、用哪些配置参数、结束时如何销毁」。 |
| `specforge/config/schema.py` | `TrainingConfig` 定义 `tp_size` / `sp_ulysses_size` / `sp_ring_size` / `dist_timeout` 四个字段，以及 `validate_world_size` 做整除校验。 |
| `specforge/utils.py` | `get_device_type()` 自动探测 CUDA/NPU/CPU，决定用哪个分布式后端。 |

一张图记住本讲主线：

```
cli._train
  └─ init_distributed(timeout, tp_size, sp_ulysses_size, sp_ring_size)   # distributed.py
        ├─ get_device_type() → _distributed_backend()  (nccl / hccl / gloo)
        ├─ _bind_local_device()  (绑定本机 LOCAL_RANK 卡)
        ├─ dist.init_process_group(...)
        ├─ device mesh = (dp, tp)            主网格
        ├─ draft mesh  = (draft_dp, sp)      草稿序列并行网格
        ├─ yunchang.set_seq_parallel_pg(...)  设置 ULYSSES_PG / RING_PG
        └─ 把全部句柄写进模块级全局变量
  └─ ... build_application_run(resolved).run() ...
  └─ finally: destroy_distributed()
```

## 4. 核心概念与源码讲解

### 4.1 init_distributed 与进程组建立

#### 4.1.1 概念说明

`init_distributed` 是训练面进入多进程世界的「总开关」。它做四件事：

1. **选后端**：根据设备类型选 `nccl`（CUDA/ROCm）、`hccl`（Ascend NPU）或 `gloo`（CPU）。
2. **绑本机设备**：把当前 rank 绑定到 `LOCAL_RANK` 指定的那张卡。
3. **建默认进程组**：调 `dist.init_process_group`，让所有 rank 通过 `env://` 会合。
4. **切小组 + 建网格**：用并行度参数把 `world_size` 划分成 TP/DP/SP 若干子组，并登记到全局句柄。

一个关键设计是：`init_distributed` **只被 trainer 进程调用，producer 永不调用**。producer 只负责捕获特征、提交 `SampleRef`（见 u7-l5），不参与梯度计算，因此不建进程组、不初始化 CUDA。这一点在 `cli._train` 里写得很清楚。

#### 4.1.2 核心流程

```
init_distributed(timeout, tp_size, sp_ulysses_size, sp_ring_size):
    1. device_type = get_device_type()          # cuda / npu / cpu
    2. backend     = _distributed_backend(device_type)   # nccl / hccl / gloo
    3. local_rank  = _bind_local_device(device_type)     # 绑卡
    4. (lazy) import yunchang 的序列并行全局量
    5. dist.init_process_group(backend, timeout)
    6. world_size = dist.get_world_size()
       dp_size    = world_size // tp_size
       assert world_size == tp_size * dp_size          # 约束①
    7. 主网格 (dp_size, tp_size)，dim 名 ("dp","tp")
    8. assert world_size % (sp_ulysses_size * sp_ring_size) == 0  # 约束②
       draft_dp_size = world_size // (sp_ulysses_size * sp_ring_size)
    9. 草稿网格 (draft_dp_size, sp_ulysses*sp_ring)，dim 名 ("draft_dp","sp")
   10. yunchang.set_seq_parallel_pg(sp_ulysses_size, sp_ring_size, rank, world_size)
   11. 从两张网格与 yunchang 全局量取出全部子组句柄，写入模块级全局变量
```

两个整除约束的形式化写法：

- 约束①：\[ \text{world\_size} \equiv 0 \pmod{\text{tp\_size}} \]
- 约束②：\[ \text{world\_size} \equiv 0 \pmod{\text{sp\_ulysses\_size} \times \text{sp\_ring\_size}} \]

其中约束①只是「主网格必须能被 tp 列整除」的等价说法——因为 `dp_size = world_size // tp_size`，若不能整除，则 `tp_size * dp_size != world_size`，断言会立刻失败。

#### 4.1.3 源码精读

`init_distributed` 的签名与并行度参数，注意三个并行度都默认为 1：

[specforge/distributed.py:133-135](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L133-L135) —— `init_distributed(timeout, tp_size=1, sp_ulysses_size=1, sp_ring_size=1)`，默认全 1 即「无并行、纯 DP」。

设备后端是一张静态映射表，CPU→gloo、CUDA→nccl、NPU→hccl：

[specforge/distributed.py:20-34](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L20-L34) —— `_DISTRIBUTED_BACKENDS` 字典与 `_distributed_backend()` 查表，未知设备类型直接 `ValueError`。

> 小知识：ROCm（AMD GPU）在 PyTorch 里复用的是 `torch.cuda` 这套 API，后端名仍是 `nccl`，所以这里没有单独的 `rocm` 条目——它走 `cuda → nccl` 这条路。

设备模块按需懒加载，NPU 要先 `import torch_npu` 才能让 `torch.npu` 可用：

[specforge/distributed.py:37-51](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L37-L51) —— `_device_module()` 在 `device_type == "npu"` 且 `torch` 还没挂 `npu` 属性时，尝试 `import torch_npu`，失败则报「需要兼容的 torch-npu 包」。

建组前先绑卡。HCCL 要求进程在初始化进程组**之前**就绑好本机 NPU，NCCL 也照此办理以避免异构主机上的 rank/设备歧义：

[specforge/distributed.py:62-85](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L62-L85) —— `_bind_local_device()` 读 `LOCAL_RANK`，校验它落在可见设备数范围内，再 `module.set_device(local_rank)`，CPU 直接返回 0。

建立默认进程组，超时单位是分钟：

[specforge/distributed.py:153](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L153) —— `dist.init_process_group(backend=backend, timeout=timedelta(minutes=timeout))`。

两个整除断言——这是本讲最值得记住的硬约束：

[specforge/distributed.py:156-168](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L156-L168) —— 先 `dp_size = world_size // tp_size` 并断言 `world_size == tp_size * dp_size`（约束①），再断言 `world_size % (sp_ulysses_size * sp_ring_size) == 0`（约束②）。

在 `cli.py` 一侧，`init_distributed` 只对 trainer 角色调用，producer 直接跳过：

[specforge/cli.py:121-137](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L121-L137) —— `if cfg.training.role == "producer"` 直接进组合根返回，**不** `init_distributed`；其余角色（all/consumer）才 `_bootstrap_single_process_env()` → `_validate_world_size` → `init_distributed(...)`，把配置里的 `dist_timeout`、`tp_size`、`sp_ulysses_size`、`sp_ring_size` 原样传进去。

进程组用完必须在 `finally` 里销毁，否则句柄会泄漏到下一次加载：

[specforge/cli.py:138-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L138-L146) —— `try: build_application_run(resolved).run() finally: destroy_distributed()`。

#### 4.1.4 代码实践

**实践目标**：确认 `init_distributed` 在「单卡、无并行」默认配置下不会真的拉起跨进程通信。

**操作步骤**：

1. 在安装好 SpecForge 的环境里，准备一个最小离线配置（或复用 `examples/configs/qwen3-8b-eagle3-offline.yaml`），保持 `training.tp_size`、`sp_ulysses_size`、`sp_ring_size` 全部为默认值 1。
2. 用单进程直接跑（不借助 torchrun）：
   ```bash
   specforge train --config examples/configs/qwen3-8b-eagle3-offline.yaml --plan
   ```
3. 把 `--plan` 去掉，让训练真正启动到 `init_distributed`（或在断点处打断点）。

**需要观察的现象**：

- 由于 `sp_size = 1*1 = 1`、`tp_size = 1`，两个断言都因 `world_size % 1 == 0` 与 `world_size == 1*world_size` 而通过。
- 日志里出现 `non-distributed: bind to cuda device 0`（`print_with_rank` 在未初始化分布式时打印 `non-distributed` 前缀）。
- 单进程下 `_bootstrap_single_process_env` 会自动补齐 `RANK=0`、`WORLD_SIZE=1` 等五个会合变量。

**预期结果**：训练顺利进入 `init_process_group`，不报整除错误，不卡在 rendezvous。**待本地验证**（需真实模型权重与 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 producer 进程不能调用 `init_distributed`？

**参考答案**：producer 只做特征捕获与 `SampleRef` 提交（u7-l5），不参与梯度计算，没必要建进程组；而且 HCCL/NCCL 初始化会占用 CUDA 上下文，producer 设计上要保持「CPU-only、可纯元数据」（见 u7-l5 的零拷贝不变量）。`cli.py` 里 `role == "producer"` 分支直接 `return build_application_run(resolved).run()`，绕过了 `init_distributed`。

**练习 2**：如果 `world_size=8`、`tp_size=3`，`init_distributed` 会怎样？

**参考答案**：`dp_size = 8 // 3 = 2`，断言 `8 == 3 * 2` 即 `8 == 6` 为假，立即抛 `AssertionError`，提示 `world size must be divisible by tp size`。这说明配置阶段就应保证整除——`schema.validate_world_size` 会在更早处用 `ValueError` 拦住。

---

### 4.2 TP / DP / SP 三类进程组与设备网格

#### 4.2.1 概念说明

`init_distributed` 一次建出**两张设备网格、五类子组**。理解它们的关系是本讲的核心。

设备网格（`DeviceMesh`）是 PyTorch 提供的高层抽象：把 `world_size` 个 rank 排成多维数组，每一维是一个命名的并行维度，沿着某一维取出来的 rank 子集就是一个进程组。SpecForge 用它避免手写 `dist.new_group(ranks=[...])` 的繁琐。

- **主网格**（给目标/通用层用）：形状 `(dp_size, tp_size)`，两维分别叫 `"dp"`、`"tp"`。
  - 沿 `"tp"` 维取组 → TP 组：同一条数据、切模型权重的 rank。
  - 沿 `"dp"` 维取组 → DP 组：不同数据、做梯度 all_reduce 的 rank。
- **草稿网格**（给草稿模型序列并行用）：形状 `(draft_dp_size, sp_size)`，两维叫 `"draft_dp"`、`"sp"`，其中 `sp_size = sp_ulysses_size × sp_ring_size`。
  - 沿 `"sp"` 维取组 → draft_sp 组：共享同一条长序列的 rank 集合。
  - 沿 `"draft_dp"` 维取组 → draft_dp 组：各自不同序列的 rank。
  - 在 SP 组内部，USP 算法又把它进一步切成 **Ulysses 组**（`sp_ulysses_size`）与 **Ring 组**（`sp_ring_size`）两个子组，分别承担「头维切分」与「序列段环形传递」。

值得强调：TP 与 SP 在当前 SpecForge 配置里**通常不同时开启**。离线训练强制 `tp_size=1`（schema 校验），在线 disaggregated consumer 则 TP/SP 全部为 1（consumer 只做 DP）。USP 只在离线 EAGLE3 长序列训练里开启。但 `init_distributed` 的代码本身同时支持两条轴，互不干扰。

#### 4.2.2 核心流程

网格形状由并行度推导：

- 主网格：`dp_size = world_size // tp_size`，形状 \((\text{dp\_size},\ \text{tp\_size})\)。
- 草稿网格：`sp_size = sp_ulysses_size × sp_ring_size`，`draft_dp_size = world_size // sp_size`，形状 \((\text{draft\_dp\_size},\ \text{sp\_size})\)。

子组来源有三：

1. **主网格 `get_group("tp"/"dp")`** → `_TP_GROUP` / `_DP_GROUP`。
2. **草稿网格 `get_group("draft_dp"/"sp")`** → `_DRAFT_DP_GROUP` / `_DRAFT_SP_GROUP`。
3. **yunchang 全局量** `process_group.ULYSSES_PG` / `RING_PG` → `_SP_ULYSSES_GROUP` / `_SP_RING_GROUP`，由 `set_seq_parallel_pg(sp_ulysses_size, sp_ring_size, rank, world_size)` 设置。

全部句柄存进模块级全局变量，配套一组 `get_*` 访问器（`get_tp_group`、`get_dp_group`、`get_sp_ulysses_group` 等）供训练层取用。

#### 4.2.3 源码精读

主网格的两维命名与构造：

[specforge/distributed.py:162-164](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L162-L164) —— `init_device_mesh(device_type, (dp_size, tp_size), mesh_dim_names=("dp","tp"))`，把 world 排成 `dp_size` 行 `tp_size` 列。

草稿网格把 SP 当成一个整体维度：

[specforge/distributed.py:170-175](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L170-L175) —— `draft_dp_size = world_size // (sp_ulysses_size*sp_ring_size)`，网格形状 `(draft_dp_size, sp_ulysses_size*sp_ring_size)`，两维 `"draft_dp"`、`"sp"`。

把 SP 组再拆成 Ulysses 与 Ring 两个子组，交给 yunchang：

[specforge/distributed.py:176-183](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L176-L183) —— `set_seq_parallel_pg(sp_ulysses_size, sp_ring_size, dist.get_rank(), world_size)` 设置 yunchang 的 `ULYSSES_PG` / `RING_PG`，再取出存进 `_SP_ULYSSES_GROUP` / `_SP_RING_GROUP`。

把五类句柄统一写进全局变量（注意 `_DRAFT_SP_GROUP` 取的是草稿网格的 `"sp"` 整维）：

[specforge/distributed.py:187-196](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L187-L196) —— 一次性赋值全部模块级全局，配套 `get_*` 访问器。`_DRAFT_SP_GROUP = draft_device_mesh.get_group("sp")`。

下游怎么用这些句柄？举两个典型例子：

- **数据预处理按 SP rank 切序列**：草稿序列并行要在数据侧就按 SP rank 取自己那段。

  [specforge/data/preprocessing.py:395-401](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L395-L401) —— `get_draft_sp_group()` 取 SP 组算 `sp_rank`/`sp_size`，`get_sp_ring_group()` 取 Ring 组算 `ring_rank`/`sp_ring_size`。

- **USP attention 取两个子组**：Ulysses 做头维 all-to-all，Ring 做序列段环形 KV 传递。

  [specforge/modeling/draft/llama3_eagle.py:1345-1357](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1345-L1357) —— `LlamaUSPAttention.__init__` 先 `assert dist.is_initialized()`（要求必须先 `init_distributed`），再 `self.ring_pg = get_sp_ring_group()`、`self.ulysses_pg = get_sp_ulysses_group()`，并记录各自的 degree 与 rank。

销毁时要小心：多个句柄可能指向同一个底层组（无 SP 时 draft_sp 与默认组别名），且退化的单 rank 组未在 torch 注册表里，`destroy` 会抛异常。代码用 `id()` 去重并 `try/except` 吞掉：

[specforge/distributed.py:199-246](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L199-L246) —— `destroy_distributed()` 遍历六个子组按 `id` 去重销毁，再销毁默认组，最后把全部全局变量置 `None`，避免「看起来已初始化但句柄失效」。

#### 4.2.4 代码实践

**实践目标**：纸上推演一次 8 卡 EAGLE3 离线 USP 的网格划分，验证整除约束与每个 rank 的归属。

**操作步骤**：

1. 假设 `world_size = 8`，配置 `tp_size = 1`（离线强制）、`sp_ulysses_size = 2`、`sp_ring_size = 2`、`attention_backend = "usp"`。
2. 计算：
   - `sp_size = 2 × 2 = 4`
   - `dp_size = 8 // 1 = 8`，主网格形状 `(8, 1)`
   - `draft_dp_size = 8 // 4 = 2`，草稿网格形状 `(2, 4)`
3. 检查两个约束：`8 % 1 == 0` ✓；`8 % 4 == 0` ✓。

**需要观察的现象 / 推演**：

- 全部 8 个 rank 被排成 2 行（draft_dp）× 4 列（sp）。同一行的 4 个 rank 共享一条序列（构成一个 SP 组），不同行的 2 个 rank 处理不同序列（DP 关系）。
- 在每个 SP 组（4 个 rank）内部，又被 yunchang 切成 Ulysses=2 与 Ring=2 两个子组。

**预期结果**：约束全部满足，`init_distributed` 可正常建组；每个 rank 同时拿到 `tp_group`（单 rank）、`dp_group`、`draft_sp_group`、`sp_ulysses_group`、`sp_ring_group` 五个句柄。**待本地验证**（需 8 卡环境，或在 `init_distributed` 前后插桩打印网格与各 `get_*_group` 的 world_size）。

#### 4.2.5 小练习与答案

**练习 1**：`world_size=8`、`sp_ulysses_size=4`、`sp_ring_size=2` 是否合法？

**参考答案**：`sp_size = 8`，约束② `8 % 8 == 0` ✓，`draft_dp_size = 1`，草稿网格形状 `(1, 8)`。合法，但此时草稿 DP 度为 1，意味着 8 个 rank 全部围着同一条序列做 SP，没有数据并行——长序列场景才划算。

**练习 2**：为什么需要 draft_sp、sp_ulysses、sp_ring 三个看似重叠的组？

**参考答案**：draft_sp（整 SP 维，大小 `sp_ulysses*sp_ring`）用于数据层按 SP rank 切序列、以及在非 USP 路径里做整体 gather；Ulysses 组与 Ring 组则是 USP 算法内部两种不同通信模式的通道——Ulysses 做 attention 头维的 all-to-all，Ring 做序列段的环形传递。算法层各取所需，所以三者并存。

---

### 4.3 shard / gather 算子与硬件后端选择

#### 4.3.1 概念说明

建好进程组只是「通了管道」，真正搬数据靠集合通信算子。`distributed.py` 提供了从最朴素到带反向传播的一组算子：

- **`shard_tensor`**：沿某维把张量切成 `world_size` 份，取本 rank 那一份。**没有通信**，纯本地切片。主要用于 TP 权重加载——每张卡只读自己那份权重。
- **`gather_tensor`**：`all_gather` 后 `cat`，把各 rank 的片段拼回完整张量。
- **`all_gather_tensor`**：用 `dist.all_gather_into_tensor` 的张量版本（更高效）。
- **`Gather`（autograd.Function）**：带反向传播的 gather，反向时按本 rank 切回自己的梯度片，并可选梯度缩放，专供序列并行的前向拼回。
- **`gather_outputs_and_unpad`**：面向 SP 输出的封装，默认沿 `get_draft_sp_group()` 拼回。

后端选择则贯穿整条链路：`get_device_type()` 决定走 cuda/npu/cpu，`_distributed_backend` 把它翻译成 nccl/hccl/gloo，`_device_module` 按需挂载 `torch.npu`。

#### 4.3.2 核心流程

`shard_tensor` 与 `gather_tensor` 是一对镜像：

```
shard_tensor(t, pg, dim):          # 本地切片，无通信
    rank = dist.get_rank(pg)
    size = dist.get_world_size(pg)
    return t.chunk(size, dim=dim)[rank].contiguous()

gather_tensor(t, pg, dim):         # all_gather + cat
    size = dist.get_world_size(pg)
    buf  = [empty_like(t) for _ in range(size)]
    dist.all_gather(buf, t, group=pg)
    return cat(buf, dim=dim)
```

设备类型探测优先级：环境变量 `SPECFORGE_DEVICE` > `torch.cuda` 可用 > `torch.npu` 可用 > CPU。

#### 4.3.3 源码精读

`shard_tensor`：最朴素的本地分片，连通信都没有：

[specforge/distributed.py:249-254](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L249-L254) —— `tensor.chunk(size, dim=dim)[rank].contiguous()`，靠「每个 rank 用不同 rank 号」自然取到不同片。

`gather_tensor`：标准的 all_gather + cat：

[specforge/distributed.py:257-264](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L257-L264) —— 预分配 `empty_like` 列表，`dist.all_gather` 填充，再 `torch.cat` 沿 `dim` 拼回。

带反向传播的 `Gather`：前向 all_gather 拼回，反向按 `part_size` 切回本 rank 梯度片，并乘 `sp_world_size` 做梯度缩放（注释标明改编自 verl 的 ulysses 实现）：

[specforge/distributed.py:283-324](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L283-L324) —— `forward` 存 `gather_dim`/`part_size`/`sp_rank`，`backward` 返回 `(None, grad[..., sp_rank], None, None, None)`。

SP 输出拼回的封装，默认用 draft_sp 组：

[specforge/distributed.py:327-351](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L327-L351) —— `gather_outputs_and_unpad`：组大小为 1 时直接返回原张量，否则 `Gather.apply(group, x, gather_dim, grad_scaler)`。

下游典型用法——TP 下线性层权重在加载时沿输出维（`dim=-1`）分片，每个 TP rank 只持有一列权重：

[specforge/layers/linear.py:54-60](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/linear.py#L54-L60) —— `handle_normal_layout` 用 `shard_tensor(state_dict["weight"], self.tp_group, -1)` 切权重，非 rank0 的 bias 清零。

硬件后端探测——优先环境变量，其次自动嗅探：

[specforge/utils.py:138-154](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/utils.py#L138-L154) —— `get_device_type()`：`SPECFORGE_DEVICE` → `torch.cuda.is_available()` → `torch.npu.is_available()` → `"cpu"`。

配置侧的整除校验，作为 `init_distributed` 断言的前置护栏：

[specforge/config/schema.py:863-878](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L863-L878) —— `validate_world_size(world_size)`：先校验 `world_size % tp_size == 0`，再校验 `world_size % (sp_ulysses_size*sp_ring_size) == 0`，分别对应本讲约束①②。它在 `cli._train` 里被调用两次（建组前读环境变量一次、建组后读真实 world_size 一次），把错误尽量拦在最早。

#### 4.3.4 代码实践

**实践目标**：阅读源码，理清 `tp_size` / `sp_ulysses_size` / `sp_ring_size` 三者如何共同决定设备网格，并指出 `world_size` 的整除约束。

**操作步骤**：

1. 打开 `specforge/distributed.py`，定位 `init_distributed`（L133 起）。
2. 顺着读 L156–L175，回答三个问题：
   - 主网格形状由哪两个参数决定？（答：`tp_size` 决定列数，`world_size // tp_size` 决定行数。）
   - 草稿网格形状由哪两个参数决定？（答：`sp_ulysses_size * sp_ring_size` 决定 `sp` 列数，`world_size // sp_size` 决定 `draft_dp` 行数。）
   - `world_size` 必须满足哪两条整除约束？（答：能被 `tp_size` 整除、能被 `sp_ulysses_size*sp_ring_size` 整除。）
3. 对照 `schema.validate_world_size`（schema.py L863），确认配置侧与初始化侧约束一致。
4. （可选）写一个 4 行的「参数→网格」推演表，填入若干组 `world_size/tp/sp` 值，验证约束。

**需要观察的现象**：`tp_size` 与 `sp_size` 是两条**独立**的轴——前者切主网格的列，后者切草稿网格的列；二者通过各自的整除约束各自把关，互不耦合。

**预期结果**：能用一句话概括——「`tp_size` 决定主网格 `(dp, tp)` 的 tp 维、`sp_ulysses_size × sp_ring_size` 决定草稿网格 `(draft_dp, sp)` 的 sp 维，`world_size` 必须同时被 `tp_size` 与 `sp_ulysses_size × sp_ring_size` 整除」。

#### 4.3.5 小练习与答案

**练习 1**：`shard_tensor` 有没有发生跨进程通信？为什么它能用于 TP 权重加载？

**参考答案**：没有通信——它只是本地 `chunk` 取本 rank 那片。能用于 TP 加载，是因为每个 TP rank 读的是**同一份**完整权重文件，但各自只保留 `chunk(size)[rank]` 那一片并丢弃其余，靠 rank 号不同自然得到不同的子矩阵，无需 any_scatter。

**练习 2**：`Gather` 这个 autograd.Function 的反向为什么要乘 `sp_world_size`（`grad_scaler=True`）？

**参考答案**：前向把 `sp_world_size` 个 rank 的片段 all_gather 拼成一份完整张量，相当于每份梯度在反向时被复制到了所有 rank；为保持梯度总量与单卡一致，反向需把回传梯度缩放回 `1/sp_world_size`（即乘 `sp_world_size` 的逆，配合 Ulysses 的均值归约语义），由 `grad_scaler` 控制。这让 SP 的 loss/梯度尺度与纯 DP 等价。

**练习 3**：在 ROCm（AMD GPU）上，`get_device_type()` 与 `_distributed_backend()` 分别返回什么？

**参考答案**：ROCm 复用 `torch.cuda`，故 `torch.cuda.is_available()` 为真，`get_device_type()` 返回 `"cuda"`；`_distributed_backend("cuda")` 返回 `"nccl"`（ROCm 版 PyTorch 的 nccl 即 hip-nccl）。所以 ROCm 走 `cuda → nccl` 这条与 NVIDIA 完全相同的路径，无需特殊分支。

## 5. 综合实践

**任务**：为一个「8 卡、EAGLE3 离线、长序列 USP」的训练作业设计并行度配置，并完整推演设备网格与每个 rank 的进程组归属，最后说明 `world_size` 整除约束是如何被三层校验保证的。

**步骤**：

1. **选并行度**。目标是把长序列铺到尽量多的卡上以降单卡显存，故开 USP：取 `sp_ulysses_size = 2`、`sp_ring_size = 2`，`sp_size = 4`。离线强制 `tp_size = 1`。
2. **推网格**：
   - 主网格：`dp_size = 8 // 1 = 8`，形状 `(8, 1)`——此时主网格只有 DP，没有 TP。
   - 草稿网格：`draft_dp_size = 8 // 4 = 2`，形状 `(2, 4)`——2 组数据并行，每组 4 卡共享一条序列。
3. **查约束**：
   - 约束① `8 % 1 == 0` ✓（`init_distributed` L156–L160）。
   - 约束② `8 % 4 == 0` ✓（`init_distributed` L166–L168）。
   - 配置侧 `validate_world_size`（schema.py L863）同样通过。
   - 另需满足 schema 的 USP 一致性：`attention_backend == "usp"` 时要求 `batch_size == 1` 且 `sp_size > 1`（schema.py L560–L569）。
4. **追句柄**：建组后，rank 0 会同时持有 `_DP_GROUP`（主网格 dp 维）、`_DRAFT_SP_GROUP`（草稿网格 sp 维，4 个 rank）、`_SP_ULYSSES_GROUP` 与 `_SP_RING_GROUP`（yunchang 切出的两个 2-rank 子组）、以及单 rank 的 `_TP_GROUP`。
5. **追校验链**：错误组合会被拦在三层——YAML 未知字段（StrictConfigModel）→ `validate_world_size`（建组前后各一次，cli.py L131/L141）→ `init_distributed` 内部断言（L158/L167）。

**预期产出**：一张「rank → 所属 (dp, draft_dp, sp, ulysses, ring) 组」的归属表，外加一句话结论：**只要 `world_size` 同时被 `tp_size` 和 `sp_ulysses_size × sp_ring_size` 整除，`init_distributed` 就能无歧义地把 world 划成两张网格、五类子组**。

## 6. 本讲小结

- `init_distributed(timeout, tp_size, sp_ulysses_size, sp_ring_size)` 是训练面进入分布式的唯一入口，只对 trainer 角色调用，producer 绕过。
- 它建出**两张设备网格**：主网格 `(dp_size, tp_size)` 与草稿网格 `(draft_dp_size, sp_size)`，其中 `sp_size = sp_ulysses_size × sp_ring_size`。
- 沿网格各维取出**五类子组**：TP、DP、draft_dp、draft_sp，外加 yunchang 切出的 Ulysses 与 Ring 两个 SP 子组，全部存进模块级全局并配 `get_*` 访问器。
- `world_size` 必须满足两条整除约束：能被 `tp_size` 整除、能被 `sp_ulysses_size × sp_ring_size` 整除，分别由 `init_distributed` 断言与 `schema.validate_world_size` 双重把关。
- `shard_tensor` 是无通信的本地分片（TP 权重加载），`gather_tensor` / `Gather` 是带/不带反向的拼回；USP attention 与数据切分都依赖这些组与算子。
- 硬件后端靠 `get_device_type()` 自动选：CUDA/ROCm→nccl、NPU→hccl、CPU→gloo；ROCm 复用 `torch.cuda` 故走 nccl 路径。

## 7. 下一步学习建议

- 下一讲 **u8-l2 并行拓扑 USP 与 ring attention** 会把本讲建立的 Ulysses/Ring 组用起来，讲清 USP 的头维 all-to-all 与序列段环形传递，以及各部署模式支持的并行组合表。
- 想看进程组在训练循环里如何驱动归约，可回看 **u6-l4 FSDP 后端与梯度累积**——`BF16Optimizer` 的梯度范数 all-reduce 正是跑在 `init_distributed` 建出的进程组上。
- 想理解数据侧如何按 SP rank 切序列，可读 `specforge/data/preprocessing.py` 与 `specforge/data/utils.py` 中对 `get_draft_sp_group` / `get_sp_ring_group` 的使用。
