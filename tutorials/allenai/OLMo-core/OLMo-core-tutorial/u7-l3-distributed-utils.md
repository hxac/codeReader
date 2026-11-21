# 分布式工具与测试基础设施

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `init_distributed()` 从环境变量到进程组的完整初始化流程，以及它做了哪些「额外的正确性加固」。
2. 区分三种 rank（全局 rank、节点内 local rank、文件系统 fs local rank）与三个规模量（world size、local world size、节点数），并知道各自从哪里读。
3. 掌握 `distributed/utils.py` 中「分布式透明」的集合通信封装（`all_gather`、`broadcast_object`、`all_reduce_value`、`do_n_at_a_time` 等），理解它们在单进程下如何自动退化。
4. 会用 `olmo_core.testing` 的 `run_distributed_test()` 在 pytest 里拉起真实的多进程分布式测试——哪怕只有 CPU 也能跑双进程 gloo 后端。
5. 能按清单排查常见的分布式初始化问题（缺 `LOCAL_RANK`、端口冲突、backend 不一致、多 rank 抢 GPU 0 等）。

本讲是第七单元（分布式训练）的收官：u7-l1 讲了数据并行的策略，u7-l2 讲了多维并行的组合，本讲下沉到它们共同依赖的地基——进程组管理与测试工具。

## 2. 前置知识

### 2.1 进程组与 rendezvous

`torch.distributed` 的世界由若干**独立进程**组成（不是线程）。要让它们协同，必须先回答三个问题：

- 我是谁？—— `RANK`（全局序号，从 0 开始）
- 一共多少人？—— `WORLD_SIZE`
- 大 家在哪集合？—— `MASTER_ADDR` + `MASTER_PORT`（这个「集合点」机制叫 **rendezvous**，碰头会）

每个进程读取这四个环境变量后调用 `torch.distributed.init_process_group()`，相互握手，形成一个**进程组（ProcessGroup）**。之后进程间就能做集合通信（all-reduce、all-gather、broadcast、barrier 等）。

torchrun 的职责正是在启动每个 worker 进程时注入这四个变量。回顾 u1-l4：训练脚本由 `torchrun --nproc-per-node=N` 拉起，`prepare_training_environment()` 再统一完成初始化。

### 2.2 后端（backend）：gloo 与 nccl

进程组通信走哪个底层库就是「后端」：

| 后端 | 设备 | 特点 |
| --- | --- | --- |
| gloo | CPU | 纯 CPU 可跑，测试友好，不支持 CUDA 张量的高速通信 |
| nccl | GPU | NVIDIA 高性能集合通信库，GPU 训练的主力 |

OLMo-core 常用复合写法 `cpu:gloo,cuda:nccl`：CUDA 张量走 nccl、CPU 张量走 gloo。这样异步 checkpoint、指标簿记等 CPU 侧集合通信不会阻塞 GPU 训练通信。

### 2.3 rank 的三个层次

- **全局 rank（global rank）**：在整个 `WORLD_SIZE` 中的编号，torchrun 分配。
- **local rank**：节点内编号（`LOCAL_RANK`），多机训练时每台机器都有自己的 0 号进程；**每张 GPU 由 local rank 决定**（`cuda:LOCAL_RANK`）。
- **fs local rank（文件系统内 rank）**：同一个文件系统视角下的编号——若所有节点共享 NFS/Weka，它等于全局 rank；若各节点磁盘独立，它等于 local rank。它决定「写本地文件时如何避免多个进程写同一位置」。

### 2.4 pytest 参数化

本讲的测试实践大量使用 `@pytest.mark.parametrize`，这在 u1-l2 已接触过：一个测试函数配多组参数，pytest 自动展开成多个用例。另外回顾 u1-l2 的跳过机制：`pytest.mark.skipif(...)` 与 `pytest.mark.gpu` 标记让无 GPU 环境自动跳过对应参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/distributed/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py) | 进程组初始化、rank/world 查询、分布式透明的集合通信封装、DTensor 工具 |
| [src/olmo_core/distributed/nn.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/nn.py) | autograd 兼容的 all-to-all 原语（给反向传播用的集合通信） |
| [src/olmo_core/testing/distributed.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py) | `run_distributed_test()`：在 pytest 里用多进程模拟分布式环境 |
| [src/olmo_core/testing/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/utils.py) | `BACKENDS`/`DEVICES` 参数化常量与各类能力探测（`has_cuda` 等） |
| [src/olmo_core/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py) | 支撑函数：`set_env_var`、`get_default_device`、`seed_all`、`move_to_device` |
| [src/olmo_core/train/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/__init__.py) | `prepare_training_environment()` / `teardown_training_environment()`：训练脚本的标准化入口 |
| [src/test/distributed/utils_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/utils_test.py) | `run_distributed_test` 的最小真实用例 |
| [src/test/distributed/nn_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/nn_test.py) | all-to-all 前向 + 反向的真实测试范例 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 `init_distributed()`：从环境变量到进程组**
2. **4.2 rank 三层体系与 world 查询函数**
3. **4.3 分布式透明的通信封装与 autograd 原语**
4. **4.4 `run_distributed_test()`：pytest 里的多进程沙盒**（本讲主实践）

### 4.1 `init_distributed()`：从环境变量到进程组

#### 4.1.1 概念说明

`torch.distributed.init_process_group()` 是 PyTorch 的原生入口，但直接用它有几个坑：环境变量不全时报错晦涩、多 rank 默认都在 GPU 0 上初始化 CUDA、NCCL 的某些默认行为会导致显存占用偏高或初始化挂起。OLMo-core 的 `init_distributed()` 是对它的**加固封装**：补环境变量、设 CUDA 设备、调 NCCL 参数、识别 Beaker 集群的网卡拓扑，最后才调用原生初始化，并做一遍环境变量校验。

它还支持**幂等重入**：如果进程组已经被（外部代码）初始化过，就跳过初始化只做其余设置——但要求 backend 一致，否则明确报错。

#### 4.1.2 核心流程

```
init_distributed(backend, timeout, ...)
  │
  ├─ 1. 设置全局 NCCL 环境加固变量
  │     TORCH_NCCL_AVOID_RECORD_STREAMS=1   # 防止异步集合通信长期占住显存
  │     TORCH_DIST_INIT_BARRIER=1           # 初始化时强制各 rank 同步
  │
  ├─ 2. 单进程兜底：若没有 RANK/WORLD_SIZE（说明不是 torchrun 拉起的），
  │     自动补一套 RANK=0, WORLD_SIZE=1, MASTER_ADDR=127.0.0.1, MASTER_PORT=<随机空闲端口>
  │
  ├─ 3. （可选）声明共享文件系统：OLMO_SHARED_FS=1
  │
  ├─ 4. Beaker 集群特判：按节点型号设置 NCCL 网卡/协议参数
  │
  ├─ 5. 若 backend 支持 CUDA：torch.cuda.set_device(cuda:LOCAL_RANK)
  │     —— 必须在 init_process_group 之前做，避免所有 rank 都在 GPU 0 初始化
  │
  ├─ 6. dist.init_process_group(backend, timeout, **kwargs)
  │     （若已初始化：检查 backend 一致 → 跳过）
  │
  └─ 7. validate_env_vars() + 打印 rank 摘要日志
```

#### 4.1.3 源码精读

整个文件顶部的环境变量常量是这个模块的「词汇表」——每个函数都围绕它们读写：

[src/olmo_core/distributed/utils.py:L20-L29](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L20-L29)

上面这段定义了 `RANK`、`WORLD_SIZE`、`MASTER_ADDR`、`MASTER_PORT`（torchrun 注入）以及 `LOCAL_RANK`、`LOCAL_WORLD_SIZE`、`NUM_NODES`、`FS_LOCAL_RANK`、`OLMO_SHARED_FS`（OLMo-core 约定的补充变量）的常量名。

单进程兜底逻辑：

[src/olmo_core/distributed/utils.py:L41-L69](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L41-L69)

这段是 `_bootstrap_single_process_distributed_env()`：当检测不到 `RANK`/`WORLD_SIZE`（即用户直接 `python train.py` 而非 `torchrun`），或显式声明 `WORLD_SIZE=1` 时，自动补齐一套「单进程分布式」环境变量，`MASTER_PORT` 用 `_find_free_local_port()` 绑定 0 号端口让操作系统随机分配（见 [L35-L38](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L35-L38)，bind 到 `("127.0.0.1", 0)` 后读回实际端口号）。**这就是为什么 OLMo-core 的训练脚本不加 torchrun 也能跑**——同一份代码在单进程下自动退化。

主函数骨架：

[src/olmo_core/distributed/utils.py:L88-L98](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L88-L98)

开头两个 `set_env_var` 调用先做 NCCL 加固（注释解释了动机：张量并行里 `async_op=True` 的集合通信会因 record streams 机制把显存占得过久）。`set_env_var` 本身来自 [src/olmo_core/utils.py:L258-L275](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L258-L275)：**已存在且值不同则默认跳过并告警**（不覆盖用户显式设置）。

设置 CUDA 设备的时机契约：

[src/olmo_core/distributed/utils.py:L163-L181](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L163-L181)

注意注释里的重点：`torch.cuda.set_device(cuda:LOCAL_RANK)` 必须放在 `init_process_group` **之前**，否则所有 rank 会在 GPU 0 上初始化 CUDA，造成显存与通信的错乱。之后若进程组已初始化，则校验 backend 一致性（不一致抛 `OLMoEnvironmentError`，提示「要么用同一个 backend，要么别重复初始化」），一致则跳过；未初始化才真正调用 `dist.init_process_group`。

收尾校验：

[src/olmo_core/distributed/utils.py:L183-L190](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L183-L190)

初始化完成后调用 `validate_env_vars()`（定义在 [L193-L215](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L193-L215)），它检查：分布式上下文必须有 `LOCAL_RANK`；非共享文件系统必须有 `FS_LOCAL_RANK` 或 `LOCAL_RANK` 之一；`NUM_NODES` 与 `LOCAL_WORLD_SIZE` 至少给一个。每条失败都抛出带**修复提示**的 `OLMoEnvironmentError`——这是排查问题的第一现场。最后打一行「全局 rank = local rank = fs local rank」的摘要日志，让你肉眼确认三个层次对齐。

训练脚本侧的标准入口在 `train/__init__.py`：

[src/olmo_core/train/__init__.py:L125-L148](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/__init__.py#L125-L148)

`prepare_training_environment()` 把 multiprocessing 启动方式设为 `spawn`（避免数据加载器段错误）、调用 `init_distributed`（backend 默认 `cpu:gloo,cuda:nccl`，docstring 在 [L85-L124](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/__init__.py#L85-L124) 解释了 CPU+CUDA 双后端的意义：异步 checkpoint 与簿记通信走 gloo 不阻塞训练）、配置日志、可选 `seed_all(seed)`。收尾由 `teardown_training_environment()`（[L151-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/__init__.py#L151-L156)）销毁进程组。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲眼看到单进程兜底与 `set_env_var` 的「不覆盖」行为。
2. **操作步骤**：
   - 在仓库外的任意空目录写一个小脚本（示例代码）：

     ```python
     # bootstrap_demo.py（示例代码）
     import os
     os.environ.pop("RANK", None)
     os.environ.pop("WORLD_SIZE", None)
     os.environ["NCCL_DEBUG"] = "INFO"  # 预先占用一个 set_env_var 会想设置的变量名做对照实验

     from olmo_core.distributed.utils import init_distributed, get_rank, get_local_rank

     init_distributed(backend="gloo", shared_filesytem=True)
     print(f"rank={get_rank()} local_rank={get_local_rank()}")
     print({k: v for k, v in os.environ.items() if k in
             ("RANK", "WORLD_SIZE", "LOCAL_RANK", "LOCAL_WORLD_SIZE",
              "NUM_NODES", "MASTER_ADDR", "MASTER_PORT", "OLMO_SHARED_FS")})
     ```

   - 用 `python bootstrap_demo.py` 直接运行（故意不用 torchrun）。
3. **需要观察的现象**：日志先打印「bootstrapping a single-process distributed setup」，随后打印的字典里 `RANK=0`、`WORLD_SIZE=1`、`MASTER_ADDR=127.0.0.1`、`MASTER_PORT` 是一个随机端口。
4. **预期结果**：脚本正常退出，`rank=0 local_rank=0`。这说明未用 torchrun 启动时也能建立（退化的）进程组。
5. 若你的环境行为与此不符，标注「待本地验证」并记录差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `torch.cuda.set_device()` 必须在 `dist.init_process_group()` 之前调用？

**答案**：如 [utils.py:L163-L168](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L163-L168) 的注释所说，若不先设置，进程组初始化过程中每个 rank 都会在默认的 GPU 0 上初始化 CUDA 上下文，导致 GPU 0 被所有进程抢占（显存浪费、通信错配）。先 `set_device(cuda:LOCAL_RANK)` 再初始化，每个进程只碰自己那张卡。

**练习 2**：如果你的代码在调用 `init_distributed(backend="gloo")` 之前已经用 nccl 初始化过进程组，会发生什么？

**答案**：不会重复初始化，也不会静默换后端——[utils.py:L170-L178](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L170-L178) 检测到已初始化且 backend 不一致时抛出 `OLMoEnvironmentError`，并提示「要么用同一 backend，要么不要预初始化」。一致时仅记录日志跳过。

**练习 3**：`validate_env_vars()` 在什么条件下要求必须有 `FS_LOCAL_RANK` 或 `LOCAL_RANK`？

**答案**：当 `OLMO_SHARED_FS` 不等于 `1`（即未声明共享文件系统）时（见 [utils.py:L203-L210](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L203-L210)）。因为不共享文件系统时，写本地文件必须靠这两个变量之一区分各进程的落盘位置，否则会互相覆盖。

### 4.2 rank 三层体系与 world 查询函数

#### 4.2.1 概念说明

这一组函数回答分布式世界里的「我是谁、有多少人」问题。它们共同的设计原则写在模块 docstring 第一行：**"most of which work in a non-distributed context as well for API unity"**（大部分函数在非分布式上下文也可用，以保证 API 统一）。也就是说，业务代码直接调用这些函数即可，**不需要写 `if is_distributed(): ... else: ...` 分支**——单进程时 `get_rank()` 返回 0、`get_world_size()` 返回 1，代码自然正确。u6-l1 讲过的指标跨 rank 归约、u6-l2 讲过的 loss all-reduce，底层用的都是这一层抽象。

#### 4.2.2 核心流程

| 函数 | 数据来源 | 单进程退化值 | 语义 |
| --- | --- | --- | --- |
| `get_rank(group)` | 进程组 | 0 | 组内 rank |
| `get_world_size(group)` | 进程组 | 1 | 组内进程总数 |
| `get_local_rank()` | `LOCAL_RANK` 环境变量 | 0 | 节点内 rank |
| `get_local_world_size()` | `LOCAL_WORLD_SIZE` 环境变量 | 1 | 节点内进程数（通常=GPU 数） |
| `get_num_nodes()` | `NUM_NODES` 或两者相除 | 1 | 节点数 |
| `get_fs_local_rank()` | `OLMO_SHARED_FS` / `FS_LOCAL_RANK` | 0 | 文件系统内 rank |
| `get_global_rank(group_rank, group)` | 进程组换算 | 原值 | 子组内 rank → 全局 rank |

注意分界线：`get_rank`/`get_world_size` 问**进程组**（动态建立的通信世界），`get_local_rank`/`get_num_nodes` 问**环境变量**（torchrun 描述的物理布局）。两者世界不同、来源不同。

`get_num_nodes()` 的换算关系：

\[
\text{num\_nodes} = \frac{\text{world\_size}}{\text{local\_world\_size}}
\]

#### 4.2.3 源码精读

判断函数与最简单的两个查询：

[src/olmo_core/distributed/utils.py:L234-L256](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L234-L256)

`is_distributed()` 就是「torch.distributed 可用且已初始化」；`barrier()` 在非分布式下是空操作；`get_rank()` 在非分布式下返回 0。这就是「API 统一」的具体形态。

fs local rank 的判定逻辑：

[src/olmo_core/distributed/utils.py:L285-L306](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L285-L306)

读这段代码：若声明了 `OLMO_SHARED_FS=1`（所有节点同一文件系统），fs local rank 直接取 `FS_LOCAL_RANK` 环境变量、缺省回退到**全局 rank**（保证全集群唯一路径）；否则回退到 **local rank**（每节点独立磁盘，节点内唯一即可）。docstring（L285-L300）用一个反例说清了语义：共享文件系统时它等价于 `get_rank()`，不共享时等价于 `get_local_rank()`。

节点数与 local world size：

[src/olmo_core/distributed/utils.py:L322-L353](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L322-L353)

`get_num_nodes()` 优先读 `NUM_NODES`，缺失时用 world size 整除 local world size 推算。两个函数的 docstring 都带 warning：它们依赖环境变量，且未初始化进程组时恒返回 1/1。

把全局 rank 映射到 DeviceMesh 坐标：

[src/olmo_core/distributed/utils.py:L450-L462](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L450-L462)

`get_mesh_coordinates()` 在 mesh 张量里找到当前全局 rank 的位置并转成坐标列表。这是 u7-l1/u7-l2 讲过的 `build_world_mesh()` 的逆查询：mesh 是「坐标 → rank」的表，这个函数给你「rank → 坐标」。断言 `size(0) in (0, 1)` 保证每个 rank 在 mesh 中至多出现一次。

后端能力判断：

[src/olmo_core/distributed/utils.py:L465-L490](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L465-L490)

`backend_supports_cuda()`（backend 含 `nccl` 为真）与 `backend_supports_cpu()`（含 `gloo` 或 `mpi` 为真）。它们被 `get_default_device()`（[src/olmo_core/utils.py:L175-L199](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L175-L199)）用来回答「当前进程该把张量放在哪」：分布式时看 backend，非分布式时按 cuda → mps → cpu 顺序探测。**测试代码里到处可见的 `get_default_device()` 就是从这里拿到正确设备的**，它让同一份测试在 CPU 与 GPU 环境都能跑。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：用一张真实的 4 进程布局验证 rank 三个层次的换算。
2. **操作步骤**：假设 `torchrun --nproc-per-node=2 --num-nodes=2`（2 节点 × 每节点 2 进程，共 4 进程）。做两件事：
   - 手算填表：全局 rank 0~3 各自的 `local_rank`、`local_world_size`、`num_nodes` 是多少？
   - 打开 [src/olmo_core/distributed/utils.py:L338-L353](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L338-L353)，确认 `NUM_NODES` 未设置时 `get_num_nodes()` = 4 // 2 = 2。
3. **需要观察的现象**：无（纯推导），但结论会在 4.4 的实践里被真实进程验证。
4. **预期结果**：节点 A 上进程的全局 rank 为 0/1、local rank 0/1；节点 B 上全局 rank 2/3、local rank 0/1；所有进程 `local_world_size=2`、`num_nodes=2`。共享文件系统时，fs local rank 依次为 0/1/2/3。
5. 若无双机环境，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_local_rank()` 读环境变量而 `get_rank()` 查进程组？两者能不能互换实现？

**答案**：`get_rank()` 语义是「在（可能新建的子）进程组内的编号」，只有进程组知道；`local_rank` 描述的是**物理布局**（节点内第几张卡），进程组本身不记录节点边界，只能由启动器（torchrun）通过 `LOCAL_RANK` 告知。不能互换：进程组查不到节点信息，环境变量也没有子组概念（见 [utils.py:L269-L282](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L269-L282) 的 warning）。

**练习 2**：`get_world_size()` 不传参数时返回的是什么？传了 `group` 呢？

**答案**：不传时是默认进程组（torchrun 建立的全体进程）的大小；传 `group` 时是该子组的大小。非分布式下都返回 1（[utils.py:L309-L319](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L309-L319)）。u7-l2 讲过的 TP/CP 子组查规模就是靠传 group。

**练习 3**：`get_num_nodes()` 在哪些情况下会返回错误结果？

**答案**：当 `NUM_NODES` 环境变量与实际不符、或既没有 `NUM_NODES` 也没有 `LOCAL_WORLD_SIZE` 时（后者会被 `validate_env_vars()` 直接拦下报错，见 [utils.py:L212-L215](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L212-L215)）；另外 world size 不能被 local world size 整除时，整除推算会失真。

### 4.3 分布式透明的通信封装与 autograd 原语

#### 4.3.1 概念说明

有了进程组，下一步是通信。`distributed/utils.py` 提供了一层**「非分布式自动退化」的集合通信封装**：`synchronize_value` / `all_reduce_value`（标量或张量同步）、`broadcast_object`（任意 Python 对象广播）、`all_gather` / `all_gather_object`（收集）、`do_n_at_a_time`（限流执行）。它们与 4.2 的查询函数共享同一设计哲学——单进程下返回值就是输入本身，业务代码零分支。

两个补充工具：

- `distributed/nn.py` 的 `all_to_all_single` / `all_to_all` 是 **autograd 兼容**的 all-to-all：区别于 `dist.all_to_all_single`（没有梯度），它们走 `torch.distributed.nn.functional`，反向传播会自动插入对应的逆向通信（all-to-all 的逆还是 all-to-all）。序列并行 / 上下文并行的 Ulysses 变换就依赖这类原语。
- `get_local_tensor` / `get_full_tensor` / `distribute_like` 处理 DTensor（u7-l1/u7-l2 讲过的分片张量）与普通张量的互转，让「单机视角代码」和「分片视角代码」可以复用。

#### 4.3.2 核心流程

**all_gather（异形张量版）** 的两段式技巧：

```
输入：本 rank 的张量 t（各 rank 形状可以不同！）
  1. shapes = all_gather_object(t.shape)     # 先用对象级通信同步形状
  2. 按各 rank 的 shape 分配 output 列表
  3. dist.all_gather(output_list, t)         # 再做真正的张量收集
输出：List[Tensor]，第 i 个元素来自 rank i
```

PyTorch 原生 `dist.all_gather` 要求预先分配输出张量，而形状在各 rank 间不一致时无法预先分配——所以先用（基于 pickle 的）`all_gather_object` 交换形状元信息。

**do_n_at_a_time（限流执行）** 的分组轮转：把 \(W\) 个 rank 按「同时至多 \(n\) 个」分批执行函数 `f`，批数为

\[
\text{group\_count} = \left\lceil \frac{W}{n} \right\rceil, \qquad
\text{group\_rank} = \text{rank} \bmod \text{group\_count}
\]

第 `i` 批里 `group_rank == i` 的那些 rank 执行 `f`，然后全体 `barrier()`，进入下一批。默认 `n = num_nodes`，即「每节点同时只有一个 rank 在干活」——典型用途是每节点下载/构建一份数据缓存，避免网络与磁盘被重复打爆。

#### 4.3.3 源码精读

标量同步与归约：

[src/olmo_core/distributed/utils.py:L359-L403](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L359-L403)

`synchronize_value` 把 bool/int/float/张量统一搬到指定 device 后 `broadcast`（默认 src=0）；`all_reduce_value` 同样搬运后做 `all_reduce`（默认 SUM）。非张量输入会先 `torch.tensor(value)`、结果再 `.item()` 还原成 Python 标量。非分布式时原样返回。**u6-l2 提过 SkipStepOptimizer 要求 loss 在 train_batch 末尾即时 all-reduce——用的正是这类封装。**

对象广播与收集：

[src/olmo_core/distributed/utils.py:L409-L447](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L409-L447)

`broadcast_object` 用 pickle 序列化后从 `src`（默认 0）广播任意 Python 对象——配置、随机种子、元数据同步的常用手段；非分布式直接返回原对象。`all_gather` 就是上面流程图的两段式实现（L430 先 `all_gather_object` 拿形状，L431-L434 再按形状分配并做张量 all-gather）；`all_gather_object`（L438-L447）则是把 pickle 后的对象收集成列表。注意这三个函数都支持传 `group` 子组。

限流执行：

[src/olmo_core/distributed/utils.py:L534-L564](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L534-L564)

`do_n_at_a_time` 的实现就是分组公式的直译：`group_count = math.ceil(world_size / n)`，循环 `group_count` 次，命中批次的 rank 执行 `f()`，所有 rank 都要过 `barrier()`——**barrier 必须全体参与**，这正是它不能写成「只有干活的 rank 循环」的原因。docstring（L534-L553）说明了默认 `n=num_nodes` 的用意。

autograd 兼容的 all-to-all：

[src/olmo_core/distributed/nn.py:L5-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/nn.py#L5-L43)

`all_to_all_single` 包装 `torch.distributed.nn.functional.all_to_all_single`（注意模块名是 `torch.distributed.nn`，不是 `torch.distributed`）：先按 `output_split_sizes` 之和分配输出张量（未指定时输出与输入同形），输入做 `.contiguous()` 后交给底层。因为走的是 `dist_nn` 版本，**输出带梯度**，反向时会自动插入逆向的 all-to-all 把梯度送回来源 rank。支持变长切分（all-to-all-v）：`input_split_sizes` 描述「发给每个 rank 多少」、`output_split_sizes` 描述「从每个 rank 收多少」。同文件 L46-L67 的 `all_to_all` 是张量列表版本。

DTensor 互转工具：

[src/olmo_core/distributed/utils.py:L500-L531](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L500-L531)

`get_local_tensor` 取 DTensor 的本地分片（若拿到的是异步通信句柄 `AsyncCollectiveTensor`，调 `.wait()` 等它就绪——注释解释了这个细节）；`get_full_tensor` 反之聚合出完整张量；`distribute_like` 把普通张量按 `source` 的 mesh 与 placements 切成同样的分片布局。u3-l6 讲过初始化的 `_apply_init` + `distribute_like` 用它们保证 DTensor 分片与单机初始化一致；u6-l2 讲过梯度裁剪时 DTensor 范数要 `full_tensor()` 归约。

#### 4.3.4 代码实践（阅读 + 复算型）

1. **实践目标**：读懂真实测试如何同时验证 `all_to_all_single` 的前向与反向。
2. **操作步骤**：
   - 阅读 [src/test/distributed/nn_test.py:L10-L42](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/nn_test.py#L10-L42)（`_all_to_all_equal_split`）：
     - 每个 rank 构造形状 `(world_size, 2)`、值全为 `float(rank)` 的输入张量（`requires_grad=True`）；
     - all-to-all 之后第 `i` 行应来自 rank `i`，所以期望输出是 `[[0,0],[1,1]]`（world_size=2 时）；
     - 对 `output.sum()` 反向，梯度期望是全 1——因为 sum 对每个元素的梯度都是 1，而 all-to-all 的逆向通信会把梯度原路送回。
   - 再看它如何被挂到 pytest：[src/test/distributed/nn_test.py:L95-L99](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/nn_test.py#L95-L99)，用 `run_distributed_test(..., world_size=2, start_method="spawn")` 拉起。
3. **需要观察的现象**：断言里同时检查了前向数值与 `input_tensor.grad`——这就是「autograd 兼容」的可验证含义。
4. **预期结果**：你能向别人解释为什么梯度期望是 `ones_like(input)`。
5. 运行部分留到 4.4 一起做。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `all_gather` 要先用 `all_gather_object` 同步形状，而不能直接调 `dist.all_gather`？

**答案**：`dist.all_gather` 需要预先分配好输出张量列表，且默认假设各 rank 张量同形。OLMo-core 的封装允许各 rank 形状不同（如 varlen 序列），所以先用基于 pickle 的对象通信把形状收集过来，按各自形状分配输出缓冲再执行真正的张量收集（[utils.py:L421-L435](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L421-L435)）。

**练习 2**：`do_n_at_a_time(f, n=1, world_size=4, local_rank=3)` 中，`f` 会在第几轮被执行？

**答案**：group_count = ceil(4/1) = 4，group_rank = 3 % 4 = 3，所以在 `active_group == 3` 的第 4 轮（最后一轮）执行；此前它要在每轮末尾跟着全体一起 barrier 等待（[utils.py:L554-L564](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L554-L564)）。

**练习 3**：`dist.all_to_all_single`（torch.distributed）与 `dist_nn.all_to_all_single`（torch.distributed.nn.functional）的本质区别是什么？

**答案**：前者是纯通信原语，输出不记录计算图、无法反传；后者是 autograd function，反向时自动插入逆向 all-to-all，把输出梯度按 `output/input_split_sizes` 路由回各来源 rank 的输入梯度。OLMo-core 的 `all_to_all_single`（[nn.py:L5-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/nn.py#L5-L43)）包装的是后者，所以能直接用在序列并行的训练计算图里。

### 4.4 `run_distributed_test()`：pytest 里的多进程沙盒

#### 4.4.1 概念说明

分布式代码最尴尬的地方是「怎么测」——pytest 默认单进程，而 `WORLD_SIZE=2` 的逻辑必须真的有两个进程才能验证。OLMo-core 的答案是 `src/olmo_core/testing/distributed.py` 的 `run_distributed_test(func, ...)`：**在 pytest 进程内部，用 `torch.multiprocessing.start_processes` 拉起 `world_size` 个真实子进程，每个子进程各自完成 rendezvous、建立进程组，然后执行你传入的 `func`，结束后销毁进程组**。子进程里 `dist.get_rank()` 是真实的 0/1，集合通信走真实的 gloo/nccl 后端——只是都跑在本机。

配套的 `BACKENDS` 参数化常量（在 `testing/utils.py`）让同一测试自动覆盖 gloo（CPU 恒可跑）与 nccl（多 GPU 才跑）两种后端，后者通过 pytest mark 在无 GPU 环境自动跳过——这正是 u1-l2 讲过的跳过机制在分布式测试上的应用。全库有近 40 个测试文件使用它（从 `distributed/` 到 `nn/moe/`、`optim/`），它是 OLMo-core 分布式正确性的基石设施。

#### 4.4.2 核心流程

```
pytest 收集到 test_xxx(backend)（@parametrize("backend", BACKENDS)）
  └─ run_distributed_test(func, world_size=2, backend=..., ...)
       ├─ 1. 选 start_method：gloo → "fork"（省内存），否则 "spawn"（CUDA 不能 fork）
       ├─ 2. _find_open_port()：从预洗牌的 29500-30000 端口池里找一个空闲端口
       └─ 3. mp.start_processes(_init_process, nprocs=world_size)
            └─ 每个子进程执行 _init_process：
                 ├─ setdefault LOCAL_RANK / LOCAL_WORLD_SIZE / NUM_NODES
                 ├─ init_distributed(backend, timeout=120s,
                 │     init_method=f"tcp://{primary_addr}:{primary_port}",
                 │     world_size=..., rank=process_rank)      ← 用显式 tcp:// 而非环境变量 rendezvous
                 ├─ 配置带 [rank N] 前缀的日志（默认只开 rank0，log_from_all_ranks=True 全开）
                 ├─ func(*args, **kwargs)                     ← 你的测试逻辑，断言写在这里
                 └─ finally: dist.destroy_process_group()      ← 保证异常也清理
```

两个关键设计：

- **rendezvous 用显式 `tcp://ip:port`**：子进程不是 torchrun 拉起的，没有现成的 `MASTER_ADDR`，所以直接把 `init_method` 传给 `init_process_group`（PyTorch 支持环境变量与 tcp URL 两种会合方式）。
- **端口池防冲突**：模块加载时把 29500~30000 洗牌存入 deque（[testing/distributed.py:L23-L33](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L23-L33)），每次取一个，被占用则叠加再试，10 次失败报错——避免并行跑多个分布式测试时端口打架。

#### 4.4.3 源码精读

子进程入口 `_init_process`：

[src/olmo_core/testing/distributed.py:L60-L83](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L60-L83)

注意四个细节：`assert world_size > 1`（单进程没有测试价值，直接断言拦住）；环境变量用 `setdefault` 补齐（不覆盖外部已有值）；timeout 收紧到 120 秒（测试挂起时快速失败，而不是生产默认的 30 分钟）；`init_method=f"tcp://..."` 显式指定会合地址。

带 rank 的日志与清理：

[src/olmo_core/testing/distributed.py:L85-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L85-L108)

这段把 `dist.get_rank()` 注入每条日志记录的 `local_rank` 属性，输出形如 `[rank 1] 2026-...: INFO: ...`——多进程日志不再混在一起。`log_from_all_ranks` 默认 False：只有 rank 0 开 DEBUG 级日志，其余进程保持安静。最后 `try/finally` 保证无论断言是否失败都 `destroy_process_group()`，不泄漏进程组给下一个测试。

主入口 `run_distributed_test`：

[src/olmo_core/testing/distributed.py:L111-L147](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L111-L147)

签名暴露了所有旋钮：`world_size`（默认 2）、`backend`（默认 gloo，**CPU 就能跑**）、`start_method`（None 时按后端自动选：gloo→fork、其他→spawn；CUDA 进程不允许 fork，因为 CUDA 上下文不能跨 fork）、`func_args`/`func_kwargs`（传给被测函数的参数）、`primary_addr`/`primary_port`（默认本机 + 自动找端口）。最终 `mp.start_processes` 同时拉起 nprocs 个 `_init_process`。

BACKENDS 参数化：

[src/olmo_core/testing/utils.py:L350-L357](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/utils.py#L350-L357)

`BACKENDS` 只有两项：gloo（无标记，任何环境都跑）和 `cuda:nccl,cpu:gloo`（挂 `MULTI_GPU_MARKS`）。后者定义在 [testing/utils.py:L115-L118](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/utils.py#L115-L118)：`pytest.mark.gpu` + `skipif(not has_multiple_gpus)`。所以在纯 CPU 机器上，nccl 参数自动 skip，gloo 参数照常跑——**这就是「无 GPU 也能写分布式测试」的全部秘密**。

最小真实用例：

[src/test/distributed/utils_test.py:L10-L21](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/utils_test.py#L10-L21)

`broadcast_object` 是被测逻辑（注意它没有 test_ 前缀、不直接被收集）：rank 0 持有 `("abc", "def")`、rank 1 持有不同的 `("abc", "abc")`，广播后断言两个 rank 都拿到 rank 0 的值。外层 `test_broadcast_object` 加 `@parametrize("backend", BACKENDS)` 后把它交给 `run_distributed_test`。同文件 [L24-L38](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/utils_test.py#L24-L38) 的 `test_do_n_at_a_time` 则示范了另一种用法：不拉真进程，直接以不同 `world_size`/`local_rank` 参数模拟调用 `do_n_at_a_time`——适合验证纯逻辑。

#### 4.4.4 代码实践（本讲主实践：双进程 all-gather 测试）

1. **实践目标**：亲手用 `run_distributed_test` 写一个双进程测试——每个进程各持一半数据，all-gather 后验证合并结果完整无缺，并用 pytest 真实跑通。

2. **操作步骤**：

   - 在仓库**外**建一个练习目录（比如 `~/dist-lab/`），新建文件 `my_all_gather_test.py`（示例代码）：

     ```python
     # my_all_gather_test.py（示例代码）
     import pytest
     import torch
     import torch.distributed as dist

     from olmo_core.distributed.utils import all_gather, get_rank, get_world_size
     from olmo_core.testing import BACKENDS, run_distributed_test


     def gather_halves():
         """每个 rank 各持一半数据，all-gather 后合并验证。运行在子进程内。"""
         rank = get_rank()
         world_size = get_world_size()
         assert world_size == 2

         # rank r 持有 [3r, 3r+1, 3r+2]，两个 rank 合起来正好是 0..5
         half = torch.tensor([3.0 * rank, 3.0 * rank + 1, 3.0 * rank + 2])

         pieces = all_gather(half)          # List[Tensor]，第 i 个来自 rank i
         assert len(pieces) == world_size
         assert pieces[rank].equal(half)     # 自己那份原样到达

         merged = torch.cat(pieces)          # [0,1,2,3,4,5]
         expected = torch.arange(6, dtype=torch.float32)
         assert torch.equal(merged, expected), f"rank {rank}: {merged}"


     @pytest.mark.parametrize("backend", BACKENDS)
     def test_gather_halves(backend: str):
         run_distributed_test(gather_halves, backend=backend, world_size=2)
     ```

   - 运行：`python -m pytest -v ~/dist-lab/my_all_gather_test.py`（显式指定文件路径可绕过本仓库 pyproject 里 `testpaths = "src/test/"` 的收集范围；文件名以 `_test.py` 结尾即可被默认模式收集）。

3. **需要观察的现象**：

   - 输出里出现两条用例：`backend=GLOO` 与 `backend=NCCL`；
   - CPU 机器上 `backend=NCCL` 显示 `SKIPPED`（Requires multiple GPUs），`backend=GLOO` 显示 `PASSED`；
   - 若某个 rank 的断言失败，日志会带 `[rank N]` 前缀指出是哪个进程失败。

4. **预期结果**：`backend=GLOO PASSED`。测试内部真实拉起了 2 个子进程、通过 tcp rendezvous 建立了 gloo 进程组、完成了一次 all-gather 并各自销毁进程组。

5. **验证你理解的三个延伸实验**（待本地验证）：
   - 把 `world_size=2` 改成 `world_size=3`，同时让每个 rank 持 2 个元素、期望值改为 `torch.arange(6)`——确认只需改数据切分逻辑，测试框架本身不动；
   - 在 `gather_halves` 开头加一行 `print(f"hello from rank {get_rank()}")`，再给 `run_distributed_test` 传 `log_from_all_ranks=True`，观察两个进程的输出交错；
   - 故意把断言写错（如期望 `torch.arange(5)`），确认 pytest 报错且进程组仍被 `finally` 正常销毁（不会影响下一个用例）。

   运行结果与环境相关，若现象不同请标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`run_distributed_test` 默认 backend 是 gloo 且 start_method 自动选 fork。为什么 nccl 后端必须用 spawn？

**答案**：fork 出的子进程会继承父进程的整个内存快照，而 CUDA 上下文不能安全地跨越 fork（CUDA 运行时不支持多进程 fork 语义），所以涉及 CUDA 的 nccl 后端必须用 spawn 重新启动干净的 Python 解释器。[testing/distributed.py:L125-L126](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L125-L126) 的 `start_method = "fork" if backend == "gloo" else "spawn"` 正是这条规则；[nn_test.py:L95-L99](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/distributed/nn_test.py#L95-L99) 甚至对 gloo 也显式用了 spawn。

**练习 2**：被测函数 `gather_halves` 里的 `assert` 失败时，pytest 如何报告？进程组会泄漏吗？

**答案**：断言在子进程内抛 `AssertionError`，`mp.start_processes` 会把子进程异常传播回 pytest 主进程报告为测试失败；`_init_process` 用 `try/finally` 包住被测函数（[testing/distributed.py:L105-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L105-L108)），失败路径也保证 `dist.destroy_process_group()`，不会泄漏。

**练习 3**：为什么 `BACKENDS` 里 nccl 那一项写的是 `cuda:nccl,cpu:gloo` 而不是单纯的 `nccl`？

**答案**：复合后端让 CUDA 张量走 nccl、CPU 张量走 gloo。测试基础设施本身（以及日志、对象级通信）需要在 CPU 上做集合通信，纯 nccl 后端无法处理 CPU 张量；这也与生产训练的默认后端（[train/__init__.py:L88](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/__init__.py#L88) 的 `cpu:gloo,cuda:nccl`）保持一致，测试环境尽量贴近真实。

### 4.5 常见分布式初始化问题排查清单

（本节是 4.1/4.4 的综合运用，属于「distributed utils」最小模块的收尾。）

| 症状 | 根因 | 出处与解法 |
| --- | --- | --- |
| 直接 `python script.py` 报缺 `RANK`/`WORLD_SIZE` 相关错误 | 没走 torchrun | OLMo-core 已自动兜底：`_bootstrap_single_process_distributed_env()` 补齐单进程环境（[utils.py:L41-L69](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L41-L69)）；若仍报错，检查是否手动传了矛盾的 `rank`/`world_size` 参数 |
| `OLMoEnvironmentError: Missing env var 'LOCAL_RANK'` | torchrun 版本过旧或自定义启动器没注入 | `validate_env_vars()`（[L200-L201](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L200-L201)）的第一个检查，补齐启动器环境变量 |
| 多 rank 写同一文件互相覆盖 | 没区分 fs local rank | 用 `get_fs_local_rank()` 生成文件名后缀；共享文件系统设 `OLMO_SHARED_FS=1`（[L285-L306](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L285-L306)） |
| `init_distributed` 报 backend 不一致 | 外部已初始化过进程组（如第三方库） | 要么传入相同 backend，要么去掉预初始化（[L170-L178](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L170-L178)） |
| 所有进程都占着 GPU 0 | `set_device` 时机不对 | 库内已保证顺序（set_device 先于 init，[L163-L168](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L163-L168)）；自查代码是否在初始化前碰了 CUDA |
| rendezvous 端口被占 | 固定端口冲突 | 训练侧单进程兜底会随机找空闲端口（[L35-L38](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L35-L38)）；测试侧用 29500-30000 洗牌端口池（[testing/distributed.py:L49-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L49-L57)） |
| 集合通信挂住直到超时 | 个别 rank 提前退出或没参与（如条件分支里的 all-reduce） | 记住 barrier/集合通信必须**全体 rank 共同执行**；参考 `do_n_at_a_time` 里即使不干活的 rank 也要过 barrier（[L560-L564](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L560-L564)）；测试超时被收紧为 120s 以快速暴露（[testing/distributed.py:L77-L83](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L77-L83)） |
| 每个进程日志混成一团 | 多进程共用 stderr | 测试框架已注入 `[rank N]` 前缀（[testing/distributed.py:L85-L101](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/testing/distributed.py#L85-L101)）；生产侧用 `prepare_cli_environment(log_filter_type=...)` 控制哪些 rank 能发言 |

## 5. 综合实践

把 4.4 的单原语测试扩展成一个**迷你的「分布式数据并行 mini-demo」测试模块**，串联本讲全部知识点：

1. 在 `~/dist-lab/` 新建 `my_ddp_lab_test.py`（示例代码），包含三个被测函数与对应测试：

   ```python
   # my_ddp_lab_test.py（示例代码）
   import pytest
   import torch
   import torch.distributed as dist

   from olmo_core.distributed.utils import (
       all_gather,
       all_reduce_value,
       broadcast_object,
       get_rank,
       get_world_size,
   )
   from olmo_core.testing import BACKENDS, run_distributed_test


   def split_compute_reduce():
       """模拟数据并行的三步：分数据 → 各自算 → 归约全局结果。"""
       rank, world_size = get_rank(), get_world_size()
       # 1) 用 broadcast_object 同步「配置」（rank 0 说了算）
       scale = broadcast_object(10.0 if rank == 0 else -1.0)
       assert scale == 10.0                       # 所有 rank 拿到 rank 0 的值
       # 2) 每个 rank 各持一半数据，本地求和后乘 scale
       local = torch.arange(rank * 3, rank * 3 + 3).float() * scale
       local_sum = all_reduce_value(local.sum().item(), torch.device("cpu"))
       # 3) 0+...+5 = 15，乘 scale 后两个 rank 应看到同一个全局和
       assert local_sum == 15.0 * scale
       # 4) 再把各自的原始片段 all-gather 回来做最终校验
       merged = torch.cat(all_gather(local))
       assert torch.equal(merged, torch.arange(6).float() * scale)


   @pytest.mark.parametrize("backend", BACKENDS)
   def test_split_compute_reduce(backend: str):
       run_distributed_test(split_compute_reduce, backend=backend, world_size=2)
   ```

2. 跑 `python -m pytest -v ~/dist-lab/my_ddp_lab_test.py`，确认 gloo 通过、nccl 按环境通过或跳过。
3. 对照 u6-l2 的「loss 以 sum 归约再除以全批有效 token 数」：把第 2 步改成「不归约直接各自打印 local_sum」，观察两个进程打印的值不同（可设 `log_from_all_ranks=True`），体会**为什么全局 loss 必须跨 rank 归约**。
4. 写一段 100 字以内的笔记回答：`broadcast_object`、`all_reduce_value`、`all_gather` 分别对应数据并行训练中的哪一步（配置同步 / 梯度或损失归约 / 评估时收集各 rank 输出）？

预期结果：gloo 用例通过；你能用自己的话说出「rank 0 广播配置 → 各 rank 本地计算 → all-reduce 得全局量 → all-gather 复原全量数据」这条贯穿数据并行的通信骨架。运行现象因环境而异，不符处标注「待本地验证」。

## 6. 本讲小结

- `init_distributed()` 是 PyTorch 原生初始化的加固封装：NCCL 环境加固、单进程自动兜底（没有 torchrun 也能跑）、`set_device` 先于建组的时机契约、backend 一致性检查、`validate_env_vars()` 的带提示报错。
- rank 有三个层次——全局 rank（进程组）、local rank（节点内，`LOCAL_RANK`，决定用哪张卡）、fs local rank（文件系统内，由 `OLMO_SHARED_FS` 决定取全局还是本地 rank）；规模量有 world size / local world size / num_nodes 三兄弟，注意哪些查进程组、哪些读环境变量。
- `distributed/utils.py` 的集合通信封装全部「分布式透明」：非分布式下 `all_gather` 返回 `[tensor]`、`broadcast_object` 原样返回——业务代码零分支，这是全库指标归约、loss 同步等机制的底座。
- `distributed/nn.py` 提供反向可传的 all-to-all（走 `torch.distributed.nn.functional`），支持变长切分，是序列/上下文并行计算图的通信原语；`do_n_at_a_time` 用「分组轮转 + 全体 barrier」实现每节点一次的限流执行。
- `run_distributed_test()` 在 pytest 内拉起真实多进程（默认双进程 gloo、CPU 可跑），子进程用显式 `tcp://` rendezvous、带 `[rank N]` 日志、`finally` 销毁进程组；`BACKENDS` 参数化让 nccl 用例在无多 GPU 环境自动跳过。
- 排查分布式问题的第一现场：`validate_env_vars()` 的报错信息、`init_distributed` 的三 rank 摘要日志、以及「集合通信必须全体参与」这条铁律。

## 7. 下一步学习建议

- **单元八（u8-l1 评测框架）**：`EvaluatorCallback` 在各 rank 之间汇总评测指标时同样依赖本讲的 all-reduce/all-gather 封装，可以带着「归约语义」的视角去读。
- **u10-l6 测试体系与贡献流程**：本讲的 `run_distributed_test` 是其核心素材之一；该讲还会讲 `src/test/nn/ddp/helpers.py` 如何把重复的分布式测试整合为共享 helper 与参数化套件——是给你自己的分布式测试「降重」的范本。
- **延伸阅读源码**：`src/test/distributed/parallel/parallel_test.py` 与 `src/test/nn/parallel/` 下多个测试，观察真实并行测试如何组合 `run_distributed_test`、DeviceMesh 与 `BACKENDS`；以及 `src/olmo_core/utils.py` 的 `seed_all()`（[L209-L224](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L209-L224)），理解「每个子进程各自播种相同 seed」与分布式可复现的关系。
