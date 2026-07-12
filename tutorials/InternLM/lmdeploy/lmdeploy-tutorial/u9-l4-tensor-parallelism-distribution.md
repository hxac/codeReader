# 张量并行与分布式

## 1. 本讲目标

单卡装不下大模型、或者单卡吞吐不够时，必须把计算摊到多张卡上。LMDeploy 的 PyTorch 引擎支持三种并行维度：**张量并行（TP）**、**数据并行（DP）**、**专家并行（EP）**，以及它们在 DeepSeek 类模型上的混合形态 **DP_TP**。

本讲聚焦「这些并行度是怎么从用户配置一步步落到 GPU 上的真实通信的」。读完本讲你应当能够：

1. 说清 `PytorchEngineConfig(tp=2)` 里的 `tp/dp/ep/attn_tp_size/mlp_tp_size/moe_tp_size` 各自含义，以及 `DistConfig.__post_init__` 如何用它们解出 `world_size` 与各级 TP。
2. 看懂 `distributed.py` 如何用 `torch.distributed` 的 `new_group` 把全局进程切成 attn/mlp/moe/ep 四套通信子组，并用 `DistManager` 单例把它们挂成全局上下文。
3. 跟踪从 `build_executor` → 多进程 worker → `dist.init_process_group` → `DistContext.build` 的完整通信初始化链。
4. 理解一个线性层在 forward 时为什么有时要 `all_reduce`、有时不要——即 Megatron 式的「列并行不通信、行并行 all_reduce」规则。

承接：u3-l2 已把 `DistConfig` 介绍为「拓扑求解器」并点过 `TPMode` 枚举，本讲把求解过程与下游通信实现讲透；u5-l2 已提过 `LinearBase` 处理 TP 切分，本讲专讲其通信原语。

## 2. 前置知识

### 2.1 为什么单卡不够

一个 \(N\) 亿参数模型以 fp16 存储就要 \(2N\) 字节权重，70B 模型约 140GB，单张 80G 卡装不下；即便装得下，KV cache、激活也要占显存。于是需要**把模型切开分到多卡**。

### 2.2 三种切法

| 并行方式 | 切什么 | 多卡是否通信 | LMDeploy 字段 |
|---------|--------|------------|--------------|
| 张量并行 TP | 切**同一层**的权重矩阵 | 是（每层 all_reduce） | `tp` / `attn_tp_size` / `mlp_tp_size` / `moe_tp_size` |
| 数据并行 DP | 切**请求**，每卡跑完整模型的不同请求 | 否（独立副本） | `dp` |
| 专家并行 EP | 切 **MoE 的专家**到不同卡 | 是（all-to-all） | `ep` |

### 2.3 张量并行的列并行 / 行并行

对一个线性层 \(y = xW\)（\(x\in\mathbb{R}^{1\times in}, W\in\mathbb{R}^{in\times out}\)），Megatron-LM 定义两种切法：

- **列并行（column-parallel）**：沿输出维度把 \(W\) 切成 \([W_1, W_2, \dots, W_n]\)，每卡持有一段，输入 \(x\) 复制，每卡算出 \(y\) 的一段 \(y_i = xW_i\)。这一层**不需要通信**，输出天然是分片的。
- **行并行（row-parallel）**：沿输入维度把 \(W\) 切成行块，输入 \(x\) 也对应切片，每卡算出部分和 \(y_i = x_i W_i\)，最后必须 **all_reduce** 求和：\(y = \sum_i y_i\)。

Attention 里 QKV 投影是列并行（每卡分到一部分头），输出投影 `wo` 是行并行（要 all_reduce）；MLP 里 `gate_up` 列并行、`down` 行并行。这条「列并行→行并行→all_reduce」的链路是本讲后半的核心。

### 2.4 进程组与 NCCL

PyTorch 的 `torch.distributed`（简称 `dist`）是多进程通信的标准库。几个关键概念：

- **rank**：进程在全局的编号（0 ~ world_size-1）。
- **world_size**：全局进程总数。
- **process group（进程组）**：从全局进程里挑一个子集做通信。`dist.new_group(ranks=[...])` 建组，`dist.all_reduce(t, group=g)` 只在该组内归约。
- **NCCL（NVIDIA Collective Communications Library）**：GPU 间集合通信的后端（all_reduce/all_gather 等），是 cuda 设备的默认 CCL。CPU 侧则用 gloo 后端。

本讲的全部代码本质都在做一件事：**根据并行度，把 `world_size` 个进程切成若干个通信子组，并把对应的 `ProcessGroup` 对象塞给各层模块备用。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py) | 用户面 `PytorchEngineConfig`，定义 `tp/dp/ep` 等字段 |
| [lmdeploy/pytorch/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) | 引擎内部 `DistConfig` 数据类：并行度求解器、`TPMode` 枚举 |
| [lmdeploy/pytorch/engine/config_builder.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py) | `build_dist_config`：把用户配置翻译成 `DistConfig` |
| [lmdeploy/pytorch/engine/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | `Engine.__init__` 取出 `tp/dp/dp_rank`、构造 executor |
| [lmdeploy/pytorch/engine/executor/__init__.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/__init__.py) | `build_executor` + `get_distributed_executor_backend`：选 uni/mp/ray 执行器 |
| [lmdeploy/pytorch/engine/executor/dist_utils.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/dist_utils.py) | `init_process_group`：真正的 `dist.init_process_group` 调用 |
| [lmdeploy/pytorch/engine/executor/base_worker.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/base_worker.py) | worker 包装：`init_process_group` → `DistContext.build` |
| [lmdeploy/pytorch/engine/executor/mp_executor.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/mp_executor.py) | 多进程执行器：`torch.cuda.set_device(proc_id)` 绑卡 |
| [lmdeploy/pytorch/distributed.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py) | **核心**：`DistContext`/`DistGroup`/`DistManager` 与各类 `get_*`/`all_reduce` 封装 |
| [lmdeploy/pytorch/nn/linear/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py) | `LinearBase`：列/行并行 + `all_reduce` 的实际发生地 |
| [lmdeploy/pytorch/nn/linear/utils.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/utils.py) | `update_tp_args`：列并行不通信、行并行才通信的规则 |

## 4. 核心概念与源码讲解

### 4.1 DistConfig：并行度拓扑的求解器

#### 4.1.1 概念说明

用户在 `PytorchEngineConfig` 里只填几个粗粒度旋钮（`tp/dp/ep`，以及 `dp>1` 时才生效的 `attn_tp_size/mlp_tp_size/moe_tp_size`）。但引擎真正运行时需要的是**一套自洽的拓扑**：全局有多少进程（`world_size`）、attention 用多大的 TP、MLP/MoE 又各用多大的 TP、MLP 的 TP 模式是普通还是 DP_TP。`DistConfig` 这个 dataclass 的职责就是在 `__post_init__` 里把这些派生量从粗粒度旋钮**求解**出来，并在求解过程中用 `assert` 暴露一切非法组合（比如 `mlp_tp` 不能整除 `attn_tp`）。

#### 4.1.2 核心流程

求解流程可概括为五步（对应 `__post_init__` 里的代码顺序）：

1. `dp==1` 时把 `mlp_tp/attn_tp/moe_tp` 全清成 `None`（单数据副本下没有「分层 TP」的概念）。
2. 回填 `mlp_tp`/`moe_tp`：`mlp_tp = mlp_tp or tp`；`moe_tp = moe_tp or (1 if ep>1 else mlp_tp)`——开了 EP 就让 MoE 走 EP 而非 TP。
3. 推导 `world_size`：`ep>1` 时等于 `ep`，否则等于 `max(mlp_tp, moe_tp)`。注意**world_size 由最大的 TP 需求决定**。
4. 回填 `attn_tp` 与 `tp`：`attn_tp = attn_tp or world_size // dp`，并令 `tp = attn_tp`。
5. 推导 `mlp_tp_mode`/`moe_tp_mode`：当某层 TP 等于 1 或等于 `attn_tp` 时是 `DEFAULT`，否则是 `DP_TP`。

#### 4.1.3 源码精读

`DistConfig` 是一个 `@dataclass`，字段分三组：并行度、分层 TP、TP 模式：

[lmdeploy/pytorch/config.py:157-L174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L157-L174) —— `DistConfig` 字段定义：`dp/ep/world_size` 是全局量，`tp` 注释写明「default tp, equal to attn_tp」，`attn_tp/mlp_tp/moe_tp` 是三种层各自的 TP。

求解器本体在 `__post_init__`，最关键的两段是 world_size 推导与整除性校验：

[lmdeploy/pytorch/config.py:195-203](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L195-L203) —— 这里先用 `world_size = ep if ep>1 else max(mlp_tp, moe_tp)` 定下进程数，再用四个 `assert` 强制 `world_size` 必须被 `dp`、`ep`、`mlp_tp`、`moe_tp` 整除。任何不合理的配置（例如 `tp=3` 配一个 4 卡机器）都会在这里直接抛错，而不是跑到一半才挂。

TP 模式由一行三元式决定：

[lmdeploy/pytorch/config.py:217-L219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L217-L219) —— `mlp_tp` 若等于 1 或 `attn_tp`，就是普通 `TPMode.DEFAULT`；否则说明 MLP 切得比 attention 更细，进入 `TPMode.DP_TP`（DeepSeek 类模型典型形态）。

`TPMode` 枚举只有两个值，是接口树与实现树共同词汇（u5-l4 已述）：

[lmdeploy/pytorch/config.py:151-L154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L151-L154) —— `DEFAULT`（普通全 TP）与 `DP_TP`（attention 走小 TP、MLP/MoE 走大 TP 的混合）。

用户面配置到 `DistConfig` 的翻译是 `from_engine_config`，字段一一对应：

[lmdeploy/pytorch/config.py:235-L249](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L235-L249) —— 注意 `engine_config.attn_tp_size` → `attn_tp`、`mlp_tp_size` → `mlp_tp`，名字在跨层时由「size」改成「tp」。

各层模块查询自己该用多大 TP、什么模式时，统一走 `get_tp_by_layer`：

[lmdeploy/pytorch/config.py:221-L233](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L221-L233) —— 传入 `'attn'/'mlp'/'moe'`，返回 `(tp_size, tp_mode)`；attention 恒为 `DEFAULT`，MLP/MoE 才可能返回 `DP_TP`。

#### 4.1.4 代码实践

1. **目标**：亲手验证 `DistConfig` 的求解逻辑，理解 `world_size` 由谁决定。
2. **操作步骤**：在仓库根目录执行下面的脚本（纯 Python，无需 GPU）。
3. **示例代码**（非项目原有代码，标注为示例）：

```python
# 示例代码：验证 DistConfig 求解
from lmdeploy.pytorch.config import DistConfig, TPMode

# 场景 A：普通 TP=2，单数据副本
c = DistConfig(tp=2)
print('A:', c.world_size, c.attn_tp, c.mlp_tp, c.mlp_tp_mode)
# 预期：world_size=2, attn_tp=2, mlp_tp=2, DEFAULT

# 场景 B：DP=2，attention 各自单卡，MLP 跨 2 卡 TP（DeepSeek 风格）
c = DistConfig(dp=2, tp=1, attn_tp_size=1, mlp_tp_size=2)
print('B:', c.world_size, c.attn_tp, c.mlp_tp, c.mlp_tp_mode)
# 预期：world_size=2, attn_tp=1, mlp_tp=2, DP_TP

# 场景 C：EP=4，MoE 专家并行
c = DistConfig(tp=1, ep=4)
print('C:', c.world_size, c.attn_tp, c.moe_tp)
# 预期：world_size=4, attn_tp=1, moe_tp=1（EP 时 MoE 走 EP 而非 TP）
```

4. **预期结果**：三组打印都符合注释里的「预期」。若把场景 A 改成 `DistConfig(tp=3)` 后再传给真实引擎，会在引擎侧因头数无法整除而报错——但 `DistConfig` 本身不校验头数，只校验整除性。
5. **待本地验证**：上述断言依赖当前源码版本，请在本地实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：用户写 `PytorchEngineConfig(tp=4)`，`DistConfig` 解出的 `world_size`、`attn_tp`、`mlp_tp` 各是多少？`mlp_tp_mode` 是什么？

**答案**：`dp==1` 时 `mlp_tp/attn_tp` 先清 None 再回填为 `tp`，故 `world_size=max(4,4)=4`、`attn_tp=4`、`mlp_tp=4`、`mlp_tp_mode=DEFAULT`（因 `mlp_tp==attn_tp`）。

**练习 2**：为什么场景 C（`ep=4`）里 `moe_tp` 是 1 而不是 4？

**答案**：`__post_init__` 中 `moe_tp = moe_tp or (1 if ep>1 else mlp_tp)`——开了 EP，MoE 的并行由 EP（专家分发到各卡）承担，就不再做 TP，避免专家内部再切。

### 4.2 distributed.py：进程组构造与全局上下文

#### 4.2.1 概念说明

`DistConfig` 只是「纸面拓扑」——一堆整数。要把它们变成能真正通信的对象，需要为每一类通信需求建一个 `torch.distributed.ProcessGroup`。`distributed.py` 就是这套「从整数拓扑到 ProcessGroup 对象」的工厂。它的设计有三个要点：

1. **每个进程组都建一对**：`gpu_group`（NCCL，跑张量）和 `cpu_group`（gloo，跑 Python 对象的 all_gather_object 等）。
2. **按层类型分别建组**：attention、MLP、MoE 各有自己的 TP 组，能复用就复用。
3. **用单例 `DistManager` + 全局 `DefaultContext`**：让任意层模块随时能查到「我属于哪个组」。

#### 4.2.2 核心流程

`DistContext.build()` 是总入口，顺序如下：

1. 若 `world_size==1` 直接返回默认单进程上下文（不初始化任何组）。
2. 断言 `dist.is_initialized()`（进程组必须在 `torch.distributed` 已初始化后才能建）。
3. 建一个覆盖全部 rank 的 cpu_group。
4. 调 `_build_tp_group` 依次建 attn/mlp/moe 三套 TP 组。
5. 调 `_build_ep_group` 建 EP 组（`ep>1` 时）。

其中 `_build_tp_group_impl` 是真正的「切组」函数：对每一个 `[start, start+tp)` 的 rank 窗口调 `dist.new_group` 各建一个 gpu/cpu 组，当前进程按 `tp_group_id = rank // tp` 取属于自己的那一个。这正是 Megatron TP 的拓扑：同一张量并行组内的进程 rank 连续。

#### 4.2.3 源码精读

`DistGroup` 是「一束进程组」的容器，把 gpu/cpu/各级组收拢：

[lmdeploy/pytorch/distributed.py:14-L36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L14-L36) —— 持有 `cpu_group/gpu_group`（单组）与 `cpu_groups/gpu_groups`（全部组列表，供 close 时统一销毁），外加 `gpu_gather_group`（DP_TP 模式下跨 attn_tp 维度的 gather 组）。

切组的核心实现：

[lmdeploy/pytorch/distributed.py:38-L67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L38-L67) —— 关键三行：`tp_rank = rank % tp`（组内编号）、`tp_group_id = rank // tp`（第几个 TP 组），然后 `for start in range(0, world_size, tp)` 对每个连续窗口 `dist.new_group(ranks=tp_ranks, backend=ccl_backend)`。同一窗口的进程拿到同一个 group 对象，不同窗口拿到不同对象——这就是 TP 组的物理构造。

MLP/MoE 组有「能复用就复用」的优化：

[lmdeploy/pytorch/distributed.py:125-L128](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L125-L128) —— 若 `mlp_tp == attn_tp`，直接把 `mlp_tp_group` 指向已建好的 `attn_tp_group`，避免重复建组。MoE 组同理还会再检查能否复用 mlp 组。

总入口 `DistContext.build`：

[lmdeploy/pytorch/distributed.py:226-L258](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L226-L258) —— 注意超时设成 `timedelta(days=35600)`（近乎无限），`world_size==1` 提前返回，`world_size>1` 时 `assert dist.is_initialized()` 保证调用顺序正确；最后 `_build_tp_group` + `_build_ep_group` 把四套组挂到 context 上。

模块层最常用的查询函数：

[lmdeploy/pytorch/distributed.py:305-L316](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L305-L316) —— `get_tp_world_rank(layer_type)` 按层类型返回 `(tp_size, rank)`，是线性层/attention 知道「我被切成了几份、我是第几份」的标准入口。

#### 4.2.4 代码实践

1. **目标**：在不开多进程、不初始化 NCCL 的前提下，观察 `world_size==1` 的单进程上下文长什么样。
2. **操作步骤**：

```python
# 示例代码
from lmdeploy.pytorch.distributed import DistContext, DistManager, get_tp_world_rank
from lmdeploy.pytorch.config import DistConfig

ctx = DistContext.build(rank=0, dist_config=DistConfig())  # world_size==1
DistManager().set_context(ctx)
print('tp world/rank:', get_tp_world_rank('attn'))   # (1, 0)
print('attn_tp_group:', ctx.attn_tp_group)            # DistGroup(rank=0)
```

3. **预期结果**：因为 `world_size==1`，`build` 走提前返回分支，所有组都是默认的 `DistGroup(rank=0)`，不调用任何 `dist.new_group`。
4. **现象观察**：若强行构造 `DistConfig(tp=2)` 再 `DistContext.build`，会因 `dist.is_initialized()` 为 False 而在 `assert` 处抛错——证明「建组必须在初始化分布式之后」。

#### 4.2.5 小练习与答案

**练习 1**：8 张卡跑 `tp=2, dp=4`，会建几个 TP 的 gpu_group？当前 rank=3 属于第几个？

**答案**：`world_size = max(mlp_tp, moe_tp)`，`dp==1` 才清 None，这里 `dp=4` 仍走普通分支。实际上 `dp>1` 时 `attn_tp = world_size//dp`。以 `tp=2,dp=4` 为例 `world_size=2`（`max(2,2)`）……注意：`dp` 在 DistConfig 层并不放大 world_size，DP 是「多个独立引擎副本」由上层编排（见 4.3）。真正建组数量 = `world_size // tp = 1` 个 TP 组，rank=3 不在该组的合法范围内——这说明 DP 副本的 rank 隔离在上层处理。结论：**TP 组的建立只看 `world_size` 与 `tp`，与 `dp` 数值无关**。

**练习 2**：`gpu_gather_group` 在什么模式下才非 None？

**答案**：仅 `TPMode.DP_TP` 且 `attn_tp != tp` 时才建（见 `_build_tp_group_impl` 第 63 行分支与第 71-77 行）。它是 DP_TP 模式下跨 attn_tp 维度做 gather 的专用组。

### 4.3 多进程启动与通信初始化

#### 4.3.1 概念说明

光有组对象还不够，得先有「多个进程」和「一个已初始化的 `torch.distributed`」。这一步由**执行器（executor）**完成。LMDeploy 提供三种执行器：

- `uni`：单进程单卡（`world_size==1`），最常用。
- `mp`：`multiprocessing` 多进程（同机多卡），将被弃用。
- `ray`：Ray 集群多进程，**`dp>1` 与 `empty_init` 强制要求 ray**。

执行器负责按 `world_size` 起进程、给每个进程绑一张卡（`torch.cuda.set_device`）、设好 `MASTER_ADDR/MASTER_PORT`、在每进程内调 `dist.init_process_group`，最后才调 `DistContext.build` 建子组。

#### 4.3.2 核心流程

```
Engine.__init__
  ├─ self.tp/dp/dp_rank = engine_config.*          # 取出并行度
  ├─ dist_config = ConfigBuilder.build_dist_config # PytorchEngineConfig → DistConfig
  └─ executor = build_executor(..., dist_config, distributed_executor_backend)
       ├─ get_distributed_executor_backend(...)    # 选 uni/mp/ray
       │     · world_size==1 → 'uni'
       │     · dp>1         → 'ray'
       │     · 否则按设备 support_ray() 决定 mp/ray
       └─ 启动 N 个 worker 进程
            每个 worker 内：
              init_backend(device_type)
              torch.cuda.set_device(proc_id)        # 绑卡
              worker.init_process_group(proc_id)
                ├─ init_process_group(rank, world_size)  # dist.init_process_group(nccl)
                └─ DistContext.build(rank, dist_config)   # 建 tp/ep 子组
```

#### 4.3.3 源码精读

`Engine.__init__` 取并行度并构造执行器：

[lmdeploy/pytorch/engine/engine.py:116-L119](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L116-L119) —— 把 `tp/dp/dp_rank` 存为引擎属性。注意这里只取了 `tp`，更细的 `attn_tp/mlp_tp` 已在 `DistConfig` 里。

[lmdeploy/pytorch/engine/engine.py:140-L165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L140-L165) —— `build_dist_config` 产出 `dist_config`，紧接着 `build_executor(...)` 把它连同 `distributed_executor_backend` 一起交给执行器，并 `executor.init()` 真正起进程。

执行器后端的选择逻辑（关键决策树）：

[lmdeploy/pytorch/engine/executor/__init__.py:11-L40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/__init__.py#L11-L40) —— 三条分支：环境变量 `LMDEPLOY_EXECUTOR_BACKEND` 最优先；`world_size==1` 必走 `uni`；`dp>1` 必走 `ray`；否则看设备是否 `support_ray()`。

[lmdeploy/pytorch/engine/executor/__init__.py:90-L98](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/__init__.py#L90-L98) —— 硬性约束：`dp>1` 和 `empty_init` 都强制要求 ray，否则 assert 失败。这是「DP 必须用 ray 编排」的根源。

真正的 `dist.init_process_group` 在 `dist_utils.py`，只有薄薄一层：

[lmdeploy/pytorch/engine/executor/dist_utils.py:38-L46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/dist_utils.py#L38-L46) —— 设 `RANK/WORLD_SIZE` 环境变量，调 `get_backend().ccl_backend()` 取后端名（cuda 设备返回 `'nccl'`），再 `dist.init_process_group(...)`。超时同样是 35600 天。

worker 包装把「初始化全局组」与「建子组」串起来：

[lmdeploy/pytorch/engine/executor/base_worker.py:59-L69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/base_worker.py#L59-L69) —— `world_size>1` 时先 `init_process_group`（建全局组），再无条件 `DistContext.build`（建 tp/ep 子组）。这是「通信初始化」的两个动作。

mp 执行器如何给每个进程绑一张卡：

[lmdeploy/pytorch/engine/executor/mp_executor.py:519-L541](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/mp_executor.py#L519-L541) —— `torch.cuda.set_device(proc_id)` 把第 `proc_id` 号进程绑到第 `proc_id` 号 GPU（进程号即卡号），随后 `worker.init_process_group(proc_id)`。这就是「多卡分配」的物理实现。

CCL 后端名由设备决定（cuda 恒为 nccl）：

[lmdeploy/pytorch/backends/default/op_backend.py:88-L90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/default/op_backend.py#L88-L90) —— 默认 `OpsBackend.ccl_backend()` 返回 `'nccl'`；昇腾等非 cuda 设备在自己的 `op_backend.py` 里覆写为 hccl 等。

#### 4.3.4 代码实践

1. **目标**：跟踪 `distributed_executor_backend` 的决策过程，回答「`tp=2` 默认会用哪个执行器」。
2. **操作步骤**：
   - 读 [lmdeploy/pytorch/engine/executor/__init__.py:55-L88](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/__init__.py#L55-L88)，确认 `build_executor` 何时调 `get_distributed_executor_backend`。
   - 在本地设 `LMDEPLOY_LOG_LEVEL=DEBUG` 后启动一个 `tp=2` 推理（见 4.4 综合实践），在日志里找 `Build <mp/ray> executor.` 与 `MASTER_ADDR=..., MASTER_PORT=...` 两行。
3. **预期结果**：`tp=2, dp=1` 在 cuda 设备上、`support_ray()` 为真时会选 `ray`（除非环境变量覆盖为 `mp`）；日志会打印执行器类型与主地址端口。
4. **待本地验证**：是否实际选 ray 取决于环境是否装了 ray，请在本地确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `world_size==1` 时根本不会调 `dist.init_process_group`？

**答案**：`base_worker.py:62` 有 `if self.world_size > 1:` 守卫；单进程下 `DistContext.build` 也走提前返回分支（`distributed.py:244`），不建任何组、不要求 `dist.is_initialized()`。

**练习 2**：`dp=2` 为什么被强制要求用 ray？

**答案**：DP 是「多个独立引擎副本」，需要跨进程/跨机的健壮编排与生命周期管理，ray 比 `multiprocessing` 更适合；代码在 `__init__.py:90-93` 用 assert 强制。

### 4.4 TP 通信：列并行 / 行并行与 all_reduce

#### 4.4.1 概念说明

前面三节都是「搭通信管道」，本节讲管道里**实际跑的数据**。一个被 patch 进来的线性层（u3-l4、u5-l2）在多卡下做 TP 推理时，通信就发生在 `LinearBase` 里。规则非常简单，由 `update_tp_args` 一行决定：

- **列并行（colwise=True）**：权重沿输出切，各卡输出天然分片，`all_reduce=False`，不通信。
- **行并行（colwise=False）**：权重沿输入切，各卡只得部分和，必须 `all_reduce=True`，在 forward 末尾做一次跨 TP 组的归约。

这条规则把「数学上要不要通信」和「代码里要不要调 `dist.all_reduce`」直接绑定，模块无需手写 if 分支。

#### 4.4.2 核心流程

`LinearBase.__init__` → `init_tp_args`：

1. `update_tp_args(is_tp, all_reduce, colwise)` 应用列/行规则。
2. 若 `is_tp`：从 `DistManager` 取当前 `DistConfig`，按 `layer_type` 查 `(tp, tp_mode)` 与 `tp_rank`，并取出 `dist_group.gpu_group` 存为 `self.tp_group`。
3. 若 `tp>1 且 tp_mode==DP_TP`：构造 `LinearForwardDPTP` 处理 DP-TP 分轮 GEMM（u5-l2 已述）。

forward 时：

- 普通模式：`_forward_default(x, self.all_reduce, None)`——子类算完矩阵乘后，若 `all_reduce` 为真就 `dist.all_reduce(out, group=self.tp_group)`。
- DP_TP 模式：走 `_forward_dp_tp`，先按需 `gather_by_tp_sizes`，再算，可能用 `reduce_scatter_by_tp_sizes` 替代 all_reduce。

#### 4.4.3 源码精读

列/行并行的判定规则：

[lmdeploy/pytorch/nn/linear/utils.py:20-L29](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/utils.py#L20-L29) —— `if not is_tp or colwise: all_reduce = False`。一句话：**只要不是列方向切（即行并行）且开了 TP，才需要 all_reduce**。这是 Megatron TP 通信量的全部来源。

`LinearBase` 如何把组对象挂上来：

[lmdeploy/pytorch/nn/linear/base.py:140-L161](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L140-L161) —— `get_tp_world_rank(layer_type)` 取 rank，`dist_cfg.get_tp_by_layer(layer_type)` 取 `(tp, tp_mode)`，`get_dist_group(layer_type).gpu_group` 取通信组，三者分别存为 `self.tp_rank/self.tp/self.tp_mode/self.tp_group`。`is_tp=False`（单卡）时全部置空。

forward 派发：

[lmdeploy/pytorch/nn/linear/base.py:219-L227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L219-L227) —— DP_TP 走 `_forward_dp_tp`；否则 `_forward_default(x, self.all_reduce, None)`。注意 `_forward_default` 是抽象方法，真正的「矩阵乘 + 条件 all_reduce」由 `AwqLinear/W8A8Linear/...` 子类实现（u5-l2）。

带 LoRA 时 all_reduce 的真实调用点：

[lmdeploy/pytorch/nn/linear/base.py:195-L199](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L195-L199) —— `if self.all_reduce:` 后，`DEFAULT` 模式调 `dist.all_reduce(out, group=self.tp_group)`，`DP_TP` 模式调 `reduce_scatter_by_tp_sizes`。这就是一次 TP 通信的完整发生。

`distributed.py` 把 `dist.all_reduce` 封装成可按字符串 `'tp'/'all'` 取组的便捷函数：

[lmdeploy/pytorch/distributed.py:378-L382](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/distributed.py#L378-L382) —— 传入字符串组名时自动 `get_group` 解析成 `ProcessGroup`，再委托 `dist.all_reduce`。MoE 的 all-to-all 等通信走 `backends` 的 `token_dispatcher`（u5-l3/u5-l4），不在此处。

#### 4.4.4 代码实践

1. **目标**：在源码层面验证「attention 的 wo 是行并行（all_reduce），qkv 是列并行（不通信）」。
2. **操作步骤**：
   - 打开 [lmdeploy/pytorch/models/llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py)，定位 `LlamaAttention` 里构造 `qkv_proj`（应见 `build_linear(..., colwise=True)` 或等价默认）与 `o_proj`（应见 `colwise=False`）的代码。
   - 对照 [lmdeploy/pytorch/nn/linear/utils.py:20-L29](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/utils.py#L20-L29) 解释：qkv 列并行 → `all_reduce=False`；o_proj 行并行 → `all_reduce=True`，故每层 attention 末尾有且仅有一次 all_reduce。
3. **预期结果**：能画出「qkv(列并行,无通信) → attention计算 → o_proj(行并行, all_reduce)」的单层通信图。
4. **待本地验证**：具体 `colwise` 参数在 llama.py 中的写法以本地源码为准。

#### 4.4.5 小练习与答案

**练习 1**：一个 6 层 transformer、`tp=2`，做一次 forward 总共发生几次 all_reduce？

**答案**：每层 attention 的 `o_proj` 一次、MLP 的 `down_proj` 一次，共 2 次/层 × 6 层 = 12 次（不计最后的 logits 收集）。QKV 与 gate_up 因列并行不通信。

**练习 2**：`LinearBase` 里 `self.tp_group` 来自哪个函数？它为何能不带参数「知道」自己属于哪个组？

**答案**：来自 `get_dist_group(layer_type).gpu_group`（base.py:153-154）。因为 `DistManager` 是全局单例，构造 worker 时 `DistContext.build` 已把当前进程的组挂进全局上下文，任何模块 `get_dist_manager().current_context()` 即可取到——这是「全局上下文 + 单例」模式的好处。

## 5. 综合实践

把本讲四节串起来，做一个**端到端调用链追踪**任务：从用户写 `PytorchEngineConfig(tp=2)` 开始，一路跟到「一次 all_reduce 实际发生在哪一行」。

**任务 A（无 GPU 也能做，源码阅读型）**：填写下面这张调用链表，每格填「文件:行号 + 一句话职责」。

| 步骤 | 触发位置 | 做了什么 |
|------|---------|---------|
| 1 用户配置 | `messages.py: PytorchEngineConfig(tp=2)` | 填入 tp 字段，`__post_init__` 校验 `tp>=1` |
| 2 翻译 | `config_builder.py: build_dist_config` | （自填） |
| 3 求解拓扑 | `config.py: DistConfig.__post_init__` | （自填：解出 world_size、attn_tp、mlp_tp_mode） |
| 4 选执行器 | `executor/__init__.py: get_distributed_executor_backend` | （自填：tp=2,dp=1 会选什么？） |
| 5 起进程绑卡 | `mp_executor.py: _main_loop` | （自填：哪一行 set_device？） |
| 6 初始化全局组 | `dist_utils.py: init_process_group` | （自填：backend 是什么？） |
| 7 建 TP 子组 | `distributed.py: _build_tp_group_impl` | （自填：tp_rank、tp_group_id 怎么算？） |
| 8 层挂组 | `linear/base.py: init_tp_args` | （自填：tp_group 从哪取？） |
| 9 实际通信 | `linear/base.py: _forward_lora/forward` | （自填：all_reduce 在哪一行？） |

**任务 B（有 2 张以上 GPU 时，运行型）**：

1. 写脚本：
   ```python
   from lmdeploy import pipeline, PytorchEngineConfig
   pipe = pipeline('Qwen/Qwen2.5-0.5B-Instruct', backend_config=PytorchEngineConfig(tp=2))
   print(pipe(['你好']))
   ```
2. 启动前开调试日志：`LMDEPLOY_LOG_LEVEL=DEBUG python demo.py 2>&1 | tee run.log`。
3. 在 `run.log` 里检索并记录：`Build <...> executor.`、`MASTER_ADDR=`、`MASTER_PORT=` 三类行。
4. 另开终端 `nvidia-smi -l 1`，观察推理期间是否有两张卡同时占用（对应 `torch.cuda.set_device(proc_id)` 的两张卡）。
5. **待本地验证**：实际执行器类型（ray/mp）、卡号分配、显存占用以本地环境为准；若 ray 未安装可设 `LMDEPLOY_EXECUTOR_BACKEND=mp` 强制 mp（注意 mp 将被弃用）。

**验收标准**：能口述「`tp=2` 时每个 TP 组有 2 个进程，rank 连续；每层 transformer 末尾的行并行线性层各做一次 all_reduce；DP 副本不共享组、由 ray 在上层编排」。

## 6. 本讲小结

- `DistConfig` 是「拓扑求解器」：用 `__post_init__` 从 `tp/dp/ep` 与分层 `attn_tp/mlp_tp/moe_tp` 解出 `world_size` 与各级 TP，并用一连串 `assert` 暴露非法组合；`world_size` 由 `max(mlp_tp, moe_tp)`（或 `ep`）决定。
- `distributed.py` 把整数拓扑变成 `ProcessGroup`：`_build_tp_group_impl` 按 `rank // tp` 分组、`rank % tp` 为组内 rank，每个组同时建 NCCL（gpu）与 gloo（cpu）两套，MLP/MoE 组能复用 attention 组就复用。
- 通信初始化链是 `Engine → build_executor → worker.init_process_group → dist.init_process_group(nccl) → DistContext.build`；`dp>1` 与 `empty_init` 强制走 ray，`world_size==1` 走 uni 且不建任何组。
- 多卡分配的物理实现是执行器在每个进程里 `torch.cuda.set_device(proc_id)`，进程号即卡号。
- TP 通信全部发生在 `LinearBase`：`update_tp_args` 规定「列并行不通信、行并行 all_reduce」，`o_proj`/`down_proj` 行并行故每层各一次 all_reduce，QKV/gate_up 列并行不通信。
- `DistManager` 单例 + 全局 `DistContext` 是任意模块无参数查到「我的组」的关键；`get_tp_world_rank`/`get_dist_group` 是标准查询入口。

## 7. 下一步学习建议

- **PD 分离（u9-l5）**：本讲的 `dp` 是「同模型多副本」，u9-l5 讲的 PD disaggregation 是「prefill 与 decode 拆成不同引擎」，二者都依赖多进程编排，但拓扑与通信对象完全不同，建议紧接着读。
- **MoE 与 EP 通信（u5-l3）**：本讲只讲了 TP 的 all_reduce；EP 的 all-to-all、token dispatch/combine 在 `backends` 的 `token_dispatcher` 里，是 MoE 多卡通信的另一套原语。
- **投机解码的分布式（u9-l2）**：草稿模型有自己的 `DistConfig`（`spec_decode/base.py` 用 `DistContext.build(rank=..., dist_config=draft_dist_config)`），可与本讲的「主模型建组」对照阅读。
- **TurboMind 的并行（u6）**：TurboMind 后端的 TP 在 C++ 侧实现（`builders/` 做 TP 权重切分），与本讲的 Python `torch.distributed` 路径完全不同，可对比两种后端对同一并行度的不同实现。
