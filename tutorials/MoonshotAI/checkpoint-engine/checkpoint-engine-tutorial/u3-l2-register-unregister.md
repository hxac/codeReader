# u3-l2 checkpoint 注册与注销生命周期

## 1. 本讲目标

上一讲(u3-l1)我们读完了 `ParameterServer.__init__`,它准备好了三样和本讲直接相关的状态:

- `self._memory_pool: dict[str, list[MemoryBuffer]]`,并且预埋了一个空列表条目 `__shared_memory_pool__`;
- `self._current_shared_memory_pool_user: str`,初始为空串,记录"当前谁在用共享池";
- `self._p2p_store`(可能为 `None`,取决于平台是否支持 P2P 以及 mooncake 是否安装)。

本讲沿着这三样状态,精读 `ParameterServer` 生命周期的前两步和最后一步:

```
register_checkpoint ──> gather_metas ──> update ──> unregister_checkpoint
     ↑ 本讲                                本讲 ↑        ↑ 本讲
```

学完本讲,你应该能够:

1. 说清 `files` 与 `named_tensors` 两种注册输入如何被同一套代码处理,以及"空注册"为什么是合法状态;
2. 独立推演出**注册失败时回滚代码的每一步会发生什么**,包括它的一处边界行为;
3. 解释 `_register_parameters_to_p2p_store` 的命名规则 `memory_pool_<name>_<idx>`,以及共享池模式下"只在首次注册"的原因;
4. 区分注销的三种语义:not-found 幂等返回、共享池"让位"(force=False)、真正的资源释放(force=True),并解释手动解页与 `host_empty_cache` 的收尾顺序。

## 2. 前置知识

本讲默认你已读过 u2-l3(锁页内存与两种 pin 策略)、u2-l5(共享 pin memory 池)和 u3-l1(初始化)。这里只做最简回顾:

- **MemoryBuffer**:一个"已锁页的扁平 uint8 缓冲 + 参数元数据清单"的组合体,是注册流程的最小产物。`manually_pinned=True` 表示它是被 `cudaHostRegister` 手动锁页的(inplace pin 路径),注销时必须手动解页。
- **p2p store**:对 mooncake `TransferEngine` 的封装。远端进程想通过 RDMA 读我的一段内存,必须先把这段内存的地址注册进 transfer engine;注册时给一段人类可读的名字。所以"注册 checkpoint"在物理上等于"锁页 + 向 p2p store 报备地址"。
- **回滚(rollback)**:注册是个多步操作(分配内存 → 拷贝数据 → 注册 p2p),中途任何一步失败,都要把已完成的步骤撤销掉,不能留下"半注册"状态,否则后续注销会踩到悬空指针或重复注册。这和数据库事务的回滚是同一个思想。

一个贯穿本讲的心智模型:**`register_checkpoint` 是"事务",`_memory_pool` 是账本,`unregister_checkpoint` 是逆事务**。读代码时时刻问两个问题:此刻账本上有没有这笔记录?p2p store 上有没有这笔记录?

## 3. 本讲源码地图

| 文件 | 角色 | 本讲涉及的关键符号 |
| --- | --- | --- |
| `checkpoint_engine/ps.py` | 服务端总装,本讲主战场 | `register_checkpoint`、`unregister_checkpoint`、`_register_parameters_to_p2p_store`、`_unregister_parameters_from_p2p_store`、`_get_memory_pool` |
| `checkpoint_engine/pin_memory.py` | 内存层,提供注册的分派器 | `_register_checkpoint`(以及它调用的 `_normal_pin_memory` / `_inplace_pin_memory`,内部细节在 u2-l3 已讲) |
| `checkpoint_engine/p2p_store.py` | P2P 传输层 | `register_named_tensors`、`unregister_named_tensors` |
| `checkpoint_engine/device_utils.py` | 硬件抽象 | `supports_inplace_pin`、`host_empty_cache` |
| `examples/update.py` | 真实调用方 | `split_checkpoint_files`(解释空注册的来源)、`update_weights` |
| `tests/test_reuse_pin_memory.py` | 生命周期测试 | `test_register_pin_memory`(本讲实践的依据) |

## 4. 核心概念与源码讲解

### 4.1 register_checkpoint:注册入口与两种输入

#### 4.1.1 概念说明

`register_checkpoint` 是 PS 对外的注册入口。它回答三个问题:

1. **权重从哪来?** 两种输入可以**同时**提供:
   - `files`:磁盘上的 `.safetensors`(或废弃的 `.npy`)文件路径列表;
   - `named_tensors`:一个 `dict[str, torch.Tensor]`,调用方已经在内存里握有的张量(比如训练侧直接把优化器产出的新权重递过来,不落盘)。
2. **内存怎么管?** `use_shared_memory_pool=True` 时复用 u2-l5 讲过的共享池,否则为这个 checkpoint 独立分配。
3. **要不要原地锁页?** `use_inplace_pin_memory=True`(默认)允许对 `/dev/shm/` 下的 safetensors 走原地锁页——**代价是注册成功后源文件会被删除**,这一点 docstring 里用 Warning 醒目标出。

#### 4.1.2 核心流程

```
register_checkpoint(name, files, named_tensors, use_shared_memory_pool, use_inplace_pin_memory)
│
├─ ① 能力降级:非 CUDA 后端不支持 inplace pin → 强制 use_inplace_pin_memory=False(打 warning)
│
├─ try
│   ├─ 分支 A:use_shared_memory_pool = True
│   │   ├─ assert 当前没有别的 checkpoint 占用共享池(user 必须为空串)
│   │   ├─ _is_first_time ← 共享池列表是否为空(空列表 = 池尚未定型)
│   │   ├─ _memory_pool["__shared_memory_pool__"] = _register_checkpoint(..., 共享池, inplace_pin=False)
│   │   ├─ _current_shared_memory_pool_user = name
│   │   └─ 若有 p2p store 且 _is_first_time → 向 p2p store 注册(只有首次!)
│   │
│   └─ 分支 B:独立内存
│       ├─ assert name 不在 _memory_pool(禁止重名注册)
│       ├─ _memory_pool[name] = _register_checkpoint(..., inplace_pin=use_inplace_pin_memory)
│       └─ 若有 p2p store → 向 p2p store 注册(每次都注册)
│
└─ except 任意异常 → 回滚(见 4.5)→ raise
```

注意两个分支的对称性:**账本写入(`_memory_pool[...] = ...`)永远发生在 p2p 注册之前**。这个顺序是理解 4.5 回滚推演的钥匙。

#### 4.1.3 源码精读

先看签名与两条防御性检查:

[checkpoint_engine/ps.py:305-335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L305-L335) —— `register_checkpoint` 的完整签名、docstring 里的删文件警告,以及第一道防御:`supports_inplace_pin()` 只在 CUDA 上返回 True,NPU/XPU 传入 `use_inplace_pin_memory=True` 会被静默降级为 False 并打 warning:

```python
if not self.device_manager.supports_inplace_pin() and use_inplace_pin_memory:
    logger.warning(...)
    use_inplace_pin_memory = False
```

其中能力开关的定义在 [checkpoint_engine/device_utils.py:285-287](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L287),一行 `return self.device_type == "cuda"`。

共享池分支(分支 A):

[checkpoint_engine/ps.py:337-358](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L337-L358) —— 先 assert 单一使用者;用 `not self._memory_pool[self.shared_memory_pool_name]`(空列表即 falsy)判断池是否首次使用;把现有池作为 `shared_pin_memory` 参数传下去让底层复用 buffer;强制 `inplace_pin=False`(原地锁页的槽位大小由文件布局决定,与"形状首次固定"的复用约束冲突,所以共享池模式天然排斥 inplace,这正是 u2-l5 讲过的结论)。最关键的是最后两行:

```python
self._current_shared_memory_pool_user = checkpoint_name
if self._p2p_store is not None and _is_first_time:
    self._register_parameters_to_p2p_store(checkpoint_name)
```

**只有首次才向 p2p store 注册**。原因在 u2-l5 已给出:复用共享池时底层 buffer 地址一个字节都没变,transfer engine 里既有的注册仍然有效,重复注册同一地址反而是浪费。

独立内存分支(分支 B):

[checkpoint_engine/ps.py:359-370](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L359-L370) —— assert 名字未占用后写入账本,再(若有 p2p store)每次都注册。`files or []` 与 `named_tensors or {}` 把 `None` 归一化为空容器,两种输入合并送进同一个 `_register_checkpoint`。

#### 4.1.4 代码实践

**实践:验证"空注册"是真实存在的调用形态(纯 CPU 可运行)**

1. **实践目标**:理解 `files`/`named_tensors` 两路输入在真实编排里如何分配到各 rank,并确认"某个 rank 两路输入都为空"是正常情况而非错误。
2. **操作步骤**:阅读 [examples/update.py:51-57](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L51-L57) 的 `split_checkpoint_files`——它把目录下所有 `.safetensors` 按秩均分。然后在任意 Python 环境里模拟这个切片:

   ```python
   files_per_rank = (3 + 8 - 1) // 8  # 3 个文件, 8 个 rank → 1
   rank_files = {r: list(range(3))[r * files_per_rank : (r + 1) * files_per_rank] for r in range(8)}
   print(rank_files)
   ```

   再对照调用点 [examples/update.py:110](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L110),`ps.register_checkpoint(checkpoint_name, files=checkpoint_files, named_tensors=named_tensors)` 对每个 rank 都会执行。
3. **需要观察的现象**:rank 3~7 拿到的切片是空列表。
4. **预期结果**:文件数小于 world_size 时,高秩 rank 必然空注册;`_register_checkpoint` 对空输入返回 `[]`(见 4.2),后续 `gather_metas` 也允许某 rank 不贡献任何权重——"部分 rank 无权重"是被全链路支持的合法形态。
5. 本实践是纯 Python 切片推演,可直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**:NPU 集群上调用 `register_checkpoint(name, files=["/dev/shm/a.safetensors"])` 走的是哪种 pin?文件会被删除吗?

**答案**:走 normal pin。NPU 上 `supports_inplace_pin()` 返回 False,入口处的降级逻辑把 `use_inplace_pin_memory` 改成 False,于是没有任何文件会被分流到 inplace 路径,`/dev/shm/a.safetensors` 保留在磁盘上。(对照 [checkpoint_engine/ps.py:331-335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335)。)

**练习 2**:共享池模式与独立模式,哪一种允许两个不同名字的 checkpoint 同时持有锁页内存?

**答案**:独立模式。共享池模式 assert `_current_shared_memory_pool_user == ""`,同一时刻只允许一个使用者;但共享池使用期间仍然可以用独立模式注册别的 checkpoint(见 [tests/test_reuse_pin_memory.py:39-46](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L39-L46),`test_checkpoint_shared1` 占池的同时注册了 `test_checkpoint2`)。

### 4.2 _register_checkpoint:内存层分派器与空注册

#### 4.2.1 概念说明

`pin_memory.py` 里的模块级函数 `_register_checkpoint`(注意与同名方法区分:PS 的方法是 `ParameterServer.register_checkpoint`,内存层的是 `pin_memory._register_checkpoint`)是纯粹的**分派器**:它不碰 `_memory_pool`、不碰 p2p store,只负责"把输入变成一组 `MemoryBuffer`"。它做了三次分流:

1. 两路输入都为空 → 直接返回 `[]`(空注册);
2. `inplace_pin=True` 时,把 `files` 再切成两半:`/dev/shm/` 开头且 `.safetensors` 结尾的走原地锁页,其余走 normal;
3. `named_tensors` **永远**走 normal pin(它们已经在普通内存里,没有"文件"可原地锁页)。

#### 4.2.2 核心流程

```
_register_checkpoint(files, named_tensors, rank, shared_pin_memory=None, inplace_pin=False)
│
├─ not files and not named_tensors → return []
├─ inplace_pin?
│   ├─ 是: files_to_inplace_pin = [f for f in files if f.startswith("/dev/shm/") and f.endswith(".safetensors")]
│   │       files_to_normal_pin = 其余文件
│   └─ 否: files_to_normal_pin = files,  files_to_inplace_pin = []
│
├─ (files_to_normal_pin 或 named_tensors 非空) → _normal_pin_memory(...)   # 结果放在列表前段
├─ files_to_inplace_pin 非空              → _inplace_pin_memory(...)      # 结果放在列表后段
└─ return memory_buffers
```

两条内部路径的细节分别是 u2-l3 与 u2-l4 的内容,本讲只强调编排层视角的两点:返回列表的顺序是 **normal 在前、inplace 在后**;`shared_pin_memory` 参数原样透传给 `_normal_pin_memory`,由其内层函数 `register_pin_memory` 决定复用还是新分配。

#### 4.2.3 源码精读

[checkpoint_engine/pin_memory.py:365-378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L365-L378) —— 函数签名与空注册短路。`if not files and not named_tensors: return []` 这两行是"部分 rank 无权重"场景的第一道支撑:空输入不会触发任何内存分配,也不会抛错。

[checkpoint_engine/pin_memory.py:379-389](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L379-L389) —— inplace 分流规则,判定条件就是两个字符串谓词:

```python
files_to_inplace_pin = [
    file for file in files
    if file.startswith("/dev/shm/") and file.endswith(".safetensors")
]
```

注意这里的 `inplace_pin` 形参已经被 PS 层做过两层过滤:共享池模式恒传 False;非 CUDA 后端已被降级为 False。

[checkpoint_engine/pin_memory.py:390-401](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L390-L401) —— normal 与 inplace 两段各自条件执行后拼接返回。normal 段的触发条件是 `files_to_normal_pin or named_tensors`,意味着"只有 named_tensors、没有文件"也走 normal pin。

#### 4.2.4 代码实践

**实践:亲手调用分派器,观察空注册(纯 CPU 可运行)**

1. **实践目标**:在本地(无需 GPU)直接驱动内存层的 `_register_checkpoint`,确认空输入路径的行为与日志。
2. **操作步骤**:在仓库根目录执行:

   ```bash
   python -c "
   from checkpoint_engine.pin_memory import _register_checkpoint
   bufs = _register_checkpoint(files=[], named_tensors={}, rank=0)
   print('buffers:', bufs)
   "
   ```

   然后试着把 `named_tensors` 换成 `{"w": __import__('torch').randn(4, 4)}` 再跑一次。
3. **需要观察的现象**:第一次调用打印一条 `start to register checkpoint with 0 files and 0 named_tensors` 日志并返回 `[]`;第二次调用会在 normal pin 分配锁页内存时失败。
4. **预期结果**:空注册返回空列表、不报错。第二次在**无 CUDA 的机器**上会因 `torch.empty(..., pin_memory=True)` 无法分配锁页内存而抛错(具体报错文本随 PyTorch 版本不同);在有 GPU 的机器上则应成功返回一个 `MemoryBuffer`。此行为**待本地验证**(取决于运行环境是否有 CUDA)。
5. 这个实践不修改任何源码,只做只读调用。

#### 4.2.5 小练习与答案

**练习 1**:同一批 `files` 里混着 `/dev/shm/a.safetensors` 和 `/data/b.safetensors`,且 `inplace_pin=True`。返回的 `memory_buffers` 列表里,两个文件对应的 buffer 谁在前?

**答案**:无法确定两个文件的相对顺序(取决于 `_normal_pin_memory` 内部按参数名排序后的切桶结果),但可以确定:**所有 normal pin 的 buffer 排在列表前段,所有 inplace pin 的 buffer 排在后段**;`/data/b.safetensors` 的 buffer 一定在 `/dev/shm/a.safetensors` 之前。这个顺序还决定了 4.3 中 p2p 注册名里的 `idx` 编号。

**练习 2**:为什么 `_register_checkpoint` 对空输入的处理是"返回空列表"而不是抛出"nothing to register"?

**答案**:因为切分逻辑(4.1.4 的实践)决定了高秩 rank 可能合法地分不到任何权重;若在这里抛错,文件数小于 world_size 的集群就无法注册了。空列表还会被下游继续容忍:`_register_parameters_to_p2p_store` 里 `if len(pool) == 0: return`,`gather_metas` 里空 `memory_buffer_metas_list` 的 rank 只是不进入全局参数表。

### 4.3 _register_parameters_to_p2p_store:命名规则与首次注册

#### 4.3.1 概念说明

锁页只是让本机的 H2D 拷贝可以异步;要让**远端 rank** 能通过 RDMA 直接读我的权重,还必须把 buffer 地址注册进 mooncake transfer engine。`_register_parameters_to_p2p_store` 做的就是这件事,它有一套稳定的命名规则:

```
memory_pool_{register_name}_{idx}
```

- `idx` 是 buffer 在 `list[MemoryBuffer]` 里的下标(由 4.2 的拼接顺序决定);
- `register_name` 一般就是 checkpoint 名,**唯一例外**:当前共享池使用者注册时,名字被改写成常量 `__shared_memory_pool__`。

这个改写是 u2-l5 结论"共享池的 p2p 注册名与 checkpoint 名无关"的直接出处:池的地址不变 → 注册不变 → 换一代权重、换一个 checkpoint 名,远端凭旧地址照样读到新数据。命名规则因此成了 join 复用模式的基石之一(u6-l3 会展开)。

#### 4.3.2 核心流程

```
_register_parameters_to_p2p_store(checkpoint_name)
│
├─ assert p2p store 已初始化
├─ pool = _get_memory_pool(checkpoint_name)     # 三岔分发(u2-l5 讲过)
├─ pool 为空 → return(空注册不占 p2p store)
├─ register_name = checkpoint_name
│                └─ 但若 checkpoint_name == 当前共享池使用者 → "__shared_memory_pool__"
├─ 对 pool 中第 idx 个 buffer:
│     named_tensors[f"memory_pool_{register_name}_{idx}"] = buffer
└─ p2p_store.register_named_tensors(named_tensors)   # 一次性批量注册地址
```

对应的注销函数 `_unregister_parameters_from_p2p_store` 用**同一套规则**重构名字列表并批量注销,返回注销数量。命名规则是这两个函数之间唯一的"接口约定"——名字拼错一个字符,注销就会 KeyError。

#### 4.3.3 源码精读

[checkpoint_engine/ps.py:721-735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L721-L735) —— 命名规则的核心实现。注意改写判断读的是 `self._current_shared_memory_pool_user`,而不是重新判断"是否共享池模式":

```python
register_name = (
    checkpoint_name
    if checkpoint_name != self._current_shared_memory_pool_user
    else self.shared_memory_pool_name
)
```

这隐含一个时序约束:**调用它之前必须已把 `_current_shared_memory_pool_user` 设为 checkpoint 名**(对照 4.1.2 流程图,`register_checkpoint` 里确实是先设使用者、后注册 p2p)。

[checkpoint_engine/ps.py:737-749](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L737-L749) —— 注销侧。`unregister_name` 用完全相同的表达式重构名字,然后委托给 store:

[checkpoint_engine/p2p_store.py:51-71](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L51-L71) —— 真正调 `engine.batch_register_memory` / `batch_unregister_memory` 的地方。注意 `register_named_tensors` 先 `self.named_tensors.update(...)` 再 assert 注册成功,注销时则从 `self.named_tensors[name]` 取地址并 `del`。这解释了为什么**重复注销同一个名字会 KeyError**(4.5 的推演会用到这个事实)。

#### 4.3.4 代码实践

**实践:用 stub 驱动真实的命名函数(纯 CPU 可运行)**

`_register_parameters_to_p2p_store` 只依赖 `self` 上的四个属性,我们不必真的启动 `ParameterServer`(那需要 GPU),用一个 stub 对象以"未绑定方法"的方式调用**生产代码**:

1. **实践目标**:验证命名规则 `memory_pool_{name}_{idx}`,以及共享池使用者被改写为 `__shared_memory_pool__` 的行为。
2. **操作步骤**:把下面脚本存为 `/tmp/practice_p2p_name.py`(注意不要写进仓库),在仓库根目录运行:

   ```python
   # 示例代码:仅用于学习,不修改仓库源码
   from checkpoint_engine.ps import ParameterServer
   from checkpoint_engine.data_types import MemoryBuffer
   import torch

   class FakeStore:
       def __init__(self):
           self.registered = {}
       def register_named_tensors(self, named_tensors):
           self.registered.update(named_tensors)

   class FakePS:
       shared_memory_pool_name = ParameterServer.shared_memory_pool_name

       def __init__(self, pool, user):
           self._p2p_store = FakeStore()
           self._memory_pool = pool
           self._current_shared_memory_pool_user = user

       # 借用真实方法的三岔分发
       _get_memory_pool = ParameterServer._get_memory_pool

   buf = MemoryBuffer(buffer=torch.empty(16, dtype=torch.uint8), size=16, metas=[])
   pool = {"ckpt-a": [buf]}

   # 场景 1:独立 checkpoint
   ps1 = FakePS(dict(pool), "")
   ParameterServer._register_parameters_to_p2p_store(ps1, "ckpt-a")
   print("独立注册名:", list(ps1._p2p_store.registered))

   # 场景 2:共享池使用者(池键固定,使用者名不同)
   ps2 = FakePS({ParameterServer.shared_memory_pool_name: [buf]}, "ckpt-b")
   ParameterServer._register_parameters_to_p2p_store(ps2, "ckpt-b")
   print("共享池注册名:", list(ps2._p2p_store.registered))
   ```

3. **需要观察的现象**:两次打印的名字列表。
4. **预期结果**:场景 1 得到 `['memory_pool_ckpt-a_0']`;场景 2 得到 `['memory_pool___shared_memory_pool___0']`(前缀 `memory_pool_` + `__shared_memory_pool__` + `_0`,连续下划线是 f-string 拼接的正常结果)。若把场景 2 的 `user` 传成空串再跑,名字会变回 `memory_pool_ckpt-b_0`——亲手验证 4.3.3 提到的时序约束。
5. 本实践只读仓库代码、只写 `/tmp`,纯 CPU 可运行;`torch.empty(16)` 不需要 CUDA。

#### 4.3.5 小练习与答案

**练习 1**:某个独立 checkpoint 有 3 个 buffer(normal 2 个 + inplace 1 个),p2p 注册名分别是什么?

**答案**:`memory_pool_<checkpoint_name>_0`、`memory_pool_<checkpoint_name>_1`、`memory_pool_<checkpoint_name>_2`。idx 就是 4.2 拼接后列表的下标,注册侧并不区分这个 buffer 当初是 normal 还是 inplace 来的。

**练习 2**:如果把一个 checkpoint 直接命名为 `__shared_memory_pool__` 并走独立模式注册,会发生什么?

**答案**:`__init__` 已经在 `_memory_pool` 里预埋了 `__shared_memory_pool__` 键(空列表),独立分支的 `assert checkpoint_name not in self._memory_pool` 会立刻失败,注册被拒绝。这个常量名因此天然被保留,不会与用户 checkpoint 冲突。

### 4.4 unregister_checkpoint:注销的三岔路口与手动解页

#### 4.4.1 概念说明

注销远比"从 dict 里删个键"复杂,因为注册时 acquiring 了三种资源:账本条目、锁页内存(部分是手动锁的)、p2p store 上的地址注册。`unregister_checkpoint(name, force=False)` 按名字的归属分成三条路:

| 场景 | 判定条件 | 行为 |
| --- | --- | --- |
| not-found | 名字既不在 `_memory_pool` 也不是当前池使用者 | 打 warning,幂等返回(不报错) |
| 让位 | 名字 == 当前共享池使用者 且 `force=False` | 只清空 `_current_shared_memory_pool_user`,**池和 p2p 注册原样保留** |
| 真释放 | 其余(独立 checkpoint,或池使用者带 `force=True`) | 注销 p2p → 手动解页 → 删账本 → `host_empty_cache` |

"让位"与"真释放"的区分是 u2-l5 的语义在代码里的落点:让位后立刻注册下一代权重可零拷贝复用;force 才把锁页内存还给系统。

#### 4.4.2 核心流程

```
unregister_checkpoint(name, force=False)
│
├─ ① not found? → warning; return
├─ ② 池使用者且 not force? → user=""; return          # 让位
├─ ③ p2p store 存在? → _unregister_parameters_from_p2p_store(name)
├─ ④ 池使用者(此时必为 force)? → user=""; 删除池条目; 重新预埋空列表
│      else(独立 checkpoint):
│      ├─ 对每个 manually_pinned 的 MemoryBuffer 执行 _unpin
│      │    ├─ ctypes.CDLL(None) 取 cudaHostGetFlags 校验 flags == 0x02
│      │    └─ cudart.cudaHostUnregister(data_ptr)
│      └─ del _memory_pool[name]                        # 解页失败则不删(见 ④ 源码注释)
└─ ⑤ device_manager.host_empty_cache()                  # CUDA: 归还 pinned host cache; 其他: gc.collect()
```

防御式顺序值得注意:**先校验(flags 必须是 0x02 即 cudaHostRegisterMapped)、先解页、后删账本**。解页失败会 raise,而 `del` 不会执行——账本保留意味着你还能重试注销,不会留下"账本没了但内存还锁着"的无主状态。

#### 4.4.3 源码精读

[checkpoint_engine/ps.py:389-400](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L389-L400) —— 前两岔:not-found 的幂等返回与"让位"分支。注意 not-found 的判定条件把两个名字空间都查了(`_memory_pool` 的键 + 当前池使用者),所以"让位之后再 force"只会命中 not-found 的 warning——因为让位时使用者已被清空,而池条目 `__shared_memory_pool__` 不等于你的 checkpoint 名。这正是 u2-l5 说过"让位后名字即失效,再 force 只得 not-found 警告"的代码出处。

[checkpoint_engine/ps.py:402-411](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L402-L411) —— 第三步先注销 p2p(让位分支已在上方提前 return,所以**让位不会注销 p2p**,池地址继续对远端可见);随后是 force 释放共享池:清使用者、`del` 池条目、再重新预埋空列表,让共享池回到"从未定型"的初始状态,下次注册可按新形状重建。

[checkpoint_engine/ps.py:414-457](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L414-L457) —— 独立 checkpoint 的手动解页。`_unpin` 先用 `ctypes.CDLL(None)` 从当前进程符号表里取 `cudaHostGetFlags`(u2-l4 讲过这套 ctypes 手法),断言 flags 恰为 `0x02`(cudaHostRegisterMapped,即当初 `cudaHostRegister` 注册过的内存),再调 `cudart.cudaHostUnregister`;循环只处理 `manually_pinned=True` 的 buffer——normal pin 出来的锁页内存由 PyTorch 缓存分配器管理,随 `del` + 下一步的 cache 清理自然回收。源码注释明确写着 "we won't delete the memory pool if unpinning fails"。

[checkpoint_engine/ps.py:458-460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L458-L460) —— 收尾的 `host_empty_cache()`。实现在 [checkpoint_engine/device_utils.py:307-312](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L307-L312):CUDA 上调 `torch._C._host_emptyCache()`(源码注释链接指向 PyTorch,需 torch>=2.5),把 pinned host cache 真正归还给操作系统;NPU/XPU 没有对应 API,退化为 `gc.collect()`。

#### 4.4.4 代码实践

**实践 A:按真实测试做状态推演表(纯 CPU,纸面 + 读码)**

1. **实践目标**:把 `unregister_checkpoint` 的三岔逻辑内化成一张可核对的状态表。
2. **操作步骤**:逐行阅读 [tests/test_reuse_pin_memory.py:36-79](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L36-L79),为其中每一次 `register` / `unregister` 调用填一行下表(表头已给出):

   | 调用 | 命中哪一岔? | `_memory_pool` 的键 | `_current_shared_memory_pool_user` |
   | --- | --- | --- | --- |

3. **需要观察的现象**:L55(让位)、L64(not-found)、L68(force)三行分别命中不同岔口。
4. **预期结果**(节选,供核对):L55 `unregister_checkpoint("test_checkpoint_shared1")` → 命中让位,键集合不变(仍含 `__shared_memory_pool__`),user 变 `""`;L64 → 命中 not-found(该名字 L37 已被独立注销),仅打印 warning;L68 force → user 变 `""`,池条目被删后立刻重建为空列表,断言 `__shared_memory_pool__ in ps._memory_pool` 成立。该测试带 `@pytest.mark.gpu`,需 GPU 才能真正执行(见 [tests/test_reuse_pin_memory.py:22-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L22-L28)),CPU 环境做纸面推演即可。

**实践 B:在 GPU 机器上观察 not-found 与手动解页(需 GPU,待本地验证)**

1. **实践目标**:亲眼看到 warning 与解页日志。
2. **操作步骤**:仿照该测试的环境变量准备,运行 `ps.unregister_checkpoint("no-such-name")`,观察日志;再用一个 `/dev/shm` 下的 safetensors 注册后注销,对比日志中是否出现 `cudaHostUnregister` 相关输出与 `p2p store unregister tensor ...` 列表。
3. **预期结果**:前者只有一条 `unregister checkpoint name no-such-name not found` warning 且立即返回;后者能看到 p2p 注销条数与解页流程。**待本地验证**(需要 CUDA 与 mooncake 环境)。

#### 4.4.5 小练习与答案

**练习 1**:让位(force=False)之后,远端 rank 还能通过 p2p 读到这批权重吗?

**答案**:能。让位分支在 p2p 注销代码之前就 `return` 了([checkpoint_engine/ps.py:398-400](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L398-L400) 早于 L402),transfer engine 里的地址注册未被动过,锁页内存也未释放。这是刻意设计:新一代权重会写进同一块 buffer,远端凭旧元数据继续读即可。

**练习 2**:为什么 `_unpin` 里要先 `cudaHostGetFlags` 断言 0x02 再解页,而不是直接 unregister?

**答案**:防御式编程。 unregister 一段没有被自己注册(或已被解页)的内存是未定义行为,可能破坏驱动状态;先用 flags 校验"这确实是 cudaHostRegister 注册过的 Mapped 内存",把误用变成一个清晰的 AssertionError,而不是让驱动在更深处崩溃。这也解释了为什么只有 `manually_pinned` 的 buffer 才走这条路——normal pin 的锁页内存不属于进程手动管理的注册区。

### 4.5 失败回滚:except 分支的三种情形推演

#### 4.5.1 概念说明

`register_checkpoint` 的整个主体包在一个 `try/except Exception` 里。回滚要撤销的资源有两类(账本条目、p2p 注册),而**失败发生在哪一步,决定了哪些资源已经落地**。这里有一个容易忽略的 Python 细节:

```python
self._memory_pool[checkpoint_name] = _register_checkpoint(...)
```

赋值语句先求值右侧再写键。若 `_register_checkpoint` 抛异常,**账本上根本不会有这个键**——但 except 分支里的清理函数会先去 `_get_memory_pool` 查这个键,查不到就抛 `RuntimeError`。于是"清理动作本身失败"成为可能。

#### 4.5.2 核心流程

回滚骨架([checkpoint_engine/ps.py:371-378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L371-L378)):

```
except Exception:
    logger.exception(原始异常)                      # ① 先留全栈日志
    if p2p_store 存在 且 非共享池模式:
        _unregister_parameters_from_p2p_store(name)  # ② 尝试撤 p2p
    unregister_checkpoint(name)                      # ③ 尝试撤账本+解页
    raise                                             # ④ 重抛原始异常
```

注意 ② 被 `not use_shared_memory_pool` 守卫:共享池模式下 ③ 一步就够(共享池的 p2p 注册名固定,即便部分注册也要靠 ③ 的逻辑处理)。

按失败点分四种情形推演(以下均为**基于源码文本的推演**,建议按 4.5.4 实践验证):

| 情形 | 失败点 | 此刻账本 | ② 的结果 | ③ 的结果 | 最终传播的异常 |
| --- | --- | --- | --- | --- | --- |
| A | 独立模式,`_register_checkpoint` 内部(文件损坏/OOM) | 键未写入 | `_get_memory_pool` 抛 `RuntimeError`(若 p2p store 为 None 则跳过 ②) | ② 抛错时不再执行;② 被跳过时命中 not-found warning 后返回 | ② 的 RuntimeError(原异常降级为 `__context__`)或 ④ 的原异常 |
| B | 独立模式,`_register_parameters_to_p2p_store`(如 `batch_register_memory` 断言失败) | 键已写入 | 成功注销一次 | ③ 内部**再次**注销同名 → `self.named_tensors[name]` 已被 ② 删除 → KeyError | ③ 的 KeyError(原异常成为 `__context__`) |
| C | 共享池模式,`_register_checkpoint` 内部 | 池保持旧值,user 仍为 `""` | 被守卫跳过 | 命中 not-found warning 后返回 | ④ 重抛原异常,状态干净 |
| D | 共享池模式,首次,p2p 注册失败 | 池已定型,user 已设为 name | 被守卫跳过 | 命中让位分支:清 user 后返回,**池保留** | ④ 重抛原异常;但池非空导致下次注册 `_is_first_time=False`,**p2p 注册不会再被尝试** |

情形 B 和情形 D 暴露了回滚路径的两处粗糙边缘:B 中"注销两次同一个名字"会 KeyError;D 中"首次 p2p 注册失败后再也无人补注册"。它们未必会在实践中触发(mooncake 注册失败本身罕见),但读代码时能推演出这两条路径,说明你真正理解了状态机。

#### 4.5.3 源码精读

[checkpoint_engine/ps.py:363-378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L363-L378) —— 把"赋值→p2p 注册"与 except 分支放在同一屏读:键写入在 L363,p2p 注册在 L369-370,回滚三步在 L372-378。情形 A/B 的分界就是 L363 与 L369 之间。

[checkpoint_engine/ps.py:277-286](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L277-L286) —— `_get_memory_pool` 的三岔分发(共享池使用者 / 已注册键 / 抛 `RuntimeError`)。情形 A 里 ② 抛出的正是最后一行的 `RuntimeError: checkpoint ... is not registered`。

[checkpoint_engine/p2p_store.py:61-66](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L61-L66) —— `unregister_named_tensors` 的第一行列表推导直接索引 `self.named_tensors[name]`,名字不存在即 KeyError,这是情形 B 的直接出处。

[tests/test_reuse_pin_memory.py:47-54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L47-L54) —— 一个**已被测试验证过的回滚情形**(与情形 C 同构):第二位共享池使用者触发 assert 失败后,except 分支走 `not use_shared_memory_pool=False` → 跳过 ② → ③ 命中 not-found → ④ 重抛 AssertionError。测试捕获的正是 AssertionError 本身,且事后断言状态未被污染。这也是我们确信"共享池模式的回滚是干净的"的依据。

#### 4.5.4 代码实践

**实践:注入一次真实的注册失败,观察回滚(需 GPU,待本地验证)**

1. **实践目标**:把 4.5.2 的推演表变成亲眼所见,重点是 logger.exception 打印的原始异常栈与最终传播的异常类型。
2. **操作步骤**(在有 CUDA 的机器上,仿照 `test_reuse_pin_memory.py` 的环境变量):

   ```python
   # 示例代码:需 GPU 环境,待本地验证
   ps.register_checkpoint("bad-ckpt", files=["/tmp/not-exist.safetensors"])
   ```

   分别在三种配置下各跑一次:p2p 可用的完整环境(对应情形 A 的 p2p 分支)、未安装 mooncake 的环境(`_p2p_store is None`,对应情形 A 的跳过分支)、`use_shared_memory_pool=True`(对应情形 C)。
3. **需要观察的现象**:日志里的原始异常(safetensors 打不开文件)与最终 `except` 传播出的异常是否相同;`ps._memory_pool` 里是否残留 `bad-ckpt` 键。
4. **预期结果**(按推演):未装 mooncake 时最终重抛原始异常、账本无残留;装有 mooncake 时若 `_register_checkpoint` 阶段就失败,传播的将是 `RuntimeError: checkpoint bad-ckpt is not registered`(原始异常可在日志与 `__context__` 中找到);共享池模式下与测试一致,AssertionError/原异常直抛、状态干净。**待本地验证**。
5. 无 GPU 环境的替代:把 4.5.2 的表格当作填空练习,只对照源码逐行验证自己的推演,再与上文表格核对。

#### 4.5.5 小练习与答案

**练习 1**:为什么 except 分支里 ②(`_unregister_parameters_from_p2p_store`)要加 `not use_shared_memory_pool` 守卫?

**答案**:共享池模式下 checkpoint 名字本身不在 `_memory_pool`(池的键固定是 `__shared_memory_pool__`),除非使用者字段已写入。情形 C 中使用者还没设置,直接调 ② 必然在 `_get_memory_pool` 处抛错;而让位语义(4.4)也不希望注销池的 p2p 注册。所以共享池的回滚统一交给 ③ 的三岔逻辑处理。

**练习 2**:情形 D 之后,如果运维直接重启进程重新注册同一批权重,p2p 注册还能恢复吗?

**答案**:能——进程重启后 `_memory_pool` 清零,`_is_first_time` 重新为 True,首次注册会重建 p2p 注册。受影响的只是"不重启、继续用同一个 PS 实例"的路径(池已非空,永远跳过 p2p 注册)。

## 5. 综合实践

**任务:绘制并验证「注册-注销」全生命周期状态机**。

把本讲四个模块串起来,完成三件事:

1. **画状态机**(纸面):以 `(_memory_pool 的键集合, _current_shared_memory_pool_user, p2p store 中该 checkpoint 的注册名集合)` 为状态向量,事件为 `{register(独立), register(共享池, 首次), register(共享池, 复用), unregister(force=False), unregister(force=True), 注册失败}`。画出状态迁移图,并标注哪些迁移会调 `cudaHostRegister`/`cudaHostUnregister`、哪些会调 `batch_register_memory`/`batch_unregister_memory`。对照依据:4.1.2 与 4.4.2 的流程图、4.5.2 的推演表。
2. **命名规则驱动验证**(纯 CPU 可运行):完成 4.3.4 的 stub 实践后,扩展它——给 stub 加一个 `unregister_named_tensors`,然后依次以未绑定方式调用 `ParameterServer._register_parameters_to_p2p_store` 与 `ParameterServer._unregister_parameters_from_p2p_store`,断言"注册名集合 == 注销名集合"。这正好把 4.3 的"命名规则是两侧唯一接口约定"落到实处。
3. **对照真实测试自查**(读码):用 [tests/test_reuse_pin_memory.py:22-79](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L22-L79) 的每一条 assert 校验你的状态机:任何一条 assert 与你的迁移边矛盾,说明你的状态机有错。有 GPU 的机器可以直接 `pytest tests/test_reuse_pin_memory.py -v` 运行验证(该测试带 gpu marker)。

完成标准:你的状态机能正确预测测试里全部 9 处 `assert`(L38、L43-46、L53-54、L56-57、L61-63、L65、L69-70、L74-76、L78-79)的结果。

## 6. 本讲小结

- `register_checkpoint` 是编排层:两种输入(`files` + `named_tensors`)合并后统一交给 `pin_memory._register_checkpoint` 分派,空输入合法返回 `[]`,支撑"文件数少于 world_size 时高秩 rank 空注册"的真实形态。
- 账本写入(`_memory_pool[name] = ...`)永远先于 p2p 注册;共享池模式下 p2p **只在首次**注册,且注册名恒为 `__shared_memory_pool__` 而与 checkpoint 名无关——地址不变则注册不变,这是 join 复用模式的物理基础。
- `unregister_checkpoint` 是三岔路口:not-found 幂等返回、共享池让位(保留池与 p2p 注册)、真释放(force 或独立 checkpoint:p2p 注销 → 手动解页 → 删账本 → `host_empty_cache`),整体遵循"先校验、先解页、后删账本"的防御式顺序。
- 手动解页只针对 `manually_pinned=True` 的 buffer:先用 `cudaHostGetFlags` 断言 0x02 再 `cudaHostUnregister`,失败时不删账本以便重试。
- 回滚路径按失败点分四种情形:独立模式的两个失败点分别会因"账本键未写入"和"重复注销同名"让清理动作本身抛错(原异常降级为 `__context__`),共享池模式的两个失败点则都能干净回滚;其中"首次 p2p 注册失败后不再重试"是一个值得记住的边缘行为。
- 本讲三处推演(情形 A/B/D 的异常传播)基于源码文本,已在文中标注**待本地验证**;与测试同构的情形 C 有 [tests/test_reuse_pin_memory.py:47-54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L47-L54) 背书。

## 7. 下一步学习建议

注册完成后,权重还只是"躺在本机锁页内存里、p2p 地址已报备"。下一讲 **u3-l3 gather_metas:全局元数据收集** 将讲每个 rank 如何用一次 `all_gather_object` 把自己这份 `MemoryBuffer` 的元数据(指针、大小、参数清单、RDMA 拓扑)广播给全体,拼出 `_current_global_parameter_metas` 这张全局权重地图——那是 `update` 广播能够开工的前提。

继续阅读建议:

- 想先看"注册的产物如何被消费":跳读 [checkpoint_engine/ps.py:462-525](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L462-L525) 的 `gather_metas`,注意它对 `memory_pool` 取不到时的容错(`except RuntimeError: memory_pool = []`);
- 想复习共享池复用的底层约束:回看 u2-l5 的 `register_pin_memory` 形状断言;
- 想了解注册名在远端如何被使用:预读 u5-l5 的 `P2PStore.batch_transfer_sync_read`,它消费的正是本讲注册的地址。
