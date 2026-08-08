# Ray 编排：placement group 与资源分配

## 1. 本讲目标

上一讲（u2-l1）我们建立了 slime 的「三模块闭环」地图：rollout 采样 → data buffer → training 训练 → 权重同步回 rollout。但那张图里有一个被刻意略过的关键问题：**这些模块到底是怎么被「摆」到具体的 GPU 上的？**

在单机单卡时代，这个问题不存在——所有东西都在同一张卡上。但 slime 面向的是几十上百张卡的 RL 训练集群，actor（训练）、rollout（推理引擎）、critic（价值模型）都要抢 GPU。如果让它们各跑各的、互不协调，就会出现：训练卡和推理卡各占一半导致显存浪费、或两个模块被调度到不同机器上跨网络通信拖慢训练、或 colocate（共卡）时谁也不肯让出显存而 OOM。

slime 用 **Ray placement group（放置组）** 来解决「谁用哪几张卡」的问题。本讲读完，你应该能够：

1. 说清楚 slime 是如何用**一个** placement group 同时容纳训练卡与推理卡的；
2. 手算 `_get_placement_group_layout` 在普通分离 / colocate / debug / 外部引擎四种模式下各返回多少张卡、rollout 从第几张卡开始；
3. 解释 `InfoActor` 这个「临时探针」是如何把「逻辑编号」翻译成「物理 GPU 编号」的；
4. 理解 colocate 模式在资源层面为什么必然导致 `needs_offload`（必须轮流让出显存）。

## 2. 前置知识

### 2.1 什么是 Ray

Ray 是一个通用的分布式计算框架。你只要把一个 Python 类用 `@ray.remote` 装饰，它的实例就成了一个跑在集群某台机器上的 **actor**；你调用它的方法时，Ray 会在后台异步执行，并返回一个 **ObjectRef**（类似 future）。`ray.get(ref)` 用来阻塞等待结果。

slime 用 Ray 做两件事：
- **调度**：把训练工人（MegatronTrainRayActor）和推理引擎（SGLangEngine）分配到具体 GPU 上；
- **通信**：模块之间通过 ObjectRef 传数据、传控制信号。

### 2.2 什么是 placement group（放置组）

placement group 是 Ray 提供的「资源预约」机制。核心概念是 **bundle（资源束）**：一个 bundle 就是一份资源声明，比如 `{"GPU": 1, "CPU": 1}` 表示「要 1 张 GPU + 1 个 CPU」。

你提交一组 bundle，Ray 会**原子地**把它们一次性调度到集群上——要么全部成功，要么全部失败。这保证了你声明的资源一定是一个整体被分配的，不会出现「训练工人调度到了 A 机，推理引擎却调度到了 B 机」的撕裂。

调度策略有多种，slime 用的是 **PACK**：尽量把所有 bundle 塞进尽可能少的节点（机器）。这对训练很关键——所有训练 rank 挤在同一台机器内，可以用机器内的 NVLink 高速互联，避免跨机网络。

> 一句话记忆：**bundle 是「我要几张卡」的声明单位，placement group 是把这些声明打包在一起原子预约的容器，PACK 是「挤同一台机器」的摆放策略。**

## 3. 本讲源码地图

本讲只围绕两个文件展开，外加一个消费侧的引用来说明 colocate 的后果。

| 文件 | 作用 |
|------|------|
| `slime/ray/placement_group.py` | 资源分配的核心：决定要多少卡、预约它们、探测物理 GPU、把卡切片分给 actor/rollout |
| `slime/ray/utils.py` | Ray 相关的小工具：默认环境变量、GPU UUID 获取、分布式锁 |
| `slime/ray/rollout.py`（引用） | 消费 placement group 的推理侧：根据 actor 占用的卡段计算 `needs_offload` |
| `train.py`（引用） | 入口：第 14 行调用 `create_placement_groups`，是整个分配的触发点 |

调用链全景：`train.py` 调 `create_placement_groups` → 调 `_get_placement_group_layout`（算总量与偏移）→ 调 `_create_placement_group`（真预约 + 用 `InfoActor` 探测）→ 切片分给 actor / rollout。

## 4. 核心概念与源码讲解

### 4.1 资源分配的入口：create_placement_groups

#### 4.1.1 概念说明

`create_placement_groups` 是分配 GPU 的总入口。它的设计有一个非常优雅的核心思想：**只创建一个 placement group，把训练卡和推理卡都装在里面，然后用一个「偏移量（offset）」把 bundle 列表切成两段**——前一段给训练，后一段给推理。

这样做的好处是：

- **原子性**：训练和推理的资源是一次性预约的，保证它们落在同一批机器上（配合 PACK 策略，通常挤在一起），跨模块通信延迟低。
- **简洁**：不需要为 actor 和 rollout 各建一个 placement group 再去协调它们的相对位置；一段切片就表达了「谁用哪些卡」。
- **天然支持 colocate**：当 offset = 0 时，actor 和 rollout 拿到的是**同一段** bundle 列表——这就是 colocate 在资源层面的全部实现，二者共享同一批物理 GPU。

函数返回一个字典，键是 `actor` / `rollout` / `critic`，值都是一个三元组 `(placement_group, bundle_index_list, gpu_id_list)`，分别表示「用哪个放置组」「该角色排在哪些 bundle 上」「这些 bundle 对应的物理 GPU 编号」。

#### 4.1.2 核心流程

```
create_placement_groups(args):
  1. (num_gpus, rollout_offset) = _get_placement_group_layout(args)
       # num_gpus: 这一个 placement group 要几张卡
       # rollout_offset: rollout 的卡从第几个 bundle 开始
  2. (pg, all_bundle_indices, all_gpu_ids) = _create_placement_group(num_gpus)
       # 真正向 Ray 预约 num_gpus 张卡，并探测每张卡的物理编号
  3. actor  的卡 = all_bundle_indices[0 : rollout_offset]      # 注意：前 offset 段给 actor
     rollout 的卡 = all_bundle_indices[rollout_offset : ]        # 后面给 rollout
  4. result = {
       "actor":   (pg, actor 的 bundle, actor 的 gpu_id),
       "rollout": (pg, rollout 的 bundle, rollout 的 gpu_id),
     }
  5. result["critic"] = result["actor"]  if 用 critic  else  None
       # critic 直接复用 actor 的同一组卡
```

注意第 3 步：actor 的切片是 `[0:rollout_offset]`，rollout 是 `[rollout_offset:]`。在 colocate 模式下 offset = 0，于是 actor 切片变成 `[0:0]` = 空？不对——等一下，这里要仔细看源码，actor 拿到的其实是**完整的** `all_bundle_indices`，只有 rollout 做了偏移切片。这正是下一节源码精读要澄清的关键点。

#### 4.1.3 源码精读

入口函数本身很短，只有十几行，但每一行都对应一个关键决策：

[slime/ray/placement_group.py:120-137](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L120-L137) —— 这是整个分配的入口。第 123 行调 `_get_placement_group_layout(args)` 拿到 `(num_gpus, rollout_offset)`；第 126 行**只创建一个** placement group；第 127–128 行只对 rollout 做偏移切片（`[rollout_offset:]`），而 actor 用的是完整列表。

```python
def create_placement_groups(args):
    num_gpus, rollout_offset = _get_placement_group_layout(args)
    pg, actor_pg_reordered_bundle_indices, actor_pg_reordered_gpu_ids = _create_placement_group(num_gpus)
    # 只有 rollout 做了偏移切片，actor 拿到完整列表
    rollout_pg_reordered_bundle_indices = actor_pg_reordered_bundle_indices[rollout_offset:]
    rollout_pg_reordered_gpu_ids = actor_pg_reordered_gpu_ids[rollout_offset:]

    result = {
        "actor":   (pg, actor_pg_reordered_bundle_indices, actor_pg_reordered_gpu_ids),
        "rollout": (pg, rollout_pg_reordered_bundle_indices, rollout_pg_reordered_gpu_ids),
    }
    result["critic"] = result["actor"] if args.use_critic else None
    return result
```

阅读这段源码，注意三个细节：

1. **actor 拿完整列表，rollout 拿后缀切片**。在非 colocate 模式下，actor 的卡是 `[0:8]` 中的前 4 张，rollout 是后 4 张——但因为 actor 拿的是完整 `[0:8]`，它实际只会用前 `actor_num_gpus` 个（由后面 `_allocate_gpus_for_actor` 按 rank 决定）。在 colocate 模式下 offset = 0，rollout 切片 `[0:]` 也是完整列表，于是 actor 和 rollout **指向完全相同的 bundle**，即同一批物理卡。
2. **`critic` 直接复用 actor 的卡**（第 135 行）。结合参数定义，critic 的 GPU 数永远等于 actor 的 GPU 数（见 [slime/utils/arguments.py:1849-1850](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1849-L1850)），所以 critic 不是额外的卡，而是和 actor 共用同一组卡、靠 `offload_train` 轮流让出显存来时分复用。这也是为什么 `--advantage-estimator=ppo`（用 critic）会强制开启 `offload_train`。
3. 这个函数在 `train.py` 第 14 行被调用（[train.py:14](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L14)），是整个训练流程里**第一个**实质性动作——先占好卡，再创建 rollout manager 和训练模型。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码确认「actor 与 rollout 是否真的共用 bundle 列表」，并理解 critic 的复用。

**操作步骤**：

1. 打开 [slime/ray/placement_group.py:130-136](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L130-L136)，对比 `result["actor"]` 和 `result["rollout"]` 的第二个元素（bundle 列表）分别是哪个变量。
2. 思考：当 `rollout_offset = 0` 时，`rollout_pg_reordered_bundle_indices` 等于什么？它和 `actor_pg_reordered_bundle_indices` 是同一个 list 对象的切片还是不同的？
3. 打开 [slime/utils/arguments.py:1847-1850](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1847-L1850)，确认 `use_critic` 何时为真、critic 的 GPU 数如何被设为与 actor 相同。

**需要观察的现象**：`rollout_offset = 0` 时，rollout 切片是完整列表的浅拷贝（Python 切片产生新 list，但元素相同），所以 actor 和 rollout 引用同一批 bundle。`use_critic` 仅在 `advantage_estimator == "ppo"` 时为真。

**预期结果**：你会确认 colocate 不是靠什么复杂机制实现的——仅仅是 `rollout_offset = 0` 让两个角色指向相同的 bundle 列表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 slime 选择「一个 placement group 装下所有卡」，而不是「actor 一个组、rollout 一个组」？

**参考答案**：单个 placement group 保证 actor 和 rollout 的卡是**一次性原子预约**的，且配合 PACK 策略会挤在同一批机器上，跨模块通信（如权重同步）走机器内互联而不是跨机网络。两个独立的组无法保证相对位置，可能被调度到不同机器。

**练习 2**：`result["critic"] = result["actor"]` 这行说明 critic 占用额外 GPU 吗？

**参考答案**：不占用。critic 复用 actor 的同一组卡（同一 placement group、同一 bundle 列表），靠 `offload_train` 让两个模型轮流使用显存。所以用 critic（PPO）不会翻倍 GPU 需求，但会强制开启 offload。

---

### 4.2 分配大脑：_get_placement_group_layout 的五种模式

#### 4.2.1 概念说明

`_get_placement_group_layout` 是整个资源分配的「大脑」。它接收全部命令行参数，输出一个二元组 `(num_gpus, rollout_offset)`：

- `num_gpus`：这个 placement group 一共要预约多少张卡；
- `rollout_offset`：rollout 的卡从第几个 bundle 开始（决定 actor 和 rollout 在 bundle 列表里如何切分）。

它用一组 `if/elif` 把所有运行模式收敛到一张表里。理解这张表，就理解了 slime 在「卡怎么分」这件事上的全部决策。

#### 4.2.2 核心流程

```python
actor_num_gpus = actor_num_nodes * actor_num_gpus_per_node   # 训练所需的卡数

# 按优先级依次判断当前处于哪种模式：
if debug_train_only:        → (actor_num_gpus,            0)   # 只训练，不推理
if rollout_external:
    if debug_rollout_only:  → (0,                          0)   # 极端调试
    else:                   → (actor_num_gpus, actor_num_gpus)  # 推理在外部，本地 0 卡给 rollout
if debug_rollout_only:      → (rollout_num_gpus,          0)   # 只推理，不训练
if colocate:                → (max(actor_num_gpus, rollout_num_gpus), 0)  # 共卡，offset=0
# 默认（普通分离部署）：
                            → (actor_num_gpus + rollout_num_gpus, actor_num_gpus)
```

把它整理成「人能背」的表：

| 模式 | num_gpus（总卡数） | rollout_offset | 含义 |
|------|------------------|----------------|------|
| 普通分离（默认） | `actor + rollout` | `actor_num_gpus` | 训练卡在前，推理卡在后，互不重叠 |
| `colocate`（共卡） | `max(actor, rollout)` | `0` | 训练和推理共用同一批卡，offset=0 表示完全重叠 |
| `debug_train_only` | `actor_num_gpus` | `0` | 只跑训练（加载预存的 rollout 数据），不预约推理卡 |
| `debug_rollout_only` | `rollout_num_gpus` | `0` | 只跑推理，不预约训练卡 |
| `rollout_external`（推理引擎在外部集群） | `actor_num_gpus` | `actor_num_gpus` | 本地只给训练卡；rollout 切片 `[actor:]` 为空，因为推理不占本地卡 |

注意 `rollout_external` 那行很巧妙：`num_gpus = actor_num_gpus`，`rollout_offset = actor_num_gpus`，于是 rollout 切片 `[actor_num_gpus:]` 正好是空列表——本地一张推理卡都不分配，因为推理引擎跑在 `--rollout-external-engine-addrs` 指定的外部集群里。

#### 4.2.3 源码精读

大脑函数的源码极其紧凑，每个分支对应一种部署形态：

[slime/ray/placement_group.py:100-117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L100-L117) —— `_get_placement_group_layout` 全文。

```python
def _get_placement_group_layout(args) -> tuple[int, int]:
    actor_num_gpus = args.actor_num_nodes * args.actor_num_gpus_per_node

    if args.debug_train_only:
        return actor_num_gpus, 0

    if args.rollout_external:
        if args.debug_rollout_only:
            return 0, 0
        return actor_num_gpus, actor_num_gpus

    if args.debug_rollout_only:
        return args.rollout_num_gpus, 0

    if args.colocate:
        return max(actor_num_gpus, args.rollout_num_gpus), 0

    return actor_num_gpus + args.rollout_num_gpus, actor_num_gpus
```

逐行看几个关键点：

- 第 101 行先算出训练所需的卡数 `actor_num_gpus`，这是后续所有分支的基础量。
- colocate 分支（第 114–115 行）用 `max(actor_num_gpus, rollout_num_gpus)`：因为两个角色共用同一批卡，所以只要其中较大的一方就够了（通常相等）。`offset = 0` 是 colocate 的灵魂——它让 actor 和 rollout 指向同一段 bundle。
- 默认分支（第 117 行）：训练卡和推理卡**相加**，offset 等于训练卡数，于是训练用前半、推理用后半，物理上完全分离，互不干扰。

参数侧也要联动理解：`--colocate` 开启时，如果用户没显式指定 `--rollout-num-gpus`，它会被自动设为 `actor_num_gpus_per_node * actor_num_nodes`（见 [slime/utils/arguments.py:1887-1888](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1887-L1888)），保证 `max()` 两边相等。而 `--rollout-num-gpus` 的默认值是 `None`（见 [slime/utils/arguments.py:44-54](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L44-L54)）。

#### 4.2.4 代码实践

**实践目标**：给定具体参数，手算 `num_gpus` 与 `rollout_offset`，并用真实函数验证。

**已知条件**：`actor-num-nodes=1`，`actor-num-gpus-per-node=4`，`rollout-num-gpus=4`。

**操作步骤（手算）**：

1. 先算 `actor_num_gpus = 1 × 4 = 4`。
2. **普通分离模式**：走默认分支 → `num_gpus = 4 + 4 = 8`，`rollout_offset = 4`。
   - actor 的卡：bundle 编号 `[0,1,2,3]`
   - rollout 的卡：bundle 编号 `[4,5,6,7]`
   - 共需 **8 张 GPU**，两段不重叠。
3. **colocate 模式**：走 colocate 分支 → `num_gpus = max(4,4) = 4`，`rollout_offset = 0`。
   - actor 的卡：bundle 编号 `[0,1,2,3]`
   - rollout 的卡：bundle 编号 `[0,1,2,3]`（**与 actor 完全相同**）
   - 共需 **4 张 GPU**，两段重叠。

**预期结果表**：

| 模式 | num_gpus | rollout_offset | actor bundle | rollout bundle | 总占用 GPU |
|------|----------|----------------|--------------|----------------|-----------|
| 普通分离 | 8 | 4 | [0,1,2,3] | [4,5,6,7] | 8 |
| colocate | 4 | 0 | [0,1,2,3] | [0,1,2,3] | 4 |

**可选的运行验证**（如果你的环境装了 `ray`，无需 GPU）：用 `SimpleNamespace` 构造假 args，调用真实函数确认手算结果。

```python
# 示例代码：验证 _get_placement_group_layout 的输出（需能 import ray）
from types import SimpleNamespace
from slime.ray.placement_group import _get_placement_group_layout

def make(debug_train_only=False, rollout_external=False,
         debug_rollout_only=False, colocate=False):
    return SimpleNamespace(
        actor_num_nodes=1, actor_num_gpus_per_node=4, rollout_num_gpus=4,
        debug_train_only=debug_train_only, rollout_external=rollout_external,
        debug_rollout_only=debug_rollout_only, colocate=colocate,
    )

print(_get_placement_group_layout(make()))                 # 普通分离 → (8, 4)
print(_get_placement_group_layout(make(colocate=True)))    # colocate   → (4, 0)
```

> 说明：`_get_placement_group_layout` 本身是纯逻辑函数（只读 args 属性、做算术），不调用 Ray API，所以可在纯 CPU 环境验证；但 `import slime.ray.placement_group` 时模块顶部 `import ray` 必须成功。若环境无 ray，以手算结果为准。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：若 `actor-num-nodes=2, actor-num-gpus-per-node=4, rollout-num-gpus=8`，普通分离模式下 `num_gpus` 和 `rollout_offset` 各是多少？

**参考答案**：`actor_num_gpus = 2 × 4 = 8`；`num_gpus = 8 + 8 = 16`，`rollout_offset = 8`。训练用 bundle `[0..7]`，推理用 `[8..15]`。

**练习 2**：`rollout_external` 模式下，为什么 `rollout_offset` 等于 `actor_num_gpus` 而不是 `0`？

**参考答案**：因为推理引擎在外部集群，本地不需要为 rollout 分配任何 GPU。让 `rollout_offset = actor_num_gpus` 能使 rollout 切片 `[actor_num_gpus:]` 恰好为空，从结果上明确表达「本地 0 张推理卡」。如果设成 0，rollout 会错误地分到训练卡。

---

### 4.3 真正预约 GPU 并探测物理编号：_create_placement_group 与 InfoActor

#### 4.3.1 概念说明

`_get_placement_group_layout` 只是算出了「要几张卡」，真正向 Ray 预约并弄清每张卡「物理上是哪一块」的是 `_create_placement_group`。

这里有一个微妙但关键的问题：Ray 的调度器把 bundle 放到 GPU 上时，**顺序是不确定的**——它可能先放节点 B 的 GPU 3，再放节点 A 的 GPU 1。而 Megatron 训练对 rank 与 GPU 的对应关系有隐含假设（比如 rank 0 通常期望是 master、且节点内 GPU 编号连续）。如果直接用 Ray 给的原始顺序，rank 与物理 GPU 的映射就会乱跳，跨实验不可复现。

所以 slime 做了两件事：

1. **用 `InfoActor` 探测**：在每个 bundle 上临时放一个占用 1 张 GPU 的小 actor，问它「你在哪台机器、哪块 GPU」，从而把每个 bundle 的物理位置探测出来。
2. **重排**：按 `(节点 IP, GPU 编号)` 排序所有 bundle，得到一个稳定、确定的顺序，让逻辑 rank 0 对应最低编号的节点和 GPU。

#### 4.3.2 核心流程

```
_create_placement_group(num_gpus):
  1. bundles = [{"GPU":1, "CPU":1}] * num_gpus     # 每个 bundle = 1 GPU + 1 CPU
  2. pg = placement_group(bundles, strategy="PACK") # 原子预约，尽量挤同机
  3. 轮询等待 pg 就绪（每 30s 打一行日志，避免静默挂死）
  4. 对每个 bundle i：
       起一个 InfoActor，强制调度到 bundle i（占用该 bundle 的 1 GPU）
       问它 get_ip_and_gpu_id() → 得到 (节点 IP, 物理 GPU 编号)
       立即 kill 掉这个临时 actor
  5. 把所有 (i, 节点IP, GPU编号) 按 (节点IP, GPU编号) 排序
  6. 返回 (pg, 重排后的 bundle 索引列表, 重排后的物理 GPU 编号列表)
```

InfoActor 是这个探测的核心工具。它被声明为 `@ray.remote(num_gpus=1)`——**为什么要占 1 张 GPU**？因为只有声明占用 GPU，Ray 才会真的给它分配一块物理 GPU，`ray.get_gpu_ids()` 才会返回有效的物理编号；如果 `num_gpus=0`，它看不到任何 GPU，探测就失败。它的方法 `get_ip_and_gpu_id` 返回节点 IP 和 GPU 编号，是整个物理映射的来源。

#### 4.3.3 源码精读

**InfoActor 定义**——这个临时探针：

[slime/ray/placement_group.py:15-18](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L15-L18) —— 占用 1 GPU，报告自己的节点 IP 与 GPU 编号。

```python
@ray.remote(num_gpus=1)
class InfoActor:
    def get_ip_and_gpu_id(self):
        return ray.util.get_node_ip_address(), ray.get_gpu_ids()[0]
```

**预约 + 探测 + 重排的完整实现**：

[slime/ray/placement_group.py:42-97](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L42-L97) —— `_create_placement_group` 全文。分段看：

预约部分（[第 47–48 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L47-L48)）—— bundle 规格是 `{"GPU": 1, "CPU": 1}`，策略 `PACK`：

```python
bundles = [{"GPU": 1, "CPU": 1} for _ in range(num_gpus)]
pg = placement_group(bundles, strategy="PACK")
```

等待部分（[第 57–67 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L57-L67)）—— 用轮询 `ray.wait(..., timeout=30)` 而不是裸 `ray.get(pg.ready())`：

```python
ready_ref = pg.ready()
elapsed = 0
log_interval = 30
while not ray.wait([ready_ref], timeout=log_interval)[0]:
    elapsed += log_interval
    total = ray.cluster_resources().get("GPU", 0)
    available = ray.available_resources().get("GPU", 0)
    logger.info(f"Waiting for placement group of {num_gpus} GPUs (elapsed {elapsed}s): ...")
```

这是一个很值得学习的工程细节：裸 `ray.get(pg.ready())` 在集群 GPU 还没注册或 autoscaler 正在拉起节点时会**静默挂死**，没有任何输出。改成每 30 秒轮询并打印「集群已注册多少 GPU、还剩多少可用」，既保持无限等待（autoscaler 集群正是靠 pending 的 placement group 触发扩容），又让挂起变得可观测。

探测部分（[第 70–82 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L70-L82)）—— 给每个 bundle 起一个 InfoActor，强制塞进对应 bundle，问完即 kill：

```python
info_actors = []
for i in range(num_bundles):
    info_actors.append(
        InfoActor.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                placement_group=pg,
                placement_group_bundle_index=i,   # 强制塞进第 i 个 bundle
            ),
        ).remote()
    )
gpu_ids = ray.get([actor.get_ip_and_gpu_id.remote() for actor in info_actors])
for actor in info_actors:
    ray.kill(actor)   # 探测完立即释放
```

重排部分（[第 84–88 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L84-L88)）—— 按节点 IP + GPU 编号排序，得到稳定的「逻辑编号 → 物理 GPU」映射：

```python
bundle_infos = [(i, gpu_ids[i][0], gpu_ids[i][1]) for i in range(num_bundles)]
sorted_bundle_infos = sorted(bundle_infos, key=sort_key)
pg_reordered_bundle_indices = [info[0] for info in sorted_bundle_infos]
pg_reordered_gpu_ids = [gpu_ids[info[0]][1] for info in sorted_bundle_infos]
```

排序键 `sort_key`（[第 21–39 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L21-L39)）把节点标识解析成可比较的数值：先尝试当 IP 地址解析（`"10.0.0.1" → [10,0,0,1]`），失败则解析主机名，再失败则取每个字符的 ASCII 值，保证无论节点用 IP、主机名还是任意字符串标识都能稳定排序。

补充：`slime/ray/utils.py` 里的 [get_physical_gpu_id](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/utils.py#L40-L43) 是另一种基于 `torch.cuda` 获取 GPU UUID 的方法，供需要 UUID（而非逻辑编号）的场景使用（如 NCCL 通信引导）；它与 InfoActor 的探测互补。

#### 4.3.4 代码实践

**实践目标**：理解 InfoActor「占 1 GPU 才能探测」的设计，以及重排如何让映射稳定。

**操作步骤**：

1. 打开 [slime/ray/placement_group.py:15-18](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L15-L18)，把 `@ray.remote(num_gpus=1)` 想象成改成 `@ray.remote(num_gpus=0)`，推理 `ray.get_gpu_ids()` 会返回什么。
2. 追踪 `_create_placement_group` 的数据流：`gpu_ids` 是一个 list，每个元素是 `(ip, gpu_id)`；`bundle_infos` 把它和下标 `i` 绑定；排序后 `pg_reordered_gpu_ids` 是按「节点+GPU」排好序的物理 GPU 编号列表。
3. 思考：假设 Ray 把 4 个 bundle 乱序放到 `[节点B-gpu3, 节点A-gpu1, 节点A-gpu0, 节点B-gpu2]`，重排后逻辑 rank 0 对应哪块 GPU？

**需要观察的现象 / 预期结果**：

- `num_gpus=0` 时 InfoActor 看不到 GPU，`get_gpu_ids()` 返回空 list，`[0]` 会抛 `IndexError`——所以必须占 1 GPU。这正是它写 `num_gpus=1` 的原因。
- 乱序示例重排后：节点A-gpu0 → 逻辑 rank 0，节点A-gpu1 → rank 1，节点B-gpu2 → rank 2，节点B-gpu3 → rank 3。即先按节点、再按 GPU 升序，rank 0 一定落在最低编号节点的最低编号 GPU 上。

> 这是源码阅读型实践，无需运行集群。结论由 `sort_key` 的排序语义决定，可直接从源码推出。**待本地验证**（若要在真实 Ray 集群观察物理编号，需多卡环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么等待 placement group 就绪用「每 30 秒轮询打日志」而不是 `ray.get(pg.ready())`？

**参考答案**：裸 `ray.get(pg.ready())` 在 GPU 未注册或 autoscaler 扩容时会静默挂死、无任何输出，难以区分「还在等」和「卡死了」。轮询 + 周期日志让等待可观测，同时保持无限等待（autoscaler 集群需要 pending 的 placement group 来触发节点扩容）。

**练习 2**：InfoActor 为什么声明 `num_gpus=1`？声明 `num_cpus=1` 能不能探测到 GPU？

**参考答案**：只有声明占用 GPU，Ray 才会给它分配物理 GPU，`ray.get_gpu_ids()` 才返回有效编号。只声明 CPU 不会分配 GPU，探测失败。`num_gpus=1` 是「为了看到 GPU 而必须占住它」。

---

### 4.4 colocate 的资源代价：needs_offload

#### 4.4.1 概念说明

前面看到 colocate 模式下 `rollout_offset = 0`，actor 和 rollout 指向**同一批物理 GPU**。这省卡，但带来一个不可避免的代价：同一块 GPU 的显存不能同时被训练（Megatron）和推理（SGLang）完全占满，否则 OOM。

所以 colocate 必须让两个角色**轮流让出显存**：训练时，推理引擎把显存让出来（offload/release）；推理时，训练工人把显存让出来。slime 用一个布尔标志 `needs_offload` 来标记「这一组推理引擎是否和训练 GPU 重叠」——只有重叠的引擎组才需要在训练阶段释放显存，不重叠的（分离部署的推理卡）就不必折腾。

这个标志不在 `placement_group.py` 里，而在消费 placement group 的 `rollout.py` 里计算——它是「共卡」这一资源决策的**直接下游后果**，理解它才能把资源分配和 u1-l6 讲过的 offload/onload 来回切换闭环。

#### 4.4.2 核心流程

```
# rollout 侧（slime/ray/rollout.py）在启动每组推理引擎时：
rollout_pg_offset = _compute_rollout_offset(args)   # 同一个 offset：colocate/debug 时为 0，否则为 actor 卡数
megatron_num_gpus = _compute_megatron_num_gpus(args) # 训练占用的 GPU 槽位数

对每一组引擎 group：
    group_abs_start = rollout_pg_offset + gpu_offset            # 这组引擎在 PG 里的绝对起始槽位
    needs_offload = (offload_rollout 开启) 且 (group_abs_start < megatron_num_gpus)
        # 即：这组引擎的起始位置落在「训练 GPU 区间」内 → 和训练重叠 → 需要 offload
```

判断逻辑很直观：如果一组推理引擎的起始 GPU 槽位 `group_abs_start` 小于训练占用的 GPU 总数 `megatron_num_gpus`，说明它落在训练区里，二者重叠，必须 `needs_offload = True`。

#### 4.4.3 源码精读

**offset 在 rollout 侧的重新计算**——和 `_get_placement_group_layout` 完全一致，保证两边对齐：

[slime/ray/rollout.py:1076-1081](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1076-L1081) —— colocate/debug 时返回 0（重叠），否则返回训练卡数。

```python
def _compute_rollout_offset(args) -> int:
    if args.debug_train_only or args.debug_rollout_only or args.colocate:
        return 0
    offset = args.actor_num_nodes * args.actor_num_gpus_per_node
    return offset
```

[slime/ray/rollout.py:1084-1089](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1084-L1089) 同理算出训练占用的槽位数（debug_rollout_only 时为 0）。

**needs_offload 的判定**——这正是 colocate 共卡的资源后果：

[slime/ray/rollout.py:1142-1143](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1142-L1143) —— 起始槽位落在训练区间内即判定为需要 offload。

```python
group_abs_start = rollout_pg_offset + gpu_offset
needs_offload = args.offload_rollout and group_abs_start < megatron_num_gpus
```

这个 `needs_offload` 随后被写进每个 `ServerGroup`（[第 1165 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1165)），并直接控制引擎组在训练阶段的行为：

[slime/ray/rollout.py:262-270](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L262-L270) —— `offload()` 方法：只有 `needs_offload=True` 的引擎组才会调用 `release_memory_occupation` 释放显存，不重叠的组直接跳过。

```python
def offload(self):
    if not self.needs_offload:
        return []
    return [engine.release_memory_occupation.remote() for engine in self.engines if engine is not None]
```

`onload()`（[第 272–279 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L272-L279)）同理，只有 `needs_offload=True` 的组才恢复显存。这就把 u1-l6 里讲的「colocate 靠 offload_rollout/offload_train 轮流让出显存」落实到了精确的、按引擎组判定的代码上。

另一个有意思的后果：对于 `needs_offload=False` 的分离推理卡，slime 会主动关掉 SGLang 的 memory_saver（[第 1148–1149 行](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1148-L1149)），因为这些卡独占、不需要省显存机制。这是「资源是否共享」一路影响到底层推理引擎配置的例子。

#### 4.4.4 代码实践

**实践目标**：把 colocate 的资源决策与 `needs_offload` 串起来，验证「共卡 → 必须 offload」的因果链。

**操作步骤**：

1. 设想两种部署（都用 `actor-num-nodes=1, actor-num-gpus-per-node=4, rollout-num-gpus=4`，并开启 `--offload-rollout`）：
   - **A. 普通分离**：`rollout_offset = 4`，`megatron_num_gpus = 4`。第一组推理引擎 `group_abs_start = 4`。
   - **B. colocate**：`rollout_offset = 0`，`megatron_num_gpus = 4`。第一组推理引擎 `group_abs_start = 0`。
2. 对每种部署，套用 `needs_offload = offload_rollout and group_abs_start < megatron_num_gpus` 判定推理引擎组是否需要 offload。
3. 打开 [slime/ray/rollout.py:262-270](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L262-L270) 确认：`needs_offload=False` 时 `offload()` 返回空 list，引擎组不会释放显存。

**需要观察的现象 / 预期结果**：

| 部署 | group_abs_start | megatron_num_gpus | needs_offload | 含义 |
|------|-----------------|-------------------|---------------|------|
| A 普通分离 | 4 | 4 | `4 < 4` → **False** | 推理卡与训练卡不重叠，无需释放显存 |
| B colocate | 0 | 4 | `0 < 4` → **True** | 推理与训练共卡，必须释放显存给训练 |

**结论**：colocate 把 `rollout_offset` 从 4 变成 0，直接导致推理引擎组的起始槽位落进训练区间，`needs_offload` 翻转为 True——这就是「共卡必然导致 offload」在代码里的完整因果链。

> 这是源码阅读 + 推理型实践，无需集群即可验证判定结果。

#### 4.4.5 小练习与答案

**练习 1**：如果用户既想 colocate 又不想 offload（设 `--no-offload-rollout`），会发生什么？

**参考答案**：从 `needs_offload = args.offload_rollout and ...` 可见，`offload_rollout=False` 时 `needs_offload` 恒为 False，推理引擎不会释放显存。但 colocate 下训练和推理共用同一批 GPU，两个模型都不让显存几乎必然 OOM。所以参数校验里 colocate 会强制把 `offload_rollout` 设为 True（见 [arguments.py:1885-1886](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1885-L1886)），`--no-offload-rollout` 会被忽略。

**练习 2**：为什么 `needs_offload` 是「按引擎组」判定，而不是「全局一个布尔」？

**参考答案**：因为 slime 支持异构拓扑（如 PD 分离、多组不同 TP 大小的引擎），不同引擎组可能落在 GPU 区间的不同位置——有的和训练重叠、有的不重叠。按组判定可以让不重叠的组跳过无谓的 offload、关掉 memory_saver，节省开销；只有真正重叠的组才付 offload 的代价。

## 5. 综合实践

**任务**：你拿到一个 2 节点、每节点 8 卡（共 16 GPU）的集群，要部署一个 slime 训练任务，参数为 `actor-num-nodes=2, actor-num-gpus-per-node=8, rollout-num-gpus=8`。请完成下面四问，把本讲的知识串起来。

1. **普通分离模式**：调用 `_get_placement_group_layout` 会返回什么？画出 actor / rollout 各自占用的 bundle 编号区间。如果每节点 8 卡，PACK 策略下这些 bundle 最可能如何分布在两台机器上？
2. **切到 colocate 模式**（其余参数不变）：返回值如何变化？总 GPU 占用从多少降到多少？actor 和 rollout 的 bundle 区间变成什么关系？
3. **物理探测**：在 colocate 模式下，`_create_placement_group` 会起几个 InfoActor？它们各自落在哪个 bundle 上？重排后逻辑 rank 0 大致对应什么位置的 GPU？
4. **offload 后果**：colocate 模式下开启 `--offload-rollout`，第一组推理引擎的 `group_abs_start` 是多少？`needs_offload` 是 True 还是 False？结合 train.py 的循环（u1-l6），说明这个 True 在「采样→训练」切换时触发了什么动作。

**参考答案要点**：

1. `actor_num_gpus = 2×8 = 16`；普通分离返回 `(16+8, 16) = (24, 16)`，但集群只有 16 卡——**放不下**，这是参数配错的信号（说明 colocate 才是合理选择）。若强行看区间：actor 用 `[0..15]`、rollout 用 `[16..23]`。PACK 下 24 个 bundle 会尽量挤进节点，但 16 卡集群无法满足。
2. colocate 返回 `(max(16,8), 0) = (16, 0)`。总占用从 24 降到 **16**（正好用满集群）。actor 用 `[0..15]`，rollout 用 `[0..15]`——**完全重叠**，即 16 卡被训练和推理时分复用。
3. colocate 下 `num_gpus = 16`，起 **16 个 InfoActor**，分别落在 bundle 0..15。重排后 rank 0 对应「最低编号节点的最低编号 GPU」（如节点 A 的 GPU 0）。
4. colocate 下 `rollout_offset = 0`，第一组引擎 `group_abs_start = 0`，`megatron_num_gpus = 16`，`0 < 16` → `needs_offload = True`。在 train.py 循环里，采样前 `rollout_manager.onload_weights()`/`onload_kv()` 把推理引擎显存恢复，采样后 `rollout_manager.offload()` 让推理引擎释放显存（因为 `needs_offload=True`），把 GPU 让给 Megatron 训练。

> 这个综合实践不需要真实集群即可完成推理；第 1 问的「放不下」结论是引导你体会「为什么生产中常用 colocate」的关键。**待本地验证**（真实多机部署）。

## 6. 本讲小结

- slime 用**一个** placement group 装下所有卡，靠 `rollout_offset` 把 bundle 列表切成 actor / rollout 两段，保证两类资源原子预约、PACK 挤在同机。
- `_get_placement_group_layout` 是分配大脑，用一组 `if/elif` 把普通分离 / colocate / debug_train_only / debug_rollout_only / rollout_external 五种模式收敛到 `(num_gpus, rollout_offset)` 二元组。
- colocate 的实现极其简洁：`rollout_offset = 0` 让 actor 和 rollout 指向**同一段** bundle 列表，即同一批物理 GPU。
- `InfoActor` 是临时探针：声明 `num_gpus=1` 强制占住一块物理 GPU，从而探测出每个 bundle 的 `(节点 IP, GPU 编号)`，用完即 kill。
- 探测结果按 `(节点, GPU)` 重排，得到稳定的「逻辑 rank → 物理 GPU」映射，保证跨实验可复现、rank 0 落在最低编号节点最低编号 GPU。
- 等待 placement group 用「每 30 秒轮询打日志」而非裸 `ray.get(pg.ready())`，让挂起可观测、兼容 autoscaler 扩容。
- colocate 共卡的直接代价是 `needs_offload`：推理引擎组起始槽位落进训练区间时必须轮流释放显存，否则 OOM；这个判定在 `rollout.py` 里按引擎组精确计算。

## 7. 下一步学习建议

本讲解决了「GPU 怎么分、谁用哪段」的资源编排问题，但还没有讲清楚 actor 和 rollout 拿到这些卡之后**暴露出什么接口**给上层调用。下一讲 **u2-l3 三大对象：actor / critic / rollout_manager** 将精读 `RayTrainGroup`（封装 `async_train` / `update_weights` / `save_model`）和 `RolloutManager`（封装 `generate` / `eval`），让你看清 `train.py` 里那些 `actor_model.xxx()` / `rollout_manager.xxx()` 调用背后的真实对象。

建议同时带着这两个问题继续读源码：
- `RayTrainGroup` 在 [actor_group.py:57-128](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L57-L128) 是如何把本讲得到的 bundle 索引用来调度每个训练工人的（`placement_group_bundle_index=reordered_bundle_indices[rank]`）。
- critic 复用 actor 的 placement group 后，`allocate_train_group` 里 `num_gpus_per_actor=0.4` 这种**分数 GPU** 是怎么让两个模型在同一批卡上共存的——这是连接本讲与训练后端（U4）的伏笔。
