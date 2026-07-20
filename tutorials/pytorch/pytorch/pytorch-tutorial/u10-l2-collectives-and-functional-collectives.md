# 集合通信与 functional collectives

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `all_reduce`、`broadcast`、`all_gather`、`reduce_scatter`、`all_to_all` 这几类集合通信原语的语义与典型用途；
- 区分 PyTorch 的两套集合通信编程模型：经典的 in-place `torch.distributed.*`（以下简称 `dist.*`）API 与新的「functional collectives」原生算子式 API；
- 理解 `ReduceOp` 枚举、`async_op` 异步语义，以及 functional collectives 为何「天然异步」；
- 看懂在 tracing（`make_fx`）/export 路径下，旧的 `dist.all_reduce` 等 in-place 集合通信如何被 `_remap_traceable_collective` 与 `_LegacyToFunctionalCollectiveMode` 重写为 functional 形式，从而避免把 `ProcessGroup` 这个不可序列化的 torchbind 常量「烤进」图里。

本讲承接 u10-l1（分布式初始化与 process group），把视角从「如何建连」推进到「建连之后进程之间如何交换张量」。

## 2. 前置知识

- **进程组（ProcessGroup）**：u10-l1 已讲过，`dist.init_process_group` 之后默认会有一个 `WORLD` 进程组，每个 rank 都有一个 0 到 `world_size-1` 的编号。集合通信就是在一组 rank 之间协调地搬动张量数据。
- **rank / world_size**：`rank` 是当前进程在组内的编号，`world_size` 是组内进程总数。
- **torch.ops 与 native 算子**：u2-l4 / u3-l1 讲过，`torch.ops.<namespace>.<op>` 是注册到 Dispatcher 的真实算子，可以被 tracing、可以被改写实现。functional collectives 的关键，就是把集合通信也做成这种「原生算子」。
- **`torch.overrides.TorchFunctionMode`**：Python 侧的 `__torch_function__` 协议允许一个上下文管理器全局拦截所有走 `torch` 函数分发的调用。本讲会用它做 tracing 时的改写。
- **`make_fx` / tracing**：用代理张量（proxy tensor）把一个 Python 函数记录成 FX 计算图（u7-l2 讲过 Dynamo 的追踪；`make_fx` 是更底层的 FX 追踪器）。

> 术语提示：本讲反复出现「in-place / 原地」「functional / 函数式」两个词。前者指直接修改输入张量（如 `dist.all_reduce(t)` 把结果写回 `t`）；后者指不修改输入、返回一个新张量（如 `all_reduce(t, ...)` 返回新值）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [torch/distributed/__init__.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/__init__.py) | `torch.distributed` 包入口；通过 `from .distributed_c10d import *` 把 `all_reduce`/`ReduceOp` 等经典 API 暴露出来。 |
| [torch/distributed/distributed_c10d.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/distributed_c10d.py) | 经典 in-place 集合通信的 Python 实现；`ReduceOp` 从 C++ 导入。 |
| [torch/distributed/_functional_collectives.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py) | 本讲主角。functional collectives 用户态 API、in-place 包装、reduce-op 枚举映射、以及 tracing 重写（`traceable_collective_remaps`、`_remap_traceable_collective`、`_LegacyToFunctionalCollectiveMode`）。 |
| [torch/fx/experimental/proxy_tensor.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/fx/experimental/proxy_tensor.py) | `make_fx` 追踪器；在 `compile_on_one_rank=True` 时启用 `_LegacyToFunctionalCollectiveMode`。 |
| [torch/_export/non_strict_utils.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_export/non_strict_utils.py) | 非 strict export 的 `_NonStrictTorchFunctionHandler`；同样调用 `_remap_traceable_collective`。 |
| [test/distributed/tensor/test_compile_on_one_rank.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/test/distributed/tensor/test_compile_on_one_rank.py) | 用 `fake` 后端在单进程下验证重写行为，是本讲可运行实践的依据。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 集合通信原语与经典 `dist.*` API；② functional collectives 原生算子式 API；③ `ReduceOp` 枚举、异步语义与 in-place 包装；④ tracing 下的重写（本次更新的核心）。

### 4.1 集合通信原语与经典 dist.\* API

#### 4.1.1 概念说明

集合通信（collective communication）是指**一组进程协同完成的一次张量交换/归约**。它和点对点通信（`send`/`recv`）相对：前者一条语句同时涉及组内所有 rank。分布式训练里最常见的几类：

| 原语 | 语义 | 典型用途 |
| --- | --- | --- |
| `broadcast` | 一个 rank 的张量发给所有人 | 参数初始化同步 |
| `all_reduce` | 所有人对同形状张量做逐元素归约（求和/取最大等），**每人**都拿到结果 | DDP 梯度同步 |
| `reduce` | 同上，但只有指定 rank 拿到结果 | 省带宽 |
| `all_gather` | 每人把自己的一份拼到一起，**每人**都拿到拼接结果 | 收集各 rank 的输出 |
| `reduce_scatter` | 先归约再分片：结果按维度切开后每人拿一块 | FSDP 梯度分片 |
| `all_to_all` | 每人把张量的不同块发给不同人 | 专家并行 |

经典的 `dist.*` API（u10-l1 之后你已经在用的那套）是 **in-place + 可选异步**：调用直接写回输入张量，返回一个 `Work` 句柄（`async_op=True`）或 `None`（同步）。

#### 4.1.2 核心流程

以 `dist.all_reduce` 为例：

1. 调用方准备输入张量 `t`（已有数据）与一个 `ProcessGroup`；
2. 调用 `dist.all_reduce(t, op=ReduceOp.SUM, group=pg)`；
3. 底层后端（NCCL/Gloo/`fake`）在组内协调，把归约结果**写回 `t`**；
4. 若 `async_op=False`，调用返回时结果已就绪（返回 `None`）；若 `async_op=True`，立即返回 `Work`，需稍后 `work.wait()`。

#### 4.1.3 源码精读

经典 `all_reduce` 的 Python 签名与文档在 distributed_c10d.py 中：

[distributed_c10d.py:3571-3591](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/distributed_c10d.py#L3571-L3591) —— 注意三点：第一参数叫 `tensor` 且是 in-place（文档明说 "operates in-place"）；`op` 默认 `ReduceOp.SUM`；返回 `Work` 或 `None`。

这些经典 API 是怎么挂到 `torch.distributed` 命名空间的？靠一行通配导入：

[__init__.py:172](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/__init__.py#L172) —— `from .distributed_c10d import *` 把 `all_reduce`、`all_gather` 等连同 `ReduceOp`（后者最终来自 `torch._C._distributed_c10d`，是 C++ 导出的枚举）一起放进 `torch.distributed`。

> 为什么要在意 `ReduceOp` 的来源？因为它是 **C++ torchbind 对象**，不是普通 Python 值——这一点在 4.4 讲「为什么不能直接烤进图」时是关键伏笔。

#### 4.1.4 代码实践

> 1. **实践目标**：用一个进程验证 `dist.all_reduce` 的 in-place 语义与 `fake` 后端可用性。
> 2. **操作步骤**：运行下面的脚本（`fake` 后端不需要真实多进程通信，单进程即可）。
> 3. **观察现象**：调用前后 `t` 的值被原地改写。
> 4. **预期结果**：对 `t = torch.arange(4.0)` 做 `ReduceOp.SUM` 的 all_reduce，输出为 `[0,1,2,3]`（fake 后端把所有 rank 视作同一份数据，求和即自身翻倍——具体数值以本地运行为准）。
> 5. 若无 `fake` 后端或 `is_available()` 为假，则「待本地验证」。

```python
# 示例代码：单进程下用 fake 后端体验 dist.all_reduce
import torch
import torch.distributed as dist
from torch.distributed import FakeStore

dist.init_process_group(backend="fake", store=FakeStore(), rank=0, world_size=2)
t = torch.arange(4.0)
print("before:", t)            # tensor([0., 1., 2., 3.])
ret = dist.all_reduce(t, op=dist.ReduceOp.SUM)
print("after :", t, "ret:", ret)  # t 被原地修改；ret 一般为 None
```

#### 4.1.5 小练习与答案

- **练习 1**：`dist.all_reduce` 与 `dist.reduce` 的返回结果分布有什么区别？
  - **答**：`all_reduce` 让**组内每个 rank** 都拿到归约结果；`reduce` 只让**指定 dst rank** 拿到，其余 rank 的输入张量不保证被更新。
- **练习 2**：为什么 DDP 同步梯度选 `all_reduce` 而不是 `reduce`？
  - **答**：每个 rank 反向之后都要用「全局平均梯度」继续更新自己的本地参数副本，因此**每个**rank 都需要完整结果，`all_reduce` 正好满足。

---

### 4.2 functional collectives 原生算子式 API

#### 4.2.1 概念说明

经典 `dist.*` API 有两个对编译器不友好的特性：(a) in-place（追踪器难以区分输入输出）；(b) 把 `ProcessGroup` 这个 torchbind 对象当参数（无法干净地序列化进 FX 图）。于是 PyTorch 设计了一套 **functional collectives**：

- **纯函数式**：不改输入，返回一个新张量；
- **原生算子下沉**：底层落到 `torch.ops._c10d_functional.*` 这些注册到 Dispatcher 的真实算子，可以被 `make_fx`/Dynamo/export 像普通算子一样追踪；
- **天然异步**：返回的是被 `AsyncCollectiveTensor` 包裹的张量，真正的 `Work::wait()` 被推迟到「该张量第一次被下游算子使用」时（eager 路径）或由编译器在图里插入 `wait_tensor` 算子（tracing 路径）。

文件顶部的模块 docstring 说得很直白：functional collectives 的设计目标就是「让编译器能追踪这些 op，然后用 plain-old-data schema 决定如何 lowering；eager 下则返回 `AsyncCollectiveTensor` 子类，自动延迟 `wait()`」。

#### 4.2.2 核心流程

functional `all_reduce` 的执行链路（文件顶部注释也给了这张图）：

```
all_reduce(tensor, reduceOp, group)
  |--> _resolve_group(group)            # 把 ProcessGroup/DeviceMesh 规范化
  |--> torch.ops._c10d_functional.all_reduce(self, reduceOp.lower(), group_name)
  |--> _maybe_wrap_tensor(tensor)       # tracing 下立即插 wait_tensor；eager 下套 AsyncCollectiveTensor
```

关键区别于经典 API：`reduceOp` 是**字符串**（`"sum"`/`"avg"`/...）而不是 `ReduceOp` 枚举；`group` 既可以是 `ProcessGroup`，也可以是 `List[int]`、`DeviceMesh`、`(DeviceMesh, int)` 等多种形式。

#### 4.2.3 源码精读

functional `broadcast` 与 `all_reduce` 的定义：

[_functional_collectives.py:147-160](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L147-L160) —— `broadcast(self, src, group, tag="")`：解析 group 后调用 `torch.ops._c10d_functional.broadcast`，再用 `_maybe_wrap_tensor` 包一层。

[_functional_collectives.py:163-184](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L163-L184) —— `all_reduce(self, reduceOp: str, group, tag="")`：注意 `reduceOp.lower()` 把字符串归一化成小写，再传给 `torch.ops._c10d_functional.all_reduce`。`group` 的可接受类型在 docstring 里列了五种。

「异步等待」对应的底层算子 `wait_tensor`：

[_functional_collectives.py:138-144](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L138-L144) —— `wait_tensor(tensor)` 调用 `torch.ops._c10d_functional.wait_tensor`，按设备语义阻塞（CPU）或同步流（CUDA）。

`all_gather` 的 functional 版本叫 `all_gather_single`（沿 `gather_dim` 拼接，目前只支持 `gather_dim=0`）：

[_functional_collectives.py:187-226](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L187-L226) —— 它调用 `torch.ops._c10d_functional.all_gather_into_tensor`，并对 `gather_dim != 0` 的情况用 `_maybe_view_chunk_cat` 做视图切分。

#### 4.2.4 代码实践

> 1. **实践目标**：在源码里对比 functional `all_reduce` 与经典 `dist.all_reduce` 的签名差异。
> 2. **操作步骤**：打开 [_functional_collectives.py:163](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L163) 与 [distributed_c10d.py:3571](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/distributed_c10d.py#L3571)，逐参数对比。
> 3. **观察现象**：两者参数顺序、归约算子的表示都不同。
> 4. **预期结果**：填出下表（答案见 4.2.5）。

| 维度 | 经典 `dist.all_reduce` | functional `all_reduce` |
| --- | --- | --- |
| 归约算子表示 | `ReduceOp` 枚举 | （?） |
| 是否 in-place | （?） | （?） |
| 返回值 | （?） | （?） |

#### 4.2.5 小练习与答案

- **练习 1**：functional `all_reduce` 为什么「天然异步」而不需要 `async_op` 参数？
  - **答**：它返回 `AsyncCollectiveTensor`（eager）或在图里插入 `wait_tensor`（tracing），同步时机由「下游第一次使用」或编译器决定，所以 API 表面不需要 `async_op` 开关。
- **练习 2**：把上面表格补全。
  - **答**：归约算子表示 = 小写字符串 `"sum"`；in-place = 否（functional 不可变）；返回值 = 一个新的（可能被包裹的）`Tensor`。

---

### 4.3 ReduceOp 枚举、异步语义与 in-place 包装

#### 4.3.1 概念说明

两套 API 的「归约算子」表示不同：经典用 C++ 枚举 `ReduceOp.SUM`，functional 用字符串 `"sum"`。要把经典调用桥接到 functional，就需要一张映射表，这就是 `REDUCE_OP_TO_STR`。同时，为了让「只认 in-place 语义」的旧代码在 tracing 下也能跑通，functional collectives 文件里提供了一组 `*_inplace` 包装：它们调用 functional 版本拿到结果，再 `copy_` 回原张量，从而**复刻经典 API 的返回 None 行为**。

#### 4.3.2 核心流程

`all_reduce_inplace(t, op, group, ...)` 的逻辑：

1. 校验 `async_op`（in-place 路径不支持异步，否则没法对齐「调用返回即完成」的语义）；
2. 默认 group 兜底为 `dist.group.WORLD`；
3. `tensor.copy_(all_reduce(tensor, op, group, tag))`——functional 拿结果，再写回输入。

#### 4.3.3 源码精读

枚举到字符串的映射：

[_functional_collectives.py:1719-1728](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L1719-L1728) —— `REDUCE_OP_TO_STR` 把 `dist.ReduceOp.SUM`/`AVG`/`PRODUCT`/`MIN`/`MAX`/`BAND`/`BOR`/`BXOR` 一一映射到字符串。4.4 的 `_remap_traceable_collective` 就要用到它。

in-place 包装示例：

[_functional_collectives.py:1731-1747](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L1731-L1747) —— `all_reduce_inplace`：拒绝 `async_op`，然后 `return tensor.copy_(all_reduce(tensor, op, group, tag))`，返回 `None`（`copy_` 返回的是 self，但语义上经典 API 同步调用返回 `None`；这里的返回值在 tracing 包装里被丢弃，见 4.4）。

> 类似的还有 `all_to_all_inplace`、`all_gather_inplace`、`isend_inplace` 等，模式一致。

#### 4.3.4 代码实践

> 1. **实践目标**：在源码层面验证「in-place 包装 = functional + copy_」。
> 2. **操作步骤**：阅读 [all_reduce_inplace:1731](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L1731)，再找到 `all_gather_inplace`，对比两者如何把 functional 结果写回。
> 3. **观察现象**：`all_gather_inplace` 因为要写回**一组**张量 `tensor_list`，所以用了循环 `dst.copy_(src)`，而不是单个 `copy_`。
> 4. **预期结果**：能用一句话说出「in-place 包装就是把 functional 的不可变结果，按经典 API 的形状 copy 回原位置」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `all_reduce_inplace` 要在 `async_op=True` 时直接抛错？
  - **答**：in-place 包装复刻的是经典 API「同步调用、返回 None、结果已就绪」的语义；异步模式下结果在调用返回时尚未就绪，无法既保证 in-place 写回又立即返回，故直接禁止。
- **练习 2**：`REDUCE_OP_TO_STR` 里为什么没有 `ReduceOp.BAND` 之外还要列 `BOR/BXOR`？
  - **答**：位运算有「与/或/异或」三种独立的归约语义，需要分别对应字符串 `"band"/"bor"/"bxor"`，下游 `_c10d_functional` 算子据此选择不同的归约 kernel。

---

### 4.4 tracing 下的重写：_remap_traceable_collective 与 _LegacyToFunctionalCollectiveMode

> 本模块是本次更新的重点（`baa92d5d7 → ca62582e`，提交 `c656085630c`）。新增了一个共享辅助函数 `_remap_traceable_collective` 与一个 `TorchFunctionMode` 子类 `_LegacyToFunctionalCollectiveMode`，让 `make_fx`（`compile_on_one_rank`）和非 strict export 两条追踪路径都能把旧 `dist.*` 自动改写成 functional 形式。

#### 4.4.1 概念说明

**问题**：经典 `dist.all_reduce` 在底层是 in-place 的 `c10d.allreduce_` 算子，它把 `ProcessGroup` 直接绑成参数。当 `make_fx` 把这种调用记录进 FX 图时，`ProcessGroup` 这个 torchbind C++ 对象会被**当作常量烤进 GraphModule**。一旦你想把图序列化（例如 `compile_on_one_rank` 让单 rank 编译出可在任意 rank 加载的图），`GraphPickler` 就会因为 `ProcessGroup` 不可序列化而失败。

**解法**：在追踪时把所有 legacy 集合通信**改写**成 functional 形式。functional 算子把 group 当作**算子参数**（一个字符串 group name，或在 `compile_on_one_rank` 下由 `mesh_get_process_group` 算子产出），于是 group 成为图里一条可序列化的数据流，而不是烤死的常量。

这一改写在三个地方各自实现过：Dynamo 的 `CollectiveFunctionRewriteVariable`、非 strict export 的 `_NonStrictTorchFunctionHandler`、以及 `make_fx` 的 `compile_on_one_rank` 路径。本次更新把共用逻辑抽成 `_remap_traceable_collective`，并在 `make_fx` 里用 `_LegacyToFunctionalCollectiveMode`（一个 `TorchFunctionMode`）来触发它。

#### 4.4.2 核心流程

改写流程（以 `dist.all_reduce(t, op=ReduceOp.MAX, group=mesh.get_group())` 在 `compile_on_one_rank=True` 下被 `make_fx` 追踪为例）：

1. `make_fx` 进入追踪上下文，检测到 `compile_on_one_rank` 为真，于是 `stack.enter_context(_LegacyToFunctionalCollectiveMode())`；
2. 用户函数里调用 `dist.all_reduce(...)` → 触发 `__torch_function__` 拦截；
3. `_LegacyToFunctionalCollectiveMode.__torch_function__` 调 `_remap_traceable_collective(func, args, kwargs)`；
4. `_remap_traceable_collective` 用 `inspect.signature(...).bind(...)` 归一化参数；若该算子属于归约类（`all_reduce` 等），把 `ReduceOp.MAX` 经 `REDUCE_OP_TO_STR` 转成字符串 `"max"`；
5. 返回 `(mapped_func, (), bound_kwargs)`，其中 `mapped_func` 是 `_remapped_allreduce`；
6. `_remapped_allreduce`（仅在 tracing 时允许）调用 `all_reduce_inplace(...)`，后者再调 functional `all_reduce` → 落到 `torch.ops._c10d_functional.all_reduce`，被 `make_fx` 记录成图节点；
7. 与此同时，`mesh.get_group()` 在 `compile_on_one_rank` 下走 `_resolve_group` 里的 `torch.ops._dtensor.mesh_get_process_group` 分支，产出一个**算子节点**而非常量。

最终图里出现的是 `_c10d_functional.all_reduce.default` 与 `mesh_get_process_group.default`，而**不再有** `c10d.allreduce_.default`，也就没有任何 `ProcessGroup` 常量被烤进图，序列化成功。

#### 4.4.3 源码精读

remap 表与各 `_remapped_*` 包装（这些在本次更新之前就存在，是 Dynamo 改写的目标）：

[_functional_collectives.py:1988-2001](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L1988-L2001) —— `traceable_collective_remaps` 把每个 legacy 函数（`legacy_allreduce`、`legacy_all_gather`、`legacy_reducescatter` 等，见 [1906-1919 行的 import 别名](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L1906-L1919)）映射到对应的 `_remapped_*` 包装。注释强调这些包装必须是模块级 `def`（不能用装饰器工厂生成的闭包），因为 Dynamo 的 guard source 解析要靠 `fn.__name__` 去模块里查属性。

**本次新增的共享改写函数**：

[_functional_collectives.py:2004-2030](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L2004-L2030) —— `_remap_traceable_collective(func, args, kwargs)`：
- 若 `func` 不在 `traceable_collective_remaps` 里，返回 `None`（表示「这个调用我不管，按原样执行」）；
- 否则用 `inspect.signature(func).bind(*args, **kwargs)` 把位置/关键字参数归一化成一个 `bound` 字典；
- 对归约类算子（`all_reduce` / `reduce_scatter_single` / `reduce_scatter_tensor` / `_reduce_scatter_base`），把 `bound["op"]` 从 `ReduceOp` 枚举翻译成字符串；
- 返回 `(mapped_func, (), bound)`——注意位置参数置空、全部用关键字传，这是为了稳定地走 `bind` 后的命名参数。

**本次新增的 `TorchFunctionMode`**：

[_functional_collectives.py:2033-2055](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py#L2033-L2055) —— `_LegacyToFunctionalCollectiveMode` 继承 `torch.overrides.TorchFunctionMode`。它的 `__torch_function__` 先调 `_remap_traceable_collective`：若返回非 `None`，就用映射后的函数与参数执行；否则 `func(*args, **kwargs)` 透传。docstring 点明了「in-place `c10d.*` 会把 ProcessGroup 绑成不可序列化的 torchbind 常量，而 functional collectives 把 group 当算子参数，于是 group 流进图里而不是被烤死」。

**调用方之一：`make_fx`**：

[proxy_tensor.py:3196-3204](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/fx/experimental/proxy_tensor.py#L3196-L3204) —— `make_fx` 的追踪上下文里，当 `torch.compiler.config.compile_on_one_rank` 为真且分布式可用时，懒导入并 `stack.enter_context(_LegacyToFunctionalCollectiveMode())`。

**调用方之二：非 strict export**：

[non_strict_utils.py:1222-1234](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_export/non_strict_utils.py#L1222-L1234) —— `_NonStrictTorchFunctionHandler._override` 同样调用 `_remap_traceable_collective`，命中即返回改写结果，从而让 export 也能把 legacy 集合通信转成 functional 形式。

**端到端测试（实践依据）**：

[test_compile_on_one_rank.py:480-500](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/test/distributed/tensor/test_compile_on_one_rank.py#L480-L500) —— `test_legacy_all_reduce_serializes_under_coor`：在 `compile_on_one_rank=True` 下用 `make_fx` 追踪一个含 `dist.all_reduce(t, op=ReduceOp.MAX, group=mesh.get_group())` 的函数，断言图里**有** `mesh_get_process_group` 与 `_c10d_functional.all_reduce`、**没有** `c10d.allreduce_`，且 `_baked_pg_constants(gm)` 为空，最后 `GraphPickler.dumps` 能成功序列化。

#### 4.4.4 代码实践

> 1. **实践目标**：在单进程下亲眼看到 `make_fx` 把 `dist.all_reduce` 改写成 functional 形式。
> 2. **操作步骤**：运行下面这段脚本（`fake` 后端 + `FakeStore`，单进程可跑；它就是测试 [test_compile_on_one_rank.py:480](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/test/distributed/tensor/test_compile_on_one_rank.py#L480) 的简化版）。
> 3. **观察现象**：打印出的图节点 target 里出现 `_c10d_functional.all_reduce` 与 `mesh_get_process_group`，而不出现 `c10d.allreduce_`。
> 4. **预期结果**：两个关键 target 都在；切换 `compile_on_one_rank=False` 重跑，会看到相反结果——出现 `c10d.allreduce_`。
> 5. 若本机 `torch.distributed.is_available()` 为假或无 `make_fx`，则「待本地验证」。

```python
# 示例代码：观察 make_fx 在 compile_on_one_rank 下改写 dist.all_reduce
import torch
import torch.distributed as dist
import torch.distributed.config as dist_config
from torch.distributed import FakeStore, init_device_mesh
from torch.fx.experimental.proxy_tensor import make_fx

def fn(t, mesh):
    t = t.clone()
    dist.all_reduce(t, op=dist.ReduceOp.MAX, group=mesh.get_group())
    return t + 1

dist.init_process_group(backend="fake", store=FakeStore(), rank=0, world_size=2)
mesh = init_device_mesh("cpu", (2,))

with dist_config.patch(compile_on_one_rank=True):
    gm = make_fx(fn, tracing_mode="fake")(torch.arange(4.0), mesh)

for n in gm.graph.nodes:
    print(n.target)
```

#### 4.4.5 小练习与答案

- **练习 1**：`_remap_traceable_collective` 为什么要用 `inspect.signature(func).bind(...)`，而不是直接透传 `args`/`kwargs`？
  - **答**：归一化成命名参数后，才能可靠地定位并改写 `op` 这个参数（把它从 `ReduceOp` 枚举翻译成字符串）；同时把结果统一成 `(func, (), kwargs)` 形式，让两条调用路径（`make_fx` 与 export）拿到一致的调用约定。
- **练习 2**：如果不开 `compile_on_one_rank`，`make_fx(fn)` 追踪 `dist.all_reduce` 会得到什么？为什么这通常是问题？
  - **答**：会直接记录 in-place 的 `c10d.allreduce_` 节点，并把 `ProcessGroup` 作为 torchbind 常量烤进 `GraphModule`；一旦尝试序列化（`GraphPickler.dumps`）就会失败——这正是 [_baked_pg_constants](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/test/distributed/tensor/test_compile_on_one_rank.py#L502-L507) 测试断言的「未开启时的坏行为」。
- **练习 3**：`_LegacyToFunctionalCollectiveMode` 和 Dynamo 的 `CollectiveFunctionRewriteVariable` 是什么关系？
  - **答**：两者做同一件事（把 legacy 集合通信改写成 functional 形式），只是挂载点不同——Dynamo 在字节码追踪层的 `VariableTracker` 里做，`_LegacyToFunctionalCollectiveMode` 在 `make_fx` 的 dispatch 层用 `TorchFunctionMode` 做；二者都复用 `traceable_collective_remaps`，本次更新又把共享的「参数归一化 + op 翻译」逻辑抽到了 `_remap_traceable_collective`。

## 5. 综合实践

把本讲四个模块串起来：用 `fake` 后端做一次「经典 → functional → tracing 改写」的完整观察。

1. **初始化**：`dist.init_process_group(backend="fake", store=FakeStore(), rank=0, world_size=2)`，并用 `init_device_mesh("cpu", (2,))` 建一个 1D mesh。
2. **经典路径**：直接调 `dist.all_reduce(t, op=dist.ReduceOp.SUM)`，打印前后 `t`，确认 in-place 行为（4.1.4）。
3. **functional 路径**：从 `torch.distributed._functional_collectives` 导入 `all_reduce`，用字符串算子 `all_reduce(t, "sum", group=mesh.get_group())` 调用，对比签名差异（4.2.4）。
4. **tracing 改写路径**：在 `dist_config.patch(compile_on_one_rank=True)` 下 `make_fx` 一个含 `dist.all_reduce` 的函数，打印图的节点 target，确认 `_c10d_functional.all_reduce` + `mesh_get_process_group` 出现、`c10d.allreduce_` 不出现（4.4.4）。
5. **总结**：写一段话，说明「为什么 functional 化能让 `ProcessGroup` 从烤死的常量变成图里的数据流」——把 `REDUCE_OP_TO_STR`、`all_reduce_inplace`、`_remap_traceable_collective`、`_LegacyToFunctionalCollectiveMode` 这四者的协作讲清楚。

## 6. 本讲小结

- 集合通信原语分两类语义：归约型（`all_reduce`/`reduce`/`reduce_scatter`）与交换型（`broadcast`/`all_gather`/`all_to_all`）；经典 `dist.*` 是 in-place、可选异步、用 `ReduceOp` 枚举。
- functional collectives 是**纯函数式 + 天然异步**的新模型，底层落到 `torch.ops._c10d_functional.*` 真实算子，返回 `AsyncCollectiveTensor`（eager）或被 `wait_tensor` 显式同步（tracing）。
- 桥接两套 API 的两个零件：`REDUCE_OP_TO_STR`（枚举 → 字符串）与 `*_inplace` 包装（functional 结果 `copy_` 回原张量，复刻经典返回 `None` 的行为）。
- 本次更新新增 `_remap_traceable_collective`（共享的「参数归一化 + op 翻译」辅助）与 `_LegacyToFunctionalCollectiveMode`（`TorchFunctionMode`，挂到 `make_fx` 的 `compile_on_one_rank` 路径），并把非 strict export 也接到同一辅助函数上。
- 改写的根本动机：in-place `c10d.*` 把 `ProcessGroup` 绑成不可序列化的 torchbind 常量；functional 算子把 group 当作算子参数，于是 group 作为可序列化的数据流（字符串 name 或 `mesh_get_process_group` 节点）流入图，使 `compile_on_one_rank` 编译出的图能在任意 rank 加载。

## 7. 下一步学习建议

- **u10-l3（DDP 与 FSDP）**：DDP 反向时的梯度同步就是 `all_reduce` 的批量、桶化应用；FSDP 的 `reduce_scatter` / `all_gather` 对应参数的 shard/unshard。学完本讲，你能直接看懂 DDP/FSDP 在通信原语层的选择。
- **继续阅读源码**：
  - [torch/distributed/_functional_collectives.py](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/_functional_collectives.py) 顶部的两段大 docstring（设计动机、`AsyncCollectiveTensor` 的 data_ptr↔Work 配对机制）值得通读；
  - `torch.distributed.device_mesh.DeviceMesh.get_group` 在 `compile_on_one_rank` 下如何走到 `mesh_get_process_group`（[device_mesh.py:99-128](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/distributed/device_mesh.py#L99-L128)）。
- **延伸**：Dynamo 侧对应的 `CollectiveFunctionRewriteVariable`（在 `torch/_dynamo/` 下搜索该名字），与本讲的 `_LegacyToFunctionalCollectiveMode` 是同一改写的两个实现面，对照阅读能加深对「为什么要在多处做同一件事」的理解。
