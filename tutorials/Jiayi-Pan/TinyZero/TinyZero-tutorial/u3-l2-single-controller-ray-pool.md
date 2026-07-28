# Single Controller 与 Ray 资源池

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 veRL「单控制器（single-controller）」架构：为什么是一个 driver 进程负责编排、一堆 Ray actor 负责计算。
- 理解 `RayResourcePool` 如何把「每台机器几个进程」的抽象翻译成 Ray 的 `PlacementGroup`，以及 `max_colocate_count` 怎样决定一张 GPU 上能挤几个角色。
- 看懂 `RayClassWithInitArgs` 与 `RayWorkerGroup` 如何把一个普通 Python 类「远程化」成受 Ray 调度的 actor，并把方法绑定到 group 上。
- 掌握 `create_colocated_worker_cls` 的「合体术」：把 Actor / Critic / Ref 等多个角色塞进同一个进程（colocate），从而在一组 GPU 上复用显存。
- 能够对照 `main_ppo.py` 画出「三个 Role → 同一个 global_pool → 同一组 GPU」的资源映射图，并解释为什么 FSDP 后端要用 `max_colocate_count=1`。

## 2. 前置知识

本讲是「数据协议与单控制器」单元的第二篇，承接上一讲 [u3-l1 DataProto](u3-l1-dataproto-protocol.md) 里建立的心智模型：driver 进程手里拿着一个 `DataProto`「货箱」，需要把它分发给各 GPU 上的 worker 去算，再把结果收回。本讲回答的问题是：**这些 worker 是怎么被创建、被放置到 GPU 上的？driver 又是怎么「远程调用」它们的？**

需要先理解的几个基础概念：

- **Ray**：一个分布式计算框架。它把一个 Python 类用 `ray.remote` 包一下，就能让这个类的实例跑在集群任意一台机器的独立进程里（叫 **actor**），主进程拿到的是一个「远程句柄（actor handle）」，调用 `handle.method.remote(...)` 会返回一个未来对象（future），用 `ray.get(...)` 阻塞取回结果。
- **PlacementGroup（放置组）**：Ray 提供的资源预留机制。你声明「我需要 8 个 bundle，每个 bundle 含 1 GPU + 1 CPU」，Ray 就会在物理机上把这些资源先占好，之后创建的 actor 可以被「钉」到某个 bundle 上，保证它落到你想要的 GPU。
- **进程 / actor / rank**：本讲里一个 worker 就是一个 Ray actor，也就是一个独立进程；每个进程有一个全局 `RANK` 和一个机内 `LOCAL_RANK`，和 PyTorch DDP 的含义一致。
- **角色（Role）**：RLHF 训练里有多个不同用途的模型——Actor（要更新的策略）、Rollout（生成回答，常与 Actor 共享权重）、Critic（价值网络）、RefPolicy（冻结的参考策略，算 KL 用）、RewardModel（神经网络奖励模型）。veRL 用 `Role` 枚举来区分它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/single_controller/base/worker_group.py` | 基类层：抽象 `ResourcePool`（资源池描述）、`ClassWithInitArgs`（类的构造参数包装）、`WorkerGroup`（worker 组基类，负责把带 `@register` 的方法绑定到组上）。 |
| `verl/single_controller/base/worker.py` | 单个 worker 的基类 `Worker`：在 `__new__` 里读环境变量 `RANK`/`WG_PREFIX`，rank 0 负责选定 `MASTER_ADDR`/`MASTER_PORT` 并注册到「注册中心」。 |
| `verl/single_controller/ray/base.py` | **本讲主角**。Ray 实现层：`RayResourcePool`、`RayClassWithInitArgs`、`RayWorkerGroup`，以及合体函数 `create_colocated_worker_cls`。 |
| `verl/trainer/ppo/ray_trainer.py` | 训练侧的封装：`Role` 枚举、`ResourcePoolManager`（资源池管理器）、`RayPPOTrainer.init_workers`（把上面这些零件组装起来）。 |
| `verl/trainer/main_ppo.py` | 入口：在这里声明 `role_worker_mapping`、`resource_pool_spec`、`mapping` 三张表，决定「哪个角色用哪个池」。 |

## 4. 核心概念与源码讲解

### 4.1 单控制器架构：driver 编排 + Ray worker 计算

#### 4.1.1 概念说明

「单控制器」是相对「多控制器」而言的。在多控制器架构（比如传统 DDP 训练）里，每个 GPU 进程都跑一份完整的训练代码，进程之间通过 `torch.distributed` 互相通信、各自决定下一步做什么。而 veRL 采用的是 **单控制器**：

- **只有一个 driver 进程**跑着训练主循环（`RayPPOTrainer.fit`），它手里握着完整的 `DataProto`，知道全局有多少数据、要分给谁。
- **worker 只负责算**：每个 GPU 上有一个 worker actor，它不主动决策，只被动接收 driver 发来的数据、执行某个方法、把结果返回。

这样做的好处是：RLHF 的数据流非常复杂（生成 → 算 ref log prob → 算 value → 算 reward → 算 advantage → 更新 critic → 更新 actor），用一个 driver 串起来，逻辑清晰、易调试；而繁重的 GPU 计算（前向、反向、生成）仍分散在各 worker 上，不损失并行度。

driver 调用 worker 的方式不是裸写 `ray.get(actor.method.remote(...))`，而是通过 **WorkerGroup（worker 组）**：你调用 `wg.update_actor(data)`，group 内部自动完成「切数据 → 分发到各 actor → 收集结果」三步。

#### 4.1.2 核心流程

driver 调一个被 `@register` 标记的方法时，group 内部执行的是 `func_generator` 生成的包装函数：

```text
wg.update_actor(data)
   │
   ├─ 1. dispatch_fn(self, data)      # 把整个 DataProto 切成 world_size 份（或广播）
   ├─ 2. execute_fn("update_actor", 分片...)  # 对每个 actor 调 .update_actor.remote(分片)，拿到 future 列表
   ├─ 3. （若 blocking）ray.get(futures)       # 阻塞等所有 actor 算完
   └─ 4. collect_fn(self, 各 actor 输出)       # 把各 rank 的输出 concat 回一个 DataProto
```

这套「分发—执行—收集」的细节（dispatch/collect 具体怎么切）由方法上的 `@register(dispatch_mode=...)` 决定，是下一讲 [u3-l3 Dispatch 机制](u3-l3-dispatch-decorator.md) 的主题。本讲只需知道：**group 把它绑定的方法都包成了上面这个统一壳子**。

#### 4.1.3 源码精读

壳子本体就是 `func_generator`，它把 dispatch/execute/collect 四个步骤串起来：

[verl/single_controller/ray/base.py:36-46](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L36-L46) — `func_generator` 生成一个闭包 `func`，依次调用 dispatch → execute → （可选阻塞）→ collect。这就是所有 group 方法共同的执行骨架。

`execute_fn` 最终落到 `RayWorkerGroup.execute_all_async`，它真正对每个 worker 发起远程调用：

[verl/single_controller/ray/base.py:335-351](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L335-L351) — 关键逻辑：如果传入的 `args`/`kwargs` 全是长度等于 worker 数的 list，就把第 `i` 份发给第 `i` 个 worker（这就是「按 rank 切分」）；否则把同样的参数广播给所有 worker。最后一行的列表推导式 `[getattr(worker, method_name).remote(*args, **kwargs) for worker in self._workers]` 就是「对所有 actor 发起远程调用，返回 future 列表」。

而「把方法绑定到 group 上」这件事，发生在 `WorkerGroup._bind_worker_method`：

[verl/single_controller/base/worker_group.py:136-196](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker_group.py#L136-L196) — 它遍历 worker 类的所有方法，挑出带 `MAGIC_ATTR`（即被 `@register` 标记）的，读出该方法的 `dispatch_mode`/`execute_mode`/`blocking`，查表得到对应的 dispatch_fn / collect_fn / execute_fn，再用 `func_generator` 合成一个壳函数，`setattr` 到 group 实例上（方法名就是原名，如 `update_actor`）。这样 driver 才能写 `wg.update_actor(...)`。

#### 4.1.4 代码实践

**目标**：理解 group 方法的「分发—执行—收集」三段式，定位每一阶段对应的源码。

**步骤**：

1. 打开 `verl/single_controller/ray/base.py`，找到 `func_generator`（L36-L46），确认它的四行执行顺序。
2. 打开 `verl/single_controller/base/worker_group.py`，找到 `_bind_worker_method`（L136-L196），看它如何用 `MAGIC_ATTR` 过滤方法、如何查 `get_predefined_dispatch_fn` / `get_predefined_execute_fn`。
3. 回到 `base.py`，读 `execute_all_async`（L335-L351），找到「list 长度 == worker 数就按 rank 切分」的那段判断。

**需要观察的现象**：

- `_bind_worker_method` 里 `setattr(self, method_name, func)` 这一行——这就是为什么你没在 `RayWorkerGroup` 里看到 `update_actor` 的定义，却能在 driver 里直接调用它的原因：方法是**运行时动态绑上去**的。
- `execute_all_async` 里的两种分支：参数是「等长 list」时切分，否则广播。

**预期结果**：你能用一句话说清「driver 调 `wg.xxx(data)` 时，data 在哪一行被切、远程调用在哪一行发出、结果在哪一行被拼回」。

#### 4.1.5 小练习与答案

**练习 1**：如果某个方法希望「只让 rank 0 的 worker 执行，其余 worker 不动」，应该用哪种 execute 模式？提示：看 `Worker` 基类里被 `@register` 标记的方法。

**答案**：用 `Execute.RANK_ZERO`。参考 [verl/single_controller/base/worker.py:183-185](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker.py#L183-L185) 的 `execute_func_rank_zero`，它标注了 `execute_mode=Execute.RANK_ZERO`，对应 `RayWorkerGroup.execute_rank_zero_async`（只取 `self._workers[0]`）。

**练习 2**：`func_generator` 里的 `blocking` 参数控制什么？

**答案**：控制第 2 步执行后是否立刻 `ray.get` 阻塞等待所有 worker 算完。`blocking=True` 时调用者拿到的是最终结果；`blocking=False` 时拿到的是 future，调用者可以继续干别的、稍后再 `ray.get`。

---

### 4.2 RayResourcePool 与 PlacementGroup：GPU 资源怎么分

#### 4.2.1 概念说明

driver 想创建 worker 之前，必须先回答一个问题：**我要用哪些机器、每台机器起几个进程？** 这就是资源池（ResourcePool）要描述的事。

`ResourcePool`（基类）用一个非常朴素的字段 `process_on_nodes` 来描述资源——它就是一个 list，每个元素代表一台机器上要起几个进程。比如 `[8]` 表示「1 台机器、起 8 个进程」；`[4, 4]` 表示「2 台机器、各起 4 个进程」。`world_size` 就是这个 list 求和。

`RayResourcePool` 在此基础上做了一件事：**把这份抽象描述翻译成 Ray 的 PlacementGroup**。每个进程对应一个 bundle，bundle 里含 1 个 GPU 和若干 CPU。Ray 拿到这组 bundle 后，会按调度策略（默认 `STRICT_PACK`，即一台机器上的 bundle 必须整体打包、不能拆散到不同机器）把它们钉到物理资源上。

这里还有一个关键参数 `max_colocate_count`：它表示「同一个资源池里最多有几个 WorkerGroup（角色）共用一份资源」。后面会看到它直接决定了 `num_gpus = 1 / max_colocate_count`。

#### 4.2.2 核心流程

```text
process_on_nodes = [8]            # 用户描述：1 台机、8 进程
        │
        ▼  RayResourcePool.get_placement_groups(strategy="STRICT_PACK")
每个进程 → 1 个 bundle = {"GPU": 1, "CPU": max_colocate_count}
        │
        ▼  placement_group(bundles=[...]*8, strategy="STRICT_PACK")
8 个 PlacementGroup（这里每个 pg 含若干 bundle，取决于 _store 结构）
        │
        ▼  ray.get([pg.ready() ...])  # 阻塞等资源预留成功
返回 pgs，后续每个 actor 被钉到某个 pg 的某个 bundle_index 上
```

每个 actor 实际向 Ray 声称的 GPU 占用是：

\[
\text{num\_gpus\_per\_actor} = \frac{1}{\text{max\_colocate\_count}}
\]

- `max_colocate_count=1` 时，每个 actor 要一整张 GPU（`num_gpus=1.0`）。
- `max_colocate_count=3` 时，每个 actor 只要 1/3 张 GPU，于是同一张 GPU 上可以塞 3 个 actor（3 个角色各一个），这就是「分时共享一张卡」的基础。

#### 4.2.3 源码精读

先看基类 `ResourcePool`，它把资源描述简化到极致：

[verl/single_controller/base/worker_group.py:26-57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker_group.py#L26-L57) — `_store = process_on_nodes` 保存「每节点进程数」list；`world_size` 就是 `sum(self._store)`；`local_world_size_list` / `local_rank_list` 把它展开成每个进程的 `LOCAL_WORLD_SIZE` / `LOCAL_RANK`，供后面给 actor 设环境变量用。

再看 `RayResourcePool` 如何把它变成 PlacementGroup：

[verl/single_controller/ray/base.py:49-62](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L49-L62) — `RayResourcePool.__init__` 在基类基础上多了 `use_gpu`、`name_prefix`、`pgs`（缓存的 PlacementGroup 列表）。

[verl/single_controller/ray/base.py:64-88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L64-L88) — `get_placement_groups`：构造 `pg_scheme`，每个进程生成一个 bundle `{"CPU": self.max_collocate_count, "GPU": 1}`（`use_gpu=False` 时只有 CPU）；对每个节点用 `placement_group(...)` 建组，策略默认 `STRICT_PACK`；最后 `ray.get([pg.ready() ...])` 阻塞等待全部就绪，并缓存到 `self.pgs`。

> 注意一个命名小坑：构造参数叫 `max_colocate_count`（单个 l），而内部存储的属性名是 `max_collocate_count`（两个 l，见基类 L32）。两者指同一个东西，源码里两种拼写混用，阅读时心里有数即可。

`num_gpus` 的计算发生在 `RayWorkerGroup._init_with_resource_pool`：

[verl/single_controller/ray/base.py:214-264](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L214-L264) — 第 224 行 `num_gpus = 1 / resource_pool.max_collocate_count`，这就是上面公式的落点；每个 actor 创建时带着这个 `num_gpus`（L259-L262），向 Ray 声明自己占用多少 GPU。

#### 4.2.4 代码实践

**目标**：搞清 `process_on_nodes` 与最终 PlacementGroup 数量、bundle 内容的对应关系。

**步骤**：

1. 在 `RayResourcePool.get_placement_groups`（L64-L88）里找到构造 `pg_scheme` 的双层列表推导，理解「外层遍历 `self._store`（每台机器的进程数），内层按进程数生成 bundle」。
2. 假设 `process_on_nodes = [4, 4]`、`use_gpu=True`、`max_colocate_count=1`，手算：会创建几个 `placement_group`？每个含几个 bundle？每个 bundle 的 GPU/CPU 各是多少？

**需要观察的现象**：`pg_scheme` 是「按节点分组」的，外层每个 `process_count` 对应一个 pg（即一台机器一个 pg）。

**预期结果**：`[4, 4]` → 2 个 PlacementGroup，每个含 4 个 bundle，每个 bundle = `{GPU:1, CPU:1}`；`world_size = 8`。

**待本地验证**：如果有 Ray 环境，可写一段 `rp = RayResourcePool(process_on_nodes=[2], max_colocate_count=1); pgs = rp.get_placement_groups()`，打印 `[pg.bundle_count for pg in pgs]` 与 `[(b) for pg in pgs for b in pg.bundle_specs]` 对照手算结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_placement_groups` 里要 `ray.get([pg.ready() for pg in pgs])`？

**答案**：`placement_group(...)` 是异步的，创建后不保证资源已经预留到位。`pg.ready()` 返回一个未来对象，`ray.get` 会阻塞直到所有 pg 都「就绪（READY）」，确保后续创建 actor 时资源确实可用，否则 actor 可能因资源不足而调度失败。

**练习 2**：若把 `max_colocate_count` 从 1 调到 3，每个 actor 的 `num_gpus` 变成多少？一张物理 GPU 上最多能放几个 actor？

**答案**：`num_gpus = 1/3`；一张 GPU 上最多放 3 个 actor（3 × 1/3 = 1）。这正是让多个角色「分时共享」同一张卡的手段。

---

### 4.3 RayClassWithInitArgs 与 RayWorkerGroup：把类变成受管 actor

#### 4.3.1 概念说明

光有资源池还不够，driver 还需要一种方式说：「请用**这个类**、**这组构造参数**，在资源池对应的每个 GPU 上各创建一个实例。」这就是 `RayClassWithInitArgs` 与 `RayWorkerGroup` 的职责。

- `RayClassWithInitArgs`：一个「类 + 构造参数」的包装。它本身不创建实例，只是把 `cls`、`args`、`kwargs` 攒在一起，等到被调用时再用 `cls.options(scheduling_strategy=...).remote(*args, **kwargs)` 真正创建一个 Ray actor，并把它钉到指定的 PlacementGroup bundle 上。
- `RayWorkerGroup`：拿着一个 `RayResourcePool` 和一个 `RayClassWithInitArgs`，**遍历资源池里的每个进程槽位，逐个创建 actor**，维护 `self._workers` 列表与 `self._worker_names`。同时它继承自 `WorkerGroup`，会在创建完成后调用 `_bind_worker_method`，把 worker 类上所有带 `@register` 的方法动态绑到自己身上（见 4.1）。

每个 actor 还需要在创建时拿到「我是哪个 rank、master 在哪」，这样 worker 内部才能建立起 PyTorch DDP 通信。这套信息通过**环境变量**传入（`WORLD_SIZE`/`RANK`/`LOCAL_RANK`/`MASTER_ADDR`/`MASTER_PORT`），而 master 地址端口则由 rank 0 的 worker 选定并注册到一个「注册中心 actor」里，其余 rank 再去取。

#### 4.3.2 核心流程

```text
RayWorkerGroup(resource_pool, ray_cls_with_init)
   │
   ├─ _init_with_resource_pool:
   │     pgs = resource_pool.get_placement_groups()   # 拿到 PlacementGroup
   │     num_gpus = 1 / max_colocate_count
   │     for 每个节点(pg_idx), 每个机内进程(local_rank):
   │         rank += 1
   │         设环境变量 WORLD_SIZE/RANK/LOCAL_RANK/WG_PREFIX...
   │         worker = ray_cls_with_init(pg, bundle_idx=local_rank, num_gpus)  # 创建 actor
   │         if rank == 0: 从注册中心取回 MASTER_ADDR/MASTER_PORT
   │     self._workers = [所有 actor 句柄]
   │
   └─ _bind_worker_method(cls, func_generator)   # 把 @register 方法绑到 group 上
```

其中每个 actor 内部（即 `Worker.__new__`）会读环境变量，rank 0 选定 master 地址端口并写进「注册中心」，其余 rank 等待 driver 把 master 信息取回再注入环境变量。

#### 4.3.3 源码精读

`RayClassWithInitArgs.__call__` 是「真正创建 actor」的地方：

[verl/single_controller/ray/base.py:142-173](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L142-L173) — 它构造 `PlacementGroupSchedulingStrategy(placement_group=..., placement_group_bundle_index=...)`，把 actor 钉到指定 bundle；若 `use_gpu` 则设 `num_gpus`；最后 `self.cls.options(**options).remote(*self.args, **self.kwargs)` 真正发起创建。另有一条 `sharing_with` 分支（L148-L154）用于「和别人共用同一个节点」，是 colocate 的另一条路径。

`RayWorkerGroup.__init__` 决定走「新建」还是「复用已有 detached worker」：

[verl/single_controller/ray/base.py:178-203](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L178-L203) — 正常路径调用 `_init_with_resource_pool`，并在最后（L202-L203）调用 `_bind_worker_method` 完成方法绑定。

核心创建循环在 `_init_with_resource_pool`：

[verl/single_controller/ray/base.py:214-278](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L214-L278) — 逐 pg、逐 local_rank 创建 actor。L235-L242 给每个 actor 注入 `WORLD_SIZE`/`RANK`/`RAY_LOCAL_WORLD_SIZE`/`RAY_LOCAL_RANK` 等环境变量；非 rank 0 还会带上 `MASTER_ADDR`/`MASTER_PORT`。L259-L262 创建 actor。L266-L276 是 rank 0 的特殊处理：循环等待名为 `{name_prefix}_register_center` 的注册中心 actor 出现，再从中取回 `MASTER_ADDR`/`MASTER_PORT`（这正是 4.1 里「driver 编排」的一部分——master 地址是 worker 们自己协商出来的）。

注册中心的写入端在 `Worker` 基类：

[verl/single_controller/base/worker.py:85-117](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker.py#L85-L117) — `__new__` 里读 `RANK`/`WG_PREFIX`；`_configure_before_init` 中 rank 0 调 `get_availale_master_addr_port` 选定地址端口，并通过 `create_worker_group_register_center` 注册出去（L112-L115）。注意 L89-L91：当 `DISABLE_WORKER_INIT=1` 时直接返回——这个开关在 4.4 的 colocate 场景下会被打开（合体时内部子 worker 不再重复初始化）。

#### 4.3.4 代码实践

**目标**：跟踪一个 actor 从「类 + 参数」到「带 rank 信息的远程实例」的完整创建链。

**步骤**：

1. 从 `RayWorkerGroup.__init__`（L178）出发，跟着 `_init_with_resource_pool`（L214）走一遍双层循环，确认外层是 `pg_idx`（节点）、内层是 `local_rank`（机内进程）。
2. 找到给 actor 注入环境变量的字典（L235-L242），列出它注入了哪些变量。
3. 读 `Worker.__new__` / `_configure_before_init`（worker.py L85-L117），找到「rank 0 选 master 地址端口并写注册中心」的那几行。

**需要观察的现象**：

- 每个 actor 的名字（L251）形如 `{name_prefix}{cia_name}_{pg_idx}:{local_rank}`，例如 `abcde3ActorRolloutRefWorker_0:3`，可在 Ray dashboard 里按这个名字定位进程。
- rank 0 必须等注册中心出现才能拿到 master 信息，所以 L268 有一个 120 次的重试循环——这隐含了一个约束：**rank 0 的 worker 必须先成功启动并注册，其余 rank 才能继续**。

**预期结果**：你能解释「环境变量是怎么从 driver 流到 worker 进程的，master 地址端口又是怎么在 worker 之间协商的」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_init_with_resource_pool` 里 rank 0 不设 `MASTER_ADDR`/`MASTER_PORT` 环境变量，而非 rank 0 反而要设？

**答案**：rank 0 是 master 地址端口的**选定者**——它在自己的 `__new__` 里调 `get_availale_master_addr_port` 选好并注册到注册中心（worker.py L105-L115），不需要预先注入。其余 rank（非 0）需要知道 master 在哪才能连过去，所以由 driver 从注册中心取回后注入它们的环境变量（base.py L243-L245）。

**练习 2**：`RayWorkerGroup.from_detached`（L285-L290）与正常构造的区别是什么？

**答案**：`from_detached` 不创建新 actor，而是用一组**已存在的 detached actor 名字**（`worker_names`）通过 `ray.get_actor(name=...)` 重新拿到句柄，组装成一个 group。它用于「多个逻辑 worker group 共享同一批物理 actor」——这正是 4.4 colocate 里 `spawn` 的底层机制。

---

### 4.4 create_colocated_worker_cls 与 colocate：多角色合体进同一进程

#### 4.4.1 概念说明

RLHF 一次训练同时涉及 Actor、Critic、Ref（甚至 RewardModel）多个模型。如果给每个角色各开一组独占 GPU 的 worker，4 个角色就要 4 倍的卡——太浪费。veRL 的解法是 **colocate（同址）**：把这些角色**塞进同一个进程**，让它们共用同一份 GPU 显存与同一个进程上下文。

实现 colocate 的核心函数是 `create_colocated_worker_cls(class_dict)`。它接收一个字典 `{key: RayClassWithInitArgs}`（例如 `{'actor_rollout':..., 'critic':..., 'ref':...}`），动态合成一个新的类 `WorkerDict`：

- `WorkerDict.__init__` 会在**同一个进程内**逐个实例化字典里的每个角色（用 `DISABLE_WORKER_INIT=1` 跳过重复的 rank 初始化）。
- 用 monkey-patch 把每个角色的方法重命名为 `{key}_{method}`（如 `actor_rollout_update_actor`、`critic_update_critic`），并保留方法上的 `@register` 标记。
- 最后把这个 `WorkerDict` 也包成 `RayClassWithInitArgs` 返回，于是它可以像普通 worker 一样被 `RayWorkerGroup` 拉起——**每个 GPU 上只起一个 actor，但这个 actor 内部同时住着多个角色**。

但训练代码（`ray_trainer`）希望仍然用 `actor_rollout_wg.update_actor(...)`、`critic_wg.update_values(...)` 这样的「分角色」接口来调用。于是 `RayWorkerGroup.spawn` 把这一组带前缀的方法再「拆」成多个逻辑 group：每个逻辑 group（如 `all_wg['critic']`）把 `critic_xxx` 重绑回 `xxx`，于是 `critic_wg.update_critic(...)` 实际路由到共享 actor 内部的 critic 子对象。**物理上是一组 actor，逻辑上是多个 worker group 视图。**

`max_colocate_count` 在这里扮演的角色很微妙，值得单独讲清（见 4.4.4 实践）：它决定了 `num_gpus = 1/max_colocate_count`。FSDP 后端推荐 `max_colocate_count=1`——每个角色不靠「切分一张卡」来共享，而是靠 `create_colocated_worker_cls` **合并成同一个进程**来共享。

#### 4.4.2 核心流程

```text
class_dict = {'actor_rollout': RCIW(ActorRolloutRefWorker, role='actor_rollout'),
              'critic':        RCIW(CriticWorker),
              'ref':           RCIW(ActorRolloutRefWorker, role='ref')}
        │
        ▼  create_colocated_worker_cls(class_dict)
合成 WorkerDict 类：
   __init__: 在同一进程内 new 出 actor_rollout / critic / ref 三个子对象
   方法被改名为 actor_rollout_init_model / critic_init_model / ref_init_model ...
        │
        ▼  RayWorkerGroup(resource_pool=global_pool, ray_cls_with_init=WorkerDict)
每个 GPU 起 1 个 actor（num_gpus=1），每个 actor 内部住着 3 个角色
        │
        ▼  wg_dict.spawn(prefix_set={'actor_rollout','critic','ref'})
拆成 3 个逻辑 group：all_wg['actor_rollout'] / all_wg['critic'] / all_wg['ref']
三者指向同一批物理 actor，但各自暴露「去掉前缀」的方法名
```

#### 4.4.3 源码精读

合体函数本体：

[verl/single_controller/ray/base.py:420-459](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L420-L459) — `create_colocated_worker_cls`：先校验所有角色的基类相同（L429-L433，因为要共享同一个 `WorkerDict` 基类）；定义内部类 `WorkerDict(worker_cls)`，其 `__init__`（L442-L450）在 `patch.dict(os.environ, {'DISABLE_WORKER_INIT': '1'})` 下逐个实例化各角色（这就是「合体」落点，`DISABLE_WORKER_INIT` 避免子对象重复跑 rank 协商）；然后用 `_bind_workers_method_to_parent` 给 `WorkerDict` 打方法补丁；最后 `ray.remote(WorkerDict)` 再包一层 `RayClassWithInitArgs` 返回。

方法改名与标记保留：

[verl/single_controller/ray/base.py:380-411](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L380-L411) — `_bind_workers_method_to_parent`：遍历每个角色的方法，只挑带 `MAGIC_ATTR` 的；生成一个 `func`，其内部 `return getattr(self.worker_dict[key], name)(*args, **kwargs)`——即「调用转发到对应子对象」；方法名设为 `{key}_{method_name}`（L407），并把原方法的 `MAGIC_ATTR` 复制过来（L405，这样外层 group 的 `_bind_worker_method` 才认得它）。

逻辑拆分：

[verl/single_controller/ray/base.py:292-317](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L292-L317) — `spawn(prefix_set)`：对每个 prefix，用 `from_detached`（复用同一批 actor 句柄）建一个新 group，再用 `_rebind_actor_methods` 把 `{prefix}_xxx` 改回 `xxx`。结果是一个 dict，如 `{'actor_rollout': wg, 'critic': wg, 'ref': wg}`，三个 wg 指向同一批 actor。

训练侧的组装在 `RayPPOTrainer.init_workers`：

[verl/trainer/ppo/ray_trainer.py:486-514](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L486-L514) — 先按角色收集 `resource_pool_to_cls`（因为三个角色都映射到 `global_pool`，所以这个池子下聚集了 `{'actor_rollout', 'critic', 'ref'}` 三个类）；L492-L493 对每个池子调 `create_colocated_worker_cls` 合成一个 `WorkerDict`；L494 用它建一个 `RayWorkerGroup`（每个 GPU 一个合体 actor）；L495 `spawn` 拆成多个逻辑 group；L500-L514 依次 `init_model`，注意 **actor_rollout 放在最后**（L512-L514 注释说明：为了让 vLLM 更准确地估算 KV cache 显存——前面先把 critic/ref 的显存占掉，剩下的才留给 rollout 的 vLLM）。

而「三个角色都映射到 global_pool」这件事，声明在入口 `main_ppo.py`：

[verl/trainer/main_ppo.py:142-156](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L142-L156) — `role_worker_mapping` 把每个 `Role` 映射到具体的 worker 类；`resource_pool_spec` 声明只有一个 `global_pool`，大小是 `[n_gpus_per_node] * nnodes`；`mapping` 把 `ActorRollout`/`Critic`/`RefPolicy` 三个角色**都**指向 `global_pool`。这三张表是 colocate 的「接线图」。

资源池管理器把它们串起来：

[verl/trainer/ppo/ray_trainer.py:54-77](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L54-L77) — `ResourcePoolManager.create_resource_pool` 遍历 `resource_pool_spec` 建 `RayResourcePool`，注意 L71 硬编码 `max_colocate_count=1`（并带注释说明 FSDP 用 1、Megatron 用 >1）；`get_resource_pool(role)` 通过 `mapping[role]` 查到该角色对应的池子。

#### 4.4.4 代码实践

**目标**：理解「物理 actor 合体」与「逻辑 group 拆分」两层映射，并说清 `max_colocate_count=1` 对 FSDP 的意义。

**步骤**：

1. 在 `create_colocated_worker_cls`（L420-L459）里找到 `WorkerDict.__init__`，确认它在一个进程内实例化了 `class_dict` 里的**所有**角色（而不是只实例化一个）。
2. 在 `_bind_workers_method_to_parent`（L380-L411）里找到方法改名的那行（`method_name_with_prefix = key + '_' + method_name`），理解 `actor_rollout_init_model` 这种名字怎么来的。
3. 在 `init_workers`（L486-L514）里确认：因为三个角色都映射到 `global_pool`，循环里 `resource_pool_to_cls` 只有一个池子条目，因此只创建了**一个** `RayWorkerGroup`（一组物理 actor）。
4. 阅读注释（L487-L489）：如果你想给不同角色用不同的资源池（不同并行度），就**不要**用 `create_colocated_worker_cls`，而是给每个角色单独传一个池子。

**需要观察的现象**：

- 合体发生在 `WorkerDict.__init__` 这一层——它不是 Ray 层面的多个 actor 共享，而是**同一个 Python 进程内的多个普通对象**。Ray 只看到一个 actor。
- `DISABLE_WORKER_INIT=1`（L448）保证三个子对象里只有「最外层那次」会做 rank/master 协商，子对象跳过，避免冲突。

**预期结果**：你能解释「为什么 `init_workers` 里只 new 了一次 worker group，却能得到 `actor_rollout_wg`、`critic_wg`、`ref_policy_wg` 三个可用对象」——因为 `spawn` 用 `from_detached` 复用了同一批 actor 句柄，只是给每个视图重绑了方法名。

#### 4.4.5 小练习与答案

**练习 1**：`create_colocated_worker_cls` 为什么要 assert 所有角色的「worker class 基类相同」（L429-L433）？

**答案**：因为它要动态定义 `class WorkerDict(worker_cls)`，必须有一个共同的基类来作为 `WorkerDict` 的父类。如果不同角色的基类不同，就无法合成一个统一的 `WorkerDict` 类。在 TinyZero 里，`ActorRolloutRefWorker` 和 `CriticWorker` 都继承自同一个 `Worker` 基类，满足这个约束。

**练习 2**：如果让 Actor 用 8 卡、Critic 只用 4 卡，应该怎么改 `main_ppo.py` 的接线？

**答案**：不能再让两个角色共用 `global_pool`，而要声明两个池子（如 `actor_pool`=[8]、`critic_pool`=[4]），并按 `init_workers` L487-L489 的注释——**不使用** `create_colocated_worker_cls`，而是给每个角色单独建一个 `RayWorkerGroup`，各自绑定自己的资源池。这样它们不再 colocate，可以有不同的并行规模。

---

## 5. 综合实践

本实践把本讲四个模块串起来：画出 `main_ppo` 把三个 Role colocate 到同一组 GPU 的资源映射图，并解释 `max_colocate_count=1` 对 FSDP 后端的意义。

### 实践目标

- 把「三张表（role_worker_mapping / resource_pool_spec / mapping）→ ResourcePoolManager → create_colocated_worker_cls → spawn」这条链路用一张图表达出来。
- 说清 FSDP 为什么选 `max_colocate_count=1`，而不是像 Megatron 那样用 `>1`。

### 操作步骤

1. **读三张表**。打开 [verl/trainer/main_ppo.py:142-156](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L142-L156)，确认：
   - `resource_pool_spec = {'global_pool': [n_gpus_per_node] * nnodes}`（假设单机 4 卡：`{'global_pool': [4]}`）。
   - `mapping`：`ActorRollout`、`Critic`、`RefPolicy` 三个角色都 → `global_pool`。
2. **画资源映射图**。用纸笔或任意画图工具画出下面的结构（以单机 4 卡为例）：

```text
角色层:    Role.ActorRollout   Role.Critic   Role.RefPolicy    （三个逻辑角色）
               │                   │              │
               └───────── mapping 都指向 ──────────┘
                              global_pool
                              (process_on_nodes=[4])
                               │
          ResourcePoolManager.create_resource_pool(max_colocate_count=1)
                               │
                               ▼
         RayResourcePool → get_placement_groups → 1 个 PlacementGroup
                          含 4 个 bundle，每 bundle = {GPU:1, CPU:1}
                               │
          create_colocated_worker_cls({'actor_rollout','critic','ref'})
                               │
                               ▼  WorkerDict（每进程内住 3 个角色）
          RayWorkerGroup: 每个 bundle 钉 1 个合体 actor，共 4 个 actor
                               │
                 spawn → 3 个逻辑 group 视图（共用这 4 个 actor）
                               │
                               ▼
   GPU0: WorkerDict{actor_rollout, critic, ref}   num_gpus=1
   GPU1: WorkerDict{actor_rollout, critic, ref}   num_gpus=1
   GPU2: WorkerDict{actor_rollout, critic, ref}   num_gpus=1
   GPU3: WorkerDict{actor_rollout, critic, ref}   num_gpus=1
```

3. **验证 colocate 的「合体」**。打开 [verl/single_controller/ray/base.py:442-450](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L442-L450)，确认每个 `WorkerDict` 内部 `self.worker_dict` 同时持有三个子对象——这就是「同一进程住多角色」的落点。
4. **解释 `max_colocate_count=1` 对 FSDP 的意义**。结合 [ray_trainer.py:64-73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L64-L73) 的注释和 [base.py:224](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L224) 的 `num_gpus = 1 / max_collocate_count`，写下你的解释（见下方参考答案）。

### 需要观察的现象

- `resource_pool_to_cls` 在 `init_workers` 里最终只有一个池子条目（三个角色合并到同一个池），所以只创建一组物理 actor。
- `num_gpus=1`：每个合体 actor 占满一整张 GPU（不是 1/3 张）。

### 预期结果 / 参考答案

**`max_colocate_count=1` 对 FSDP 后端的意义**：

`max_colocate_count` 控制的是「同一份资源（一张 GPU）上允许挤几个 WorkerGroup/角色」，体现为每个 actor 向 Ray 声明的 `num_gpus = 1/max_colocate_count`。

- 若 `max_colocate_count=3`，每个 actor 只要 1/3 张 GPU，于是 Actor、Critic、Ref 可以作为**三个独立进程**分时共享同一张卡。这对 Megatron 后端合适——Megatron 有自己精细的显存管理与张量/流水并行，能容忍多个进程共卡。
- 但 **FSDP 不适合这种「多进程分时共卡」**：FSDP 把参数分片到各 data-parallel rank，期望每个进程**独占**它那张卡的显存预算来放自己的参数分片、梯度和优化器状态；多个 FSDP 进程挤在一张卡上会各自按「整卡」做显存规划，极易 OOM，也难以协调。

所以 FSDP 后端选 `max_colocate_count=1`（每个进程独占一整张 GPU、`num_gpus=1`），而**用 `create_colocated_worker_cls` 把 Actor/Critic/Ref 三个角色合并进同一个进程**来达到「共用 GPU」的目的——这是「进程级合并」，而非「GPU 级分时」。换句话说：**FSDP 靠 colocate 合体来共享显存，而非靠 `max_colocate_count>1` 来分时**。这也是 `ResourcePoolManager.create_resource_pool` 里 L66-L68 注释所建议的策略。

### 待本地验证

如果本地有 Ray + 多卡环境，可在 `init_workers` 之后打印 `len(self.actor_rollout_wg._workers)`、`len(self.critic_wg._workers)`，验证三个逻辑 group 的 worker 数相同且指向同一批 actor（可用 `w._actor_id` 比对）。

## 6. 本讲小结

- **单控制器** = 一个 driver 进程编排、一组 Ray worker actor 计算；driver 通过 `WorkerGroup` 调方法，group 内部自动完成 dispatch → execute → collect 三步，骨架见 `func_generator`。
- **`RayResourcePool`** 把「每节点进程数」（`process_on_nodes`）翻译成 Ray `PlacementGroup`，每个进程一个 bundle（1 GPU + 若干 CPU），用 `STRICT_PACK` 打包到节点上。
- **`num_gpus = 1 / max_colocate_count`**：`max_colocate_count` 决定一张 GPU 上能挤几个角色进程，是「分时共享」的开关。
- **`RayClassWithInitArgs` + `RayWorkerGroup`** 负责把一个类按资源池逐槽位远程化成 actor，并注入 `RANK`/`WORLD_SIZE`/`MASTER_ADDR` 等环境变量；master 地址端口由 rank 0 选定并经「注册中心」广播。
- **`create_colocated_worker_cls`** 把多个角色合体进同一个 `WorkerDict` 进程，再用 `spawn` 拆成多个逻辑 group 视图——物理一组 actor、逻辑多个 worker group。
- **`main_ppo` 的三张表**（`role_worker_mapping`/`resource_pool_spec`/`mapping`）是 colocate 的接线图：三个 Role 都指向唯一的 `global_pool`，于是一组 GPU 上每个进程同时住着 Actor/Critic/Ref；FSDP 选 `max_colocate_count=1` 是为了「进程级合体共享显存」而非「GPU 级分时」。

## 7. 下一步学习建议

本讲解完了「worker 是怎么被创建、放置、合体的」，但还故意留了一个黑盒：driver 调 `wg.update_actor(data)` 时，**`data` 具体是怎么被切成各 rank 的份、结果又怎么拼回来**——也就是 `dispatch_fn` / `collect_fn` 的内部机制。这正是下一讲 [u3-l3 Dispatch 机制：注册与数据分发](u3-l3-dispatch-decorator.md) 的主题，它会拆开 `@register` 装饰器与 `ONE_TO_ALL` / `DP_COMPUTE_PROTO` / `ALL_TO_ALL` 等 Dispatch 模式。

之后在 [u4-l2 RayPPOTrainer 初始化与 Worker 编排](u4-l2-ray-trainer-init.md) 里，你会看到 `init_workers` 的完整顺序与「为什么 rollout 要最后创建」；在 [u6-l1 ActorRolloutRefWorker 混合引擎](u6-l1-hybrid-actor-rollout-ref-worker.md) 里，你会看到被 colocate 进同一进程的 Actor/Rollout/Ref 三个子对象各自如何被初始化。建议带着本讲的「合体 actor」心智模型去读那两讲，会顺畅很多。
