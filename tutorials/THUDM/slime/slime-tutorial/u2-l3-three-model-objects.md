# 三大对象：actor / critic / rollout_manager

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `train.py` 里 `actor_model`、`critic_model`、`rollout_manager` 这三个变量的「真身」分别是什么类型。
- 区分 **本地门面对象（`RayTrainGroup`）** 与 **Ray 远程演员（`RolloutManager`）**，并理解为什么前者是普通 Python 对象、后者是远程句柄。
- 掌握 `RayTrainGroup` 的命名约定：以 `async_` 开头的方法返回 Ray `ObjectRef` 列表，其余方法内部 `ray.get` 阻塞、返回同步结果。
- 认识 `TrainRayActor` 抽象基类如何定义训练工人的统一契约，以及 `MegatronTrainRayActor` 如何作为唯一具体实现被注入。
- 理解三个对象之间两次「握手」（`train_parallel_config` 下发、`start_rollout_id` 对齐）如何把它们串成一个整体。

## 2. 前置知识

本讲建立在 **u2-l1（三模块架构总览）** 与 **u2-l2（Ray 编排与 placement group）** 之上。你需要先记住两点：

1. slime 把 RL 训练切成 **rollout（采样）→ data buffer（桥梁）→ training（训练）→ 权重同步回 rollout** 的闭环；权重同步是 **training→rollout 单向**。
2. slime 用 **一个 placement group（放置组）** 装下所有 GPU，靠 `rollout_offset` 把 bundle 切成 actor 段与 rollout 段；colocate 模式下二者指向同一批物理 GPU。

本讲要回答的核心问题是：**上一讲把卡分好了，那么 `train.py` 到底在操控哪几个对象、它们各自能调哪些方法、调用后是立刻拿到结果还是只拿到一个「未来值」？** 这是从「资源编排」迈向「调用接口」的关键一步。

几个前置术语：

- **Ray 远程演员（Ray remote actor）**：用 `@ray.remote` 装饰的类，其实例跑在独立进程里。你在主进程拿到的是一个**句柄（handle）**，对它调用 `.method.remote(...)` 会立即返回一个 **`ObjectRef`（对象引用，俗称 future）**，需要用 `ray.get(ref)` 才能阻塞取到真实返回值。
- **门面（facade）**：一个普通本地对象，内部不干实事，而是把调用 fan-out（扇出）给一组真正的远程演员，再把结果聚合回来。
- **fan-out**：一次调用被分发到多个并行执行单元上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) | 训练主循环入口，创建并驱动三大对象的「最薄一层」。 |
| [slime/ray/placement_group.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py) | 工厂函数 `create_training_models` / `create_rollout_manager`，把 placement group 装配成三大对象。 |
| [slime/ray/actor_group.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py) | `RayTrainGroup` 门面类，封装一组训练工人。 |
| [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | `RolloutManager` Ray 远程演员，封装 rollout 全流程。 |
| [slime/ray/train_actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py) | `TrainRayActor` 抽象基类，定义训练工人的统一契约。 |
| [slime/backends/megatron_utils/actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py) | `MegatronTrainRayActor`，`TrainRayActor` 的唯一具体实现。 |

---

## 4. 核心概念与源码讲解

先建立一个最重要的全局认知。`train.py` 的开头是这样创建三大对象的：

```python
# train.py:14-21（节选）
pgs = create_placement_groups(args)                                   # 上一讲：分卡
rollout_manager, num_rollout_per_epoch = create_rollout_manager(args, pgs["rollout"])
actor_model, critic_model = create_training_models(args, pgs, rollout_manager)
```

注意：这三个变量的**类型并不相同**。这是初学者最容易踩的坑——它们看起来都像「一个模型对象」，但底层机制完全两样：

| `train.py` 变量 | Python 类型 | 本地对象 / Ray 远程句柄 | 内部真正持有 |
| --- | --- | --- | --- |
| `actor_model` | `RayTrainGroup` | **本地对象**（普通类实例） | 一组 `MegatronTrainRayActor` 远程句柄 |
| `critic_model` | `RayTrainGroup`（或 `None`） | **本地对象**（普通类实例） | 一组 `MegatronTrainRayActor` 远程句柄（`role="critic"`） |
| `rollout_manager` | `RolloutManager` 句柄 | **Ray 远程句柄**（`@ray.remote` 类） | 若干 `SGLangEngine` + `DataSource` |

一句话记忆：**两个 train 对象是「本地门面」，rollout 对象是「远程演员」**。这种差异直接决定了它们各自的方法返回的是「同步结果」还是「Ray ObjectRef」。下面三个小节分别拆解。

### 4.1 TrainRayActor 抽象：训练工人的统一契约

#### 4.1.1 概念说明

`RayTrainGroup` 内部那一组工人（worker），并不是随便什么类，而是遵循一个统一契约的对象——这个契约就是抽象基类 **`TrainRayActor`**。它定义了「一个能被 slime 驱动的训练工人」必须具备哪些行为：能初始化、能训练一步、能存档、能把权重同步出去、能在显存紧张时「睡觉/醒来」。

之所以要做这层抽象，是为了把 **「编排层（slime/ray/）」与「具体训练后端（slime/backends/megatron_utils/）」解耦**。编排层只面向 `TrainRayActor` 这个抽象接口编程；将来如果接入新的训练后端，只要再写一个子类即可，编排层代码不需要改动。目前 slime 只有一个具体实现 `MegatronTrainRayActor`。

继承关系是这样的：

```
RayActor                 # 极简基类，只提供 get_master_addr_and_port 等工具方法
  └── TrainRayActor      # 抽象契约：定义 init/train/save_model/update_weights/sleep/wake_up
        └── MegatronTrainRayActor   # 唯一具体实现：真正跑 Megatron 训练
```

#### 4.1.2 核心流程

一个 `TrainRayActor` 子类实例的生命周期由 `RayTrainGroup` 驱动，顺序大致是：

1. **构造**：`RayTrainGroup` 用 `ray.remote(实现类)(world_size, rank, ...)` 远程创建实例，设置 `MASTER_ADDR/PORT/WORLD_SIZE/RANK/LOCAL_RANK` 等环境变量。
2. **`init(args, role, ...)`**：初始化分布式进程组（`dist.init_process_group`）、设置 NUMA 亲和性、加载模型与检查点，返回一个 `start_rollout_id`（表示应从第几个 rollout 续训）。
3. **`train(rollout_id, rollout_data_ref, external_data)`**：执行一步训练（或仅前向算 value/logprob）。返回 `None`（actor）或 `{"values": [...]}`（critic）。
4. **`save_model(rollout_id)`**：把当前权重存盘。
5. **`update_weights()`**：把训练后的权重同步给 rollout 引擎（这是 training→rollout 单向同步的入口）。
6. **`sleep()` / `wake_up(tags)`**：colocate/offload 模式下释放显存、再恢复显存。

#### 4.1.3 源码精读

抽象基类 `TrainRayActor` 继承自 `RayActor`，构造函数负责设置分布式环境变量，`init` 负责初始化进程组：

[slime/ray/train_actor.py:28-48](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L28-L48) —— `TrainRayActor` 构造函数：写入 `MASTER_ADDR/PORT/WORLD_SIZE/RANK/LOCAL_RANK`，为后续 `dist.init_process_group` 做准备。

[slime/ray/train_actor.py:50-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L50-L66) —— `init` 方法：设置 CUDA 设备、调用 `dist.init_process_group` 建立通信组、`init_gloo_group()` 建立 CPU 侧 gloo 组（用于主机间元数据同步）。

抽象契约本身——注意这些方法都带 `@abc.abstractmethod`，只声明、不实现：

[slime/ray/train_actor.py:101-123](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L101-L123) —— 六个抽象方法：`sleep` / `wake_up` / `train` / `save_model` / `update_weights` / `_get_parallel_config`。这就是「训练工人」必须实现的六件事。

唯一具体实现 `MegatronTrainRayActor` 把这些方法落到 Megatron 上：

[slime/backends/megatron_utils/actor.py:51](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L51) —— 类声明，明确写出继承自 `TrainRayActor`。

[slime/backends/megatron_utils/actor.py:100-105](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L100-L105) —— 在 `init` 里直接构建 `self.train_parallel_config` 字典（`dp_size`/`cp_size`/`vpp_size` 等）。注意：抽象里声明的 `_get_parallel_config` 在实践中并未被单独重写，并行配置是作为属性直接填充的——这是阅读时容易困惑的小细节。

[slime/backends/megatron_utils/actor.py:107](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L107) —— `init` 返回 `start_rollout_id = loaded_rollout_id + 1`，告诉编排层「该从第几个 rollout 续训」。这个返回值会在 4.2 节看到如何被收集与对齐。

#### 4.1.4 代码实践

**实践目标**：验证抽象基类的契约真的被具体实现覆盖。

**操作步骤**：

1. 打开 [slime/ray/train_actor.py:101-123](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L101-L123)，记下六个抽象方法名。
2. 打开 [slime/backends/megatron_utils/actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py)，用编辑器搜索 `def sleep`、`def wake_up`、`def train`、`def save_model`、`def update_weights`，确认每个抽象方法都有对应的具体实现。
3. 单独观察 `_get_parallel_config`：搜索整个仓库会发现它只在 `train_actor.py:122` 出现一次，没有被任何子类重写，也没有被任何地方调用——它是一份「待实现」的契约占位。

**需要观察的现象**：五个生命周期方法（sleep/wake_up/train/save_model/update_weights）在 Megatron 子类里都能找到对应 `def`；`_get_parallel_config` 找不到重写。

**预期结果**：你会确认「抽象契约的五个核心方法都被 Megatron 实现，第六个 `_get_parallel_config` 目前是占位」。这说明抽象的主要价值是**文档化训练工人的接口形状**，而非严格的运行期强制（`TrainRayActor` 未设置 `ABCMeta`，`@abc.abstractmethod` 在此更多是表达意图）。

> 说明：本实践为「源码阅读型实践」，无需运行；如果你对「`@abc.abstractmethod` 为何没强制」存疑，待本地用 `print(type(MegatronTrainRayActor).__mro__)` 之类验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么编排层（`slime/ray/`）要面向 `TrainRayActor` 抽象，而不是直接 `import MegatronTrainRayActor`？

**参考答案**：为了把「编排逻辑」与「具体后端」解耦。编排层只依赖契约（init/train/save_model/update_weights/sleep/wake_up），将来接入新后端时只需新增一个子类、在工厂里切换 `actor_cls`，编排层一行不用改。

**练习 2**：`init` 方法返回的 `start_rollout_id` 有什么用？为什么不能由编排层自己算？

**参考答案**：`start_rollout_id` 表示「从检查点恢复后，应从第几个 rollout 续训」。它必须由真正加载了检查点的工人返回（`loaded_rollout_id + 1`），因为只有工人知道磁盘上的检查点对应第几轮；编排层本身不读检查点，无法自己算。

---

### 4.2 RayTrainGroup：训练工人的门面

#### 4.2.1 概念说明

**`RayTrainGroup` 是一个普通的本地 Python 类**（不是 `@ray.remote`），它扮演「门面」角色：内部持有一组训练工人的远程句柄 `self._actor_handlers`，对外暴露几个简洁方法，把一次调用 fan-out 到所有工人、再把结果聚合回来。

`actor_model` 和 `critic_model` 都是 `RayTrainGroup` 实例，区别仅在 `role`（`"actor"` 或 `"critic"`）和占用哪段 placement group。critic 默认复用 actor 的同一批物理 GPU（见上一讲），靠 `offload_train` 时分复用。

`RayTrainGroup` 的方法命名遵循一条**铁律**（写在其类文档字符串里）：

> Functions start with `async` should return list of object refs.

也就是说：

- 方法名以 `async_` 开头 → **不阻塞**，返回 `ObjectRef` 列表（每个工人一个 ref）。
- 方法名不以 `async_` 开头 → **内部已 `ray.get`**，返回同步结果。

这条约定是本讲最重要的实操记忆点。

#### 4.2.2 核心流程

`RayTrainGroup` 的方法可以分成三类：

1. **异步类（返回 ref 列表）**：只有 `async_train`。它对每个工人调 `actor.train.remote(...)`，立即返回一组 ref。这样多个工人可以并行训练，主循环拿到 ref 后可以继续做别的事（例如让 critic 先训、actor 再消费 critic 的 value）。
2. **同步类（内部 ray.get）**：`save_model`、`update_weights`、`create`、`onload`、`offload`、`release`、`clear_memory`、`set_rollout_manager`。它们都先 fan-out 再 `ray.get` 等待全部完成。
3. **创建类**：`create(rollout_manager)` —— 物理创建工人远程演员、调 `init`、并把 `rollout_manager` 注入每个工人。

#### 4.2.3 源码精读

先看类文档字符串里的命名约定：

[slime/ray/actor_group.py:13-16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L13-L16) —— 类声明与「`async_` 开头方法返回 ref 列表」的约定，这是阅读本类所有方法时的判别准则。

`async_train` 是唯一不阻塞的方法，注意它没有 `ray.get`：

[slime/ray/actor_group.py:130-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L130-L148) —— `async_train`：返回的是 `actor.train.remote(...)` 的列表（`ObjectRef` 列表）；当 `external_data` 是 list 时按工人逐个下发（用于把 critic 的 value refs 传给 actor）。这正是 `train.py` 里 `value_refs = critic_model.async_train(...)` 之后 `actor_model.async_train(..., external_data=value_refs)` 能衔接的原因。

与之对照，`save_model` 是同步的——内部 `ray.get`：

[slime/ray/actor_group.py:150-159](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L150-L159) —— `save_model`：`ray.get([actor.save_model.remote(...) ...])` 后才 `return ret`，调用方拿到的是同步结果。`_release_train_enabled()` 分支还会把 `args.save` 写回 `args.load`，支撑 release-train 模式逐轮重建。

`update_weights` 同样同步，它是 **training→rollout 权重同步** 的编排入口：

[slime/ray/actor_group.py:161-172](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L161-L172) —— `update_weights`：默认路径 `ray.get([actor.update_weights.remote() ...])` 阻塞等待所有工人把权重推给 rollout 引擎；当启用「full + disk」模式时，走 `_reload_rollout_weights_from_disk` 从磁盘把权重灌进引擎。无论哪条路径，都对调用方表现为同步。

`create` 负责真正拉起工人并完成两次握手之一：

[slime/ray/actor_group.py:187-207](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L187-L207) —— `create`：调 `_allocate_gpus_for_actor` 物理创建工人，再 `ray.get([actor.init.remote(...) ...])` 收集每个工人返回的 `start_rollout_ids`，最后调 `set_rollout_manager` 把 rollout 句柄注入每个工人。

工人类如何被选定？这就是抽象的「注入点」：

[slime/ray/actor_group.py:99-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L99-L104) —— `_allocate_gpus_for_actor` 里，若 `actor_cls is None` 则默认用 `MegatronTrainRayActor`，否则用传入的 `actor_cls`。这正是 4.1 节抽象落地的位置。

那么 `RayTrainGroup` 是怎么被造出来的？看工厂函数：

[slime/ray/placement_group.py:163-183](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L163-L183) —— `create_actor_model`：根据 `actor_num_nodes` / `actor_num_gpus_per_node` 和 placement group 造一个 `RayTrainGroup`（通过 `allocate_train_group`），并立即 `actor_model.create(rollout_manager=...)` 把工人拉起。

[slime/ray/placement_group.py:186-224](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L186-L224) —— `create_training_models`：先造 actor，若 `--use-critic` 再造一个 `role="critic"` 的 `RayTrainGroup`；critic 复用的 placement group 见下一行。

[slime/ray/placement_group.py:135](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L135) —— `result["critic"] = result["actor"]`：critic 直接复用 actor 的同一段 placement group（同一批物理 GPU），这就是「PPO 会强制开启 offload_train 时分复用」在数据结构上的根因（承接 u2-l2）。

两次「握手」中的第二次——`start_rollout_id` 对齐——也发生在工厂里：

[slime/ray/placement_group.py:211-219](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L211-L219) —— 收集所有工人返回的 `start_rollout_ids`，`assert len(set(start_rollout_ids)) == 1` 要求所有工人对「从第几轮续训」达成一致，再写入 `args.start_rollout_id`。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`async_` 前缀 = 返回 ref、其余 = 同步」这条命名约定。

**操作步骤**：

1. 打开 [slime/ray/actor_group.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py)。
2. 对 `RayTrainGroup` 的每个 `def` 方法，判断它的 `return` 语句里是否出现 `ray.get(...)`：
   - `async_train`（L130）：返回值是列表推导，元素是 `actor.train.remote(...)`——**无 `ray.get`**。
   - `save_model`（L150）：`ret = ray.get([...])` 后 `return ret`——**有 `ray.get`**。
   - `update_weights`（L161）：`return ray.get([...])`——**有 `ray.get`**。
   - `onload`（L174）/`offload`（L177）/`clear_memory`（L209）/`set_rollout_manager`（L212）：都是 `return ray.get([...])`——**有 `ray.get`**。
3. 把结果填成一张三列表：方法名 / 是否以 `async_` 开头 / 返回 ref 还是同步结果。

**需要观察的现象**：只有 `async_train` 不以同步方式返回；其余方法统统 `ray.get` 阻塞。

**预期结果**：你会发现「方法名是否带 `async_` 前缀」与「是否在内部 `ray.get`」完全一一对应，没有任何例外。这条约定之所以重要，是因为 `train.py` 里 `value_refs = critic_model.async_train(...)` 故意不阻塞，好让 critic 与 actor 的训练重叠。

> 说明：本实践为源码阅读型；如需运行验证，可在本地给 `async_train` 加一行 `print(type(result[0]))`，预期打印 Ray 的 `ObjectRef` 类型（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`train.py` 里为什么对 critic 用 `async_train`、紧接着又对 actor 用 `async_train(external_data=value_refs)`，而不是各自 `save_model` 那样直接同步？

**参考答案**：critic 的训练产出 value，actor 的训练需要消费这些 value（算 advantage）。用 `async_train` 让 critic 先返回 ref（不阻塞），再把 ref 作为 `external_data` 传给 actor，actor 在自己进程内 `ray.get(value_refs)` 取到 value——这样 critic 与 actor 的计算可以重叠，而不必等 critic 全部算完才开始 actor。

**练习 2**：`actor_model` 和 `critic_model` 都是 `RayTrainGroup`，它们占用的物理 GPU 一样吗？

**参考答案**：默认一样。`placement_group.py:135` 让 `result["critic"] = result["actor"]`，critic 复用 actor 的同一段 placement group（同一批物理 GPU），靠 `offload_train` 轮流让出显存（承接 u2-l2）。因此 PPO（需要 critic）会强制开启 offload。

---

### 4.3 RolloutManager：rollout 的远程演员

#### 4.3.1 概念说明

与 `RayTrainGroup` 不同，**`RolloutManager` 是一个 `@ray.remote` 类**，所以 `train.py` 里的 `rollout_manager` 是一个**远程句柄**，不是本地对象。它跑在独立进程里（`num_cpus=1, num_gpus=0`），内部持有：

- `self.servers`：一个或多个 `RolloutServer`（每个背后是一个 router + 若干 `SGLangEngine` 推理引擎）。
- `self.data_source`：数据源（由 `--data-source-path` 指定的类实例化）。
- `self.generate_rollout` / `self.eval_generate_rollout`：由 `--rollout-function-path` / `--eval-function-path` 加载的可调用对象。

`RolloutManager` 对外暴露 `generate` / `eval` / `save` / `offload` / `onload` 等方法。**因为它本身是远程演员，对它的任何方法都必须用 `.remote(...)` 调用，返回的是单个 `ObjectRef`**；要拿真实结果，调用方需自行 `ray.get(...)`。这与 `RayTrainGroup`「方法自己决定同步与否」截然不同——这里是「一律返回 ref，由调用方决定何时阻塞」。

> 一个易混点：`RolloutManager` 内部也会对它持有的引擎调 `.remote()`，但那些是它自己的事；对 `train.py` 而言，`rollout_manager.generate` 永远要先 `.remote()` 再 `ray.get()`。

#### 4.3.2 核心流程

`RolloutManager` 在闭环中承担「采样 + 数据转换」一职，核心方法 `generate` 的内部流程：

1. 记录 `rollout_id`，恢复健康监控。
2. `_get_rollout_data`：调用 `self.generate_rollout(...)`（默认是 sglang rollout），拿到 `list[list[Sample]]` 并展平。
3. `_save_debug_rollout_data`：可选地落盘调试数据。
4. `_convert_samples_to_train_data`：把 `Sample` 列表转成训练用的张量字典（tokens / loss_masks / rewards / rollout_log_probs 等），并做组归一等奖励后处理。
5. `_split_train_data_by_dp`：按 DP 维度切成每个 rank 一份，包成 Ray `Box` 引用返回。

`eval` 流程类似但不转换为训练数据；`save` 委托给 `data_source.save`。

#### 4.3.3 源码精读

先确认它确实是 `@ray.remote`：

[slime/ray/rollout.py:427-429](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L427-L429) —— `@ray.remote class RolloutManager:` 声明它为远程演员，文档字符串点明职责：「run rollout and convert rollout data to training data」。

`__init__` 里通过 `load_function` 把字符串路径解析成可调用对象，并实例化数据源：

[slime/ray/rollout.py:444-457](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L444-L457) —— `load_function(args.data_source_path)` 造数据源、`load_function(args.rollout_function_path)` 取 rollout 函数、`load_function(args.eval_function_path)` 取评估函数。这就是 slime「用 import path 字符串注入自定义逻辑」的统一机制（详见 u6-l1）。

`generate` 是闭环采样的总入口：

[slime/ray/rollout.py:553-567](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L553-L567) —— `generate`：取 rollout 数据 → 落盘调试 → `_convert_samples_to_train_data` → `_split_train_data_by_dp`，最终返回「按 DP 切好的 rollout_data 引用列表」。注意 `train.py` 里写的是 `ray.get(rollout_manager.generate.remote(rollout_id))`——`.remote()` 返回 ref、`ray.get` 才取到这个列表。

`eval` 与 `save`：

[slime/ray/rollout.py:569-582](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L569-L582) —— `eval` 调评估 rollout 函数并记录指标（不转训练数据）；`save` 直接委托 `self.data_source.save(rollout_id)`。

两次「握手」中的第一次——`train_parallel_config` 下发——发生在 `RolloutManager` 这边：

[slime/ray/rollout.py:828-829](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L828-L829) —— `set_train_parallel_config`：接收 actor 传来的并行配置（dp_size 等）并存为 `self.train_parallel_config`。这个配置在 `_split_train_data_by_dp`（L843）里被用来决定把数据切成几份。

下发方是 `TrainRayActor.set_rollout_manager`：

[slime/ray/train_actor.py:125-128](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L125-L128) —— `set_rollout_manager`：存下 rollout 句柄，并在 rank 0 上调 `rollout_manager.set_train_parallel_config.remote(self.train_parallel_config)`。这就是 actor 把自己的 DP/CP 配置告诉 rollout manager 的握手——只有知道了 dp_size，rollout 才知道把数据切几份。

`RolloutManager` 又是怎么被造出来的？看另一个工厂函数：

[slime/ray/placement_group.py:227-253](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L227-L253) —— `create_rollout_manager`：用 `RolloutManager.options(num_cpus=1, num_gpus=0).remote(args, pg)` 创建远程演员（注意它**不占 GPU**，GPU 都给了内部引擎）；若 `args.num_rollout is None`，则通过 `get_num_rollout_per_epoch` 反推总 rollout 数。

#### 4.3.4 代码实践

**实践目标**：在 `train.py` 里找出所有对 `rollout_manager` 的调用，把它们归入闭环五阶段。

**操作步骤**：

1. 打开 [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py)。
2. 搜索所有 `rollout_manager.` 出现的位置，记录方法名与所在阶段。你会找到（参考行号）：
   - `rollout_manager.onload_weights.remote()` / `onload_kv.remote()`（L24/L33，初始化阶段）
   - `rollout_manager.check_weights.remote(action="compare")`（L30，校验）
   - `rollout_manager.eval.remote(...)`（L37/L51/L91，**评估**阶段）
   - `rollout_manager.generate.remote(rollout_id)`（L53，**采样**阶段）
   - `rollout_manager.offload.remote()`（L56，让出显存）
   - `rollout_manager.save.remote(rollout_id)`（L80，存数据源）
   - `rollout_manager.dispose.remote()`（L93，收尾）
3. 注意每一处都是 `.remote()` 后被 `ray.get(...)` 包裹（除了少数刻意不阻塞的）。

**需要观察的现象**：`rollout_manager` 的方法**全部**通过 `.remote()` 调用，与 `actor_model.async_train`（返回 ref 列表）不同，这里每个 `.remote()` 只返回**单个** `ObjectRef`。

**预期结果**：你得到一张「方法 → 闭环阶段」的映射表，并确认 rollout_manager 是「一律 `.remote()` + 由调用方 `ray.get`」的远程演员风格。

> 说明：本实践为源码阅读型；运行验证需要完整集群，标注「待本地验证」即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RolloutManager` 自己 `num_gpus=0`，但它却能跑推理？

**参考答案**：`RolloutManager` 只是一个协调者（coordinator），它**不直接持有 GPU**，而是内部创建若干 `SGLangEngine` 远程演员（那些演员才占 GPU）。`RolloutManager` 负责调度这些引擎、转换数据，本身只跑 CPU 逻辑，所以 `num_gpus=0`。

**练习 2**：`train_parallel_config` 握手的方向是什么？为什么需要它？

**参考答案**：方向是 **actor → rollout_manager**（`TrainRayActor.set_rollout_manager` 在 rank 0 调 `set_train_parallel_config`）。rollout 产出的训练数据需要按训练侧的 `dp_size` 切成每 rank 一份（`_split_train_data_by_dp`），所以 rollout 必须先知道训练侧的并行配置。

---

## 5. 综合实践

把本讲三个对象串起来，完成下面这张「方法清单 + 返回类型」总表。这是本讲的核心交付物，也是后续读 `train.py` 循环时的速查表。

**任务**：阅读源码后，填写并核对下表（已在讲义中给出答案，请逐行到源码验证）。

### actor_model / critic_model（`RayTrainGroup`，本地门面）

| 方法 | 所在文件:行 | 是否 `async_` 前缀 | 返回类型 | 调用方需 `ray.get` 吗 |
| --- | --- | --- | --- | --- |
| `async_train` | actor_group.py:130 | 是 | `list[ObjectRef]`（每工人一个） | 需要时由调用方 `ray.get` |
| `save_model` | actor_group.py:150 | 否 | 同步结果（list） | 否（内部已 get） |
| `update_weights` | actor_group.py:161 | 否 | 同步结果 | 否（内部已 get） |
| `create` | actor_group.py:187 | 否 | `start_rollout_ids`（同步） | 否 |
| `onload` | actor_group.py:174 | 否 | 同步结果 | 否 |
| `offload` | actor_group.py:177 | 否 | 同步结果 | 否 |
| `release` | actor_group.py:180 | 否 | `None` | 否 |
| `clear_memory` | actor_group.py:209 | 否 | 同步结果 | 否 |
| `set_rollout_manager` | actor_group.py:212 | 否 | 同步结果 | 否 |

> 关键结论：`RayTrainGroup` 里**只有 `async_train` 返回 ref**，其余方法都已在内部 `ray.get`。命名前缀就是判别准则。

### rollout_manager（`RolloutManager`，Ray 远程句柄）

| 方法 | 所在文件:行 | 返回类型（经 `.remote()`） | 调用方需 `ray.get` 吗 |
| --- | --- | --- | --- |
| `generate` | rollout.py:553 | `ObjectRef` → 解析为按 DP 切好的数据引用列表 | 是 |
| `eval` | rollout.py:569 | `ObjectRef` | 是 |
| `save` | rollout.py:581 | `ObjectRef` | 是 |
| `offload` / `onload` / `onload_weights` / `onload_kv` | rollout.py:587-602 | `ObjectRef` | 是 |
| `get_num_rollout_per_epoch` | rollout.py:549 | `ObjectRef` → int | 是 |
| `set_train_parallel_config` | rollout.py:828 | `ObjectRef` | 是（被 actor 内部调用） |
| `check_weights` | rollout.py:631 | `ObjectRef` | 是 |
| `dispose` | rollout.py:506 | `ObjectRef` | 是 |

> 关键结论：`RolloutManager` 是远程演员，**所有方法都返回单个 `ObjectRef`**，是否阻塞完全由调用方（`train.py`）决定。

**进阶追问（建议写到笔记里）**：

1. 在 `train.py` 的循环里，哪一处刻意**没有**立刻 `ray.get`？为什么？
   （提示：`value_refs = critic_model.async_train(...)` 不阻塞，目的是让 critic 与 actor 训练重叠。）
2. 三个对象之间共有两次「握手」，分别是哪两次、方向各是什么？
   （答案：① `train_parallel_config`：actor → rollout_manager；② `start_rollout_id`：工人 → 编排层，再对齐写入 `args.start_rollout_id`。）

## 6. 本讲小结

- `train.py` 操控的三个对象类型并不相同：`actor_model` / `critic_model` 是本地门面 `RayTrainGroup`，`rollout_manager` 是 Ray 远程演员 `RolloutManager` 的句柄。
- `RayTrainGroup` 的命名铁律：以 `async_` 开头的方法返回 `list[ObjectRef]`（目前只有 `async_train`），其余方法内部已 `ray.get`、返回同步结果。
- `RolloutManager` 因为是远程演员，所有方法都经 `.remote()` 返回单个 `ObjectRef`，是否阻塞由调用方决定。
- `TrainRayActor` 是训练工人的抽象契约（init/train/save_model/update_weights/sleep/wake_up），`MegatronTrainRayActor` 是唯一具体实现，通过 `actor_cls` 注入到 `RayTrainGroup`。
- critic 默认复用 actor 的同一批物理 GPU（`result["critic"] = result["actor"]`），靠 offload_train 时分复用。
- 三个对象通过两次握手协作：actor 把 `train_parallel_config` 下发给 rollout_manager；所有工人把 `start_rollout_id` 上报并对齐，决定从第几轮续训。

## 7. 下一步学习建议

本讲只看清了「三大对象长什么样、能调什么」，但每个方法**内部**做了什么尚未展开。建议按以下顺序深入：

- **u3-l1（Sample 数据结构）**：`rollout_manager.generate` 返回的训练数据，其基本元素是 `Sample`，先读懂它才能理解 `_convert_samples_to_train_data`。
- **u3-l2（默认 rollout 函数 generate_rollout）**：进入 `RolloutManager` 内部，看 `generate_rollout` 如何驱动 SGLang 引擎、算奖励、产出 `Sample`。
- **u4-l1（MegatronTrainRayActor 训练工人生命周期）**：进入 `RayTrainGroup` 内部，精读 `MegatronTrainRayActor.train` / `train_actor` 如何算 advantage、走训练步。
- **u5-l1（权重同步全景）**：本讲只点到 `update_weights` 是「training→rollout 单向同步」的入口，u5 会讲透 full/delta × nccl/disk 四种组合的实现。
