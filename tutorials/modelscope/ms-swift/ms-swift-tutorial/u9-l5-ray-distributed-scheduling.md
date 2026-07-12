# Ray 分布式调度

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 ms-swift 里「Ray」到底指什么，以及它和 torchrun/DeepSpeed/FSDP 的关系与分工。
- 读懂 `RayHelper` 的两个装饰器 `@RayHelper.worker` 与 `@RayHelper.function`，能解释「同一段代码，单机直通、多机变 Ray actor」的双模透明机制。
- 掌握 `RayArguments` 三个字段如何把 YAML 里的 `use_ray` / `device_groups` 注入到运行时，并触发 `RayHelper.initialize`。
- 理解 megatron-ray 这一套独立栈（`MegatronRayPipeline` / `WorkerGroup` / `ResourcePool`）如何在 GRPO/GKD 里把「训练 worker」与「rollout 推理副本」调度到不同 GPU，并区分 colocate（共享）与 separate（独占）两种放置模式。
- 动手用 `try_init_ray` 启动一次 Ray 会话，并读懂一份多机 SFT 或 Megatron GRPO 的 YAML 配置。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（对应前置讲义）：

- **分布式训练基础（u9-l1）**：torchrun 如何用 `NPROC_PER_NODE`/`NNODES` 拉起多进程，DeepSpeed ZeRO/FSDP 切的是「参数/优化器状态/梯度」，DDP 切的是 batch。
- **SFT 主流程（u5-l4）**：`SwiftPipeline` 的模板方法骨架，以及 `SwiftSft` 里 `_prepare_model_tokenizer` / `_prepare_template` / `_prepare_dataset` / `run` 这条准备链。
- **参数体系（u2-l1）**：`BaseArguments` 用多继承把多个 mixin 拼成统一参数对象。

本讲会用到一个关键直觉：**Ray 解决的不是「怎么切模型」，而是「怎么把多个独立的进程/角色调度到集群的一堆 GPU 上，并让它们彼此通信」。** 它和 DeepSpeed/FSDP 是正交关系——Ray 负责「编排（谁在哪张卡、谁先谁后）」，DeepSpeed/FSDP/Megatron 负责「并行算法（怎么切、怎么算）」。你可以理解为：

- torchrun：一种「笨但简单」的编排器——所有进程跑同一份脚本，靠环境变量对齐 rank，必须手动在每台机器上启动、手动配 `MASTER_ADDR/PORT`。
- Ray：一种「聪明的编排器」——你只在一个 YAML 里声明每个角色要几张卡，Ray 自动创建进程、分配 GPU、跨机调度、维护通信拓扑。

理解了这一点，本讲的所有设计都顺理成章。

## 3. 本讲源码地图

本讲涉及的关键源码文件如下：

| 文件 | 作用 |
|------|------|
| [swift/ray_utils/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py) | **HF-trainer 侧的 Ray 核心**：`RayHelper` 类，定义 `worker`/`function` 装饰器、worker 创建、远程派发与结果回收。 |
| [swift/ray_utils/arguments.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/arguments.py) | `RayArguments` 数据类：`use_ray` / `ray_exp_name` / `device_groups` 三个字段。 |
| [swift/ray_utils/__init__.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/__init__.py) | 导出 `RayHelper`、`RayArguments`，并提供启动入口 `try_init_ray()`。 |
| [swift/ray_utils/resource_manager.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/resource_manager.py) | `ResourceManager`：把 `device_groups` 翻译成 Ray placement group，做 rank→节点→GPU 的映射。 |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | `SwiftSft`：用 `@RayHelper.worker` / `@RayHelper.function` 装饰，是 HF-trainer 侧 Ray 的典型消费者。 |
| [swift/cli/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py) | `swift sft` 真正干活的脚本，在其中调用 `try_init_ray()`。 |
| [swift/ray/megatron/pipeline.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py) | **megatron-ray 侧的总编排器**：`MegatronRayPipeline`，解析 YAML、建资源池、拉起 worker/rollout、串训练器。 |
| [swift/ray/megatron/worker_group.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/worker_group.py) | `WorkerGroup` + `dispatch_collect` 装饰器：一组 Ray actor 的「派发-回收」抽象。 |
| [swift/ray/megatron/resource_pool.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/resource_pool.py) | `ResourcePool`：用 `STRICT_PACK` placement group 锁定「一个 PG 一台机」，并探测物理 GPU 序号。 |
| [swift/ray/megatron/megatron_worker.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/megatron_worker.py) | `MegatronWorker`：每个 Ray actor 内部持有一份 Megatron 训练器与可选的 rollout 适配器。 |
| [swift/ray/megatron/driver_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/driver_utils.py) | `RayConfig` / `parse_ray_yaml`：把 megatron-ray 的 YAML 拆成「Ray 配置 + 各角色配置 + 共享配置」三段。 |
| [docs/source_en/Instruction/Ray.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Ray.md) | 官方 Ray 文档，含「Megatron Ray」与「Swift Ray」两节，是本讲的权威说明。 |

> **先记住一个重要事实**：ms-swift 里其实有 **两套** Ray 实现，互不调用：
> 1. **`swift/ray_utils/`（Swift Ray）**：面向 **HF Trainer**（`SwiftSft`、采样/蒸馏），靠 `RayHelper` 的**装饰器**把本地方法调用透明转成 Ray 远程调用。
> 2. **`swift/ray/megatron/`（megatron-ray）**：面向 **Megatron 的 GRPO/GKD**，是一套自包含的 `Pipeline + WorkerGroup + ResourcePool`，**不使用 `RayHelper`**，而是直接基于 Ray 原语构建。
>
> 这两套分别解决不同场景，本讲会先把第一套讲透，再讲第二套，并指出它们的区别。

## 4. 核心概念与源码讲解

### 4.1 RayHelper 装饰器机制：双模透明的魔法

#### 4.1.1 概念说明

`RayHelper` 想解决的问题是：**让同一份训练代码，在单机时直接跑、在多机时自动变成 Ray 分布式，而不需要写两套逻辑。**

设想你写了一个 `SwiftSft` 类，里面有一堆 `_prepare_xxx` 和 `run` 方法。单机时，你希望它们就是普通的方法调用；当你加了 `--use_ray true`，你希望这些方法被「派发」到集群里某个 GPU 组上的 Ray actor 里执行，结果再收回来。如果用传统写法，你需要到处写 `if use_ray: ray.get(actor.xxx.remote()) else: self.xxx()`，代码会非常啰嗦。

`RayHelper` 用 Python 装饰器把这个分支收敛掉了：你在类上标 `@RayHelper.worker(group=[...])`，在方法上标 `@RayHelper.function(group=...)`，剩下的派发逻辑全部交给装饰器内部判断。判断的依据只有一个——**当前进程是否已经初始化了 Ray、当前进程是 driver 还是 worker**。这就是「双模透明」：装饰器会在运行时根据 Ray 状态决定是「本地直调」还是「远程派发」。

这套思路来自 ROLL（阿里的一套 RL 框架，源码注释里写明 *Some code borrowed from ROLL*）。它的设计哲学是：**角色不在「类」的层面硬编码，而在「函数」的层面声明**——一个类可以同时扮演多个角色（一个 group），不同方法分属不同 group，由参数决定它们落在哪片硬件上。

#### 4.1.2 核心流程

整个机制围绕「两种身份」展开。在一个 Ray 会话里，进程要么是 **driver**（发起者，通常是 rank 0 的主进程），要么是 **worker**（被 driver 拉起的 Ray actor）。`RayHelper` 的核心判定有三个：

```
RayHelper.ray_inited()  → 当前进程是否已 ray.init()
RayHelper.is_worker()   → 当前进程是否是 Ray worker（而非 driver）
RayHelper.is_called_from_init() → 当前调用栈里是否有 __init__
```

`@RayHelper.worker(group)` 装饰类时，发生这些事：

1. **若 Ray 未初始化**（单机模式）：直接返回原类，装饰器相当于空操作，类就是普通类。
2. **若当前进程是 worker**：也返回原类——因为 worker 进程里这个类要在本地实例化、本地执行。
3. **若当前进程是 driver 且 Ray 已初始化**：
   - 把类用 `ray.remote(cls)` 包成「可远程实例化的类」，登记到 `RayHelper.worker_cls[group]`。
   - **篡改类的 `__init__`**：新的 `__init__` 在 driver 调用时，不是真的 new 一个对象，而是调用 `RayHelper._create_workers(group, ...)` 去集群上拉起若干 actor；在 worker 调用时，才真正执行原始 `__init__`。

`@RayHelper.function(group)` 装饰方法时，用 `functools.wraps` 包出一个 `wrapper`，它的判定逻辑是：

```
若 Ray 未初始化            → 本地直调 func(self, ...)           # 单机
若当前进程是 worker：
    若方法属于本 worker 的 group → 本地执行 func(...)
    否则（init 期间跨组调用）     → 返回 None（静默跳过）
若当前进程是 driver：
    若在 __init__ 期间           → 返回 None（让每个 worker 自己 init）
    否则                         → execute_all_sync(...) 远程派发并 collect 结果
```

最关键的一行直觉是：**driver 上「调用一个被装饰的方法」=「向一组 actor 发远程调用」**。这就是「本地调用平滑转远程调用」的实现方式。

派发（dispatch）有三种模式，回收（collect）也有几种，签名如下（见 [swift/ray_utils/base.py:141-145](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L141-L145)）：

- `dispatch`：`'all'`（所有 worker 收到相同参数）/ `'slice'`（把列表按 worker 数切片，做负载均衡）/ 一个自定义函数。
- `execute`：`'first'`（只让 rank0 执行）/ `'all'`（所有 worker 执行）。
- `collect`：`'none'`（原样返回各 worker 结果列表）/ `'flatten'`（拍平）/ 自定义函数。

#### 4.1.3 源码精读

**① `RayHelper` 的核心状态与初始化** —— [swift/ray_utils/base.py:23-53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L23-L53)

```python
class RayHelper:
    resource_manager: Optional[ResourceManager] = None
    worker_cls: Dict = {}          # group -> ray.remote(类)
    worker_instance: Dict = {}     # group -> [actor_handle, ...]
    initialized = False
    device_groups: Dict[str, Any] = None

    @staticmethod
    def initialize(device_groups: Dict[str, Any]):
        if RayHelper.ray_inited():
            return
        import ray
        RayHelper.device_groups = device_groups
        ray.init()
        if RayHelper.resource_manager is None:
            RayHelper.resource_manager = ResourceManager(device_groups)
```

注意 `RayHelper` 是一个**全静态的「工具箱」**（所有方法和属性都是 `@staticmethod`/类变量），它的状态（`worker_cls`/`worker_instance`/`resource_manager`）在整个进程内全局共享。`initialize` 做两件事：`ray.init()` 建立会话，再用 `ResourceManager(device_groups)` 把 YAML 里的设备分组翻译成 placement group。

**② `@RayHelper.worker` 装饰器** —— [swift/ray_utils/base.py:90-118](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L90-L118)

```python
@staticmethod
def worker(group: Union[str, List[str]]):
    def decorator(cls):
        if not RayHelper.ray_inited():
            return cls                      # 单机：原样返回
        if RayHelper.is_worker():
            return cls                      # worker 进程：原样返回（本地实例化）
        cls.decorated = True
        groups = [group] if isinstance(group, str) else group
        import ray
        _cls = ray.remote(cls)              # driver：包成可远程实例化的类
        for g in groups:
            RayHelper.worker_cls[g] = _cls  # 登记到路由表

        init_method = cls.__init__
        @functools.wraps(init_method)
        def new_init(self, *args, **kwargs):
            if not RayHelper.is_worker():
                RayHelper._create_workers(group, *args, **kwargs)  # driver：拉 actor
            init_method(self, *args, **kwargs)                      # worker：真正初始化
        cls.__init__ = new_init
        return cls
    return decorator
```

这一段是「双模透明」的心脏。driver 上 `SwiftSft(args)` 这一行，触发的不是构造一个本地对象，而是 `_create_workers('default', args)`——它在集群上拉起若干个 actor（见 ③）。而同样的 `SwiftSft(args)` 在 worker 进程里，则正常执行原始 `__init__`。

**③ `function` 装饰器：driver 派发 / worker 执行** —— [swift/ray_utils/base.py:163-189](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L163-L189)

```python
@functools.wraps(func)
def wrapper(self, *args, **kwargs) -> T:
    if not RayHelper.ray_inited():
        return func(self, *args, **kwargs)          # 单机：本地直调
    if RayHelper.is_worker():
        if not hasattr(self, 'group'):
            self.group = os.environ['RAY_SWIFT_GROUP'].split(',')
        if group not in self.group:
            if RayHelper.is_called_from_init():
                return None                          # init 期间跨组：静默
            else:
                raise ValueError()
        else:
            return func(self, *args, **kwargs)       # worker 本地执行
    else:
        if RayHelper.is_called_from_init():
            return None                              # driver 在 init 期间不派发
        result = RayHelper.execute_all_sync(group, dispatch, execute, func.__name__, *args, **kwargs)
        return RayHelper.collect_func(collect, result)   # 回收
```

注意 worker 通过环境变量 `RAY_SWIFT_GROUP`（在创建 actor 时注入，见 ④）知道「自己属于哪个 group」，从而判断某个被装饰的方法是否该由自己执行。

**④ 远程派发执行** —— [swift/ray_utils/base.py:198-235](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L198-L235)

```python
@staticmethod
def execute_all_async(group, dispatch, execute, method_name, *args, **kwargs):
    workers = RayHelper.worker_instance[group]
    length = len(workers)
    if execute == 'first':
        return getattr(workers[0], method_name).remote(*args, **kwargs)   # 只 rank0
    elif dispatch == 'all':
        return [getattr(w, method_name).remote(*args, **kwargs) for w in workers]  # 全发
    elif dispatch == 'slice':
        # 把 list/tuple 参数按 worker 数切片（负载均衡）
        ...
```

`execute_all_sync` 只是 `ray.get(execute_all_async(...))` 的同步包装。`slice` 模式会把列表参数用 `divmod` 均匀切片分给各 worker，是「数据并行」的天然实现。

**⑤ 创建 worker（注入分布式环境变量）** —— [swift/ray_utils/base.py:283-311](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L283-L311)

每个 actor 创建时，会通过 `RuntimeEnv(env_vars=...)` 注入一组环境变量，让 actor 内部的 torchrun/NCCL 能正常工作：

```python
env_vars.update({
    'WORLD_SIZE': str(world_size),
    'RANK': str(rank),
    'LOCAL_RANK': str(0),
    'CUDA_VISIBLE_DEVICES': ','.join([str(r) for r in deploy_pg['gpu_rank']]),
    'MASTER_ADDR': ip,
    'MASTER_PORT': str(port),
    'RAY_SWIFT_GROUP': ','.join(local_groups),
    'RAY_SWIFT_ARGS': get_args(),   # 把命令行剩余参数透传给 worker
})
```

可以看到，**Ray actor 内部仍然需要 `WORLD_SIZE`/`RANK`/`MASTER_ADDR` 这套分布式环境变量**——Ray 负责「调度」，actor 内部仍然是标准的 torch 分布式。这正是「Ray 编排 + torch 并行」分层协作的体现。

#### 4.1.4 代码实践

> 对应规格里的实践任务：阅读 `RayHelper` 在 `SwiftSft` 中的用法，说明 `@RayHelper.worker`/`@RayHelper.function` 如何把方法分发到 Ray actor，并尝试用 `try_init_ray` 启动一个 Ray 会话。

**实践目标**：亲眼看到「装饰器随 Ray 状态切换行为」，并启动一个真实的 Ray 会话。

**操作步骤**：

1. 阅读 [swift/pipelines/train/sft.py:21-160](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L21-L160)，注意三处装饰：

   ```python
   @RayHelper.worker(group=['default'])        # 整个类属于 'default' 组
   class SwiftSft(SwiftPipeline, TunerMixin):
       ...
       @RayHelper.function(group='default')
       def _prepare_model_tokenizer(self, **kwargs): ...
       @RayHelper.function(group='default')
       def _prepare_template(self) -> None: ...
       @RayHelper.function(group='default')
       def _prepare_dataset(self): ...
       @RayHelper.function(group='default')
       def run(self): ...
   ```

   在你的笔记里写下：`SwiftSft(args)` 这一行在「单机」时是普通实例化，在「driver + Ray」时是 `_create_workers('default', args)` 拉起一组 actor，在「worker」时是真正跑 `__init__`。

2. 阅读 [swift/cli/sft.py:17-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L17-L18)：

   ```python
   from swift.ray_utils import try_init_ray
   try_init_ray()
   ```

   再看 [swift/ray_utils/__init__.py:7-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/__init__.py#L7-L18)，理解它如何从命令行读 `--use_ray`/`--device_groups`，仅当 `use_ray` 为真时才 `RayHelper.initialize(...)`。这正是「单机直通」的开关：`swift sft` 不带 `--use_ray` 时 `ray_inited()` 恒为 False，所有装饰器退化成空操作。

3. **启动一个 Ray 会话（本地最小实验，无需多机）**。安装 ray 后，新建一个脚本 `ray_probe.py`（这是示例代码，不是项目原有文件）：

   ```python
   # 示例代码
   from swift.ray_utils import RayHelper

   device_groups = {
       'nproc_per_node': 2,
       'default': {
           'device': 'GPU',
           'ranks': list(range(0, 2)),
           'workers': ['default'],
       },
   }
   RayHelper.initialize(device_groups)
   print('ray_inited =', RayHelper.ray_inited())
   print('is_worker  =', RayHelper.is_worker())
   RayHelper.teardown()
   ```

   运行 `python ray_probe.py`。

**需要观察的现象**：日志里出现 Ray 的初始化信息（如 `Connecting to existing Ray cluster...` 或本地 `Started a local Ray instance`），最后打印 `ray_inited = True`、`is_worker = False`（当前是 driver）。若机器没有 GPU，可以把 `device` 改成 `'CPU'`、`ranks` 改成整数（如 `2`），ResourceManager 会走 CPU 分支。

**预期结果**：成功初始化说明 `device_groups` 被正确解析、placement group 创建成功。若报 `ray` 未安装，先 `pip install ray`（或参照 u1-l2 安装 `ray` extras）。**若你无法运行（无 GPU/无权限），请明确标注「待本地验证」，不要假装已运行。**

#### 4.1.5 小练习与答案

**练习 1**：在 `@RayHelper.function` 的 wrapper 里，为什么 driver 在 `__init__` 期间调用被装饰方法时直接返回 None？

**参考答案**：因为构造期各 worker 还没拉起（`_create_workers` 刚刚发起），driver 此时不应该、也无法向它们发远程调用。设计上让「每个 worker 在自己的 `__init__` 里各自完成本地初始化」，driver 的 `__init__` 只负责拉 actor，所以装饰器用 `is_called_from_init()` 检测到调用栈里有 `__init__` 就静默返回 None，避免「在 actor 尚未就绪时派发」。

**练习 2**：`dispatch='slice'` 与 `dispatch='all'` 分别适合什么场景？

**参考答案**：`slice` 适合「数据并行」——你传入一个大数据集/batch，希望被均分到各 worker 上并行处理；`all` 适合「状态同步」——所有 worker 收到完全相同的参数（如加载同一份配置、做一次广播）。

**练习 3**：为什么 `RayHelper` 全部用静态成员，而不是实例化一个 `RayHelper()` 对象？

**参考答案**：因为装饰器在「类定义/方法定义时」就要读取 `RayHelper` 的状态（`ray_inited()` 等），而那时还没有任何实例；且 driver 与 worker 是不同进程，需要一个进程级单例来共享 `worker_cls`/`worker_instance`。静态成员天然满足「进程内全局唯一」。

---

### 4.2 RayArguments 参数注入：从 YAML 到 Ray 会话

#### 4.2.1 概念说明

上一节我们看到，`RayHelper` 的所有行为都由「Ray 是否初始化」这一个布尔量决定。那么这个布尔量由谁打开？答案是 `RayArguments`——一个只有三个字段的小数据类，它通过多继承混入到 `BaseArguments`，从而让 `swift sft` 等命令天然支持 `--use_ray`。

`RayArguments` 的职责很薄：**把命令行/YAML 里的 Ray 开关解析成 Python 对象，并在合适时机触发 `RayHelper.initialize`。** 它本身不做任何分布式逻辑，只是一个「参数载体 + 触发器」。

#### 4.2.2 核心流程

整体注入链路：

```
YAML/命令行  --device_groups / --use_ray
        │
        ▼
RayArguments（混入 BaseArguments / SftArguments）
   __post_init__: ① json.loads(device_groups)  ② 设环境变量 RAY_SWIFT_EXP_NAME
        │
        ▼
swift/cli/sft.py: try_init_ray()
   读 --use_ray / --device_groups → 若 use_ray: RayHelper.initialize(device_groups)
        │
        ▼
RayHelper.initialize → ray.init() + ResourceManager(device_groups)
        │
        ▼
@RayHelper.worker / @RayHelper.function 装饰器「激活」（双模透明生效）
```

三个字段的含义：

- `use_ray: bool = False`：总开关。为 False 时整套 Ray 机制处于「睡眠」状态。
- `ray_exp_name: Optional[str]`：实验名，会被设进环境变量 `RAY_SWIFT_EXP_NAME`，作为 worker 命名前缀（多实验隔离用）。
- `device_groups: Optional[str]`：**JSON 字符串**形式的设备分组，`use_ray=True` 时必填。例如 `'{"nproc_per_node": 4, "default": {"device": "GPU", "ranks": "list(range(0,4))", "workers": ["default"]}}'`。

#### 4.2.3 源码精读

**① RayArguments 数据类** —— [swift/ray_utils/arguments.py:8-30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/arguments.py#L8-L30)

```python
@dataclass
class RayArguments:
    use_ray: bool = False
    ray_exp_name: Optional[str] = None
    device_groups: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.device_groups, str):
            self.device_groups = json.loads(self.device_groups)   # JSON 串 → dict
        if self.ray_exp_name:
            os.environ['RAY_SWIFT_EXP_NAME'] = self.ray_exp_name.strip()
```

注意 `device_groups` 在「命令行」形态下是字符串（`--device_groups '{...}'`），`__post_init__` 把它 `json.loads` 成 dict。但 YAML 形态下 `parse_yaml_args` 会把字典直接展开成 JSON 字符串再传进来，所以这里两种来源都能处理。

**② 混入 BaseArguments** —— [swift/arguments/base_args/base_args.py:47-48](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L47-L48) 与 [swift/arguments/base_args/base_args.py:194](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L194)

```python
class BaseArguments(GenerationArguments, QuantizeArguments, DataArguments, TemplateArguments,
                    ModelArguments, RayArguments):
```

`BaseArguments.__post_init__` 里显式调用 `RayArguments.__post_init__(self)`（这正是 u2-l1 讲过的「mixin 的 `__post_init__` 不会自动链式调用，须手动调」）。于是 `SftArguments`（继承 `BaseArguments`）天然带上了 `--use_ray` / `--device_groups`。

**③ try_init_ray：真正的触发器** —— [swift/ray_utils/__init__.py:7-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/__init__.py#L7-L18)

```python
def try_init_ray():
    import argparse, json
    from transformers.utils import strtobool
    parser = argparse.ArgumentParser()
    parser.add_argument('--use_ray', type=str, default='0')
    parser.add_argument('--device_groups', type=str, default=None)
    args, _ = parser.parse_known_args()
    args.use_ray = strtobool(args.use_ray)
    if args.use_ray:
        RayHelper.initialize(json.loads(args.device_groups))
```

注意它**独立用一个小 argparse 解析**，不依赖完整的 `SftArguments`——因为 Ray 初始化必须发生在「一切业务逻辑之前」（连 `import` 重型依赖都要让位），所以用最小解析器提前抢跑。这也是它叫 `try_init_ray`（试探性初始化）的原因。

**④ ResourceManager 把 device_groups 变成 placement group** —— [swift/ray_utils/resource_manager.py:19-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/resource_manager.py#L19-L92)

device_groups 的 YAML 结构（来自 [examples/train/multi-node/ray/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/multi-node/ray/sft.yaml)）：

```yaml
device_groups:
  nproc_per_node: 4            # 每台机器最少几张卡
  default:                     # 组名（自定义）
    device: GPU                # GPU 或 CPU
    ranks: list(range(0, 4))   # 哪些 rank 归本组（GPU 可写 [0,1,2,3]/4/list(range(...))）
    workers:                   # 本组承载哪些角色
      - default
```

`ResourceManager.__init__` 会：扫描 Ray 集群里所有节点，挑出 GPU 数 ≥ `nproc_per_node` 的节点；为每个节点建一个 placement group（`bundle = {device_type: nproc_per_node, CPU: node_cpu//2+1}`）；再把每个 worker 名字映射到具体的 `(node_rank, gpu_rank, placement_group)`。

关键约束（[swift/ray_utils/resource_manager.py:54-55](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/resource_manager.py#L54-L55)）：所有 rank 必须互不重复（`assert len(set(all_ranks)) == len(all_ranks)`），节点数 `nnodes = ceil(总rank数 / nproc_per_node)` 自动算出。

#### 4.2.4 代码实践

**实践目标**：写一份等价于命令行的多机 SFT YAML，验证 `device_groups` 语法。

**操作步骤**：

1. 打开 [examples/train/multi-node/ray/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/multi-node/ray/sft.yaml) 和 [examples/train/multi-node/ray/sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/multi-node/ray/sft.sh)。脚本只有一行 `swift sft examples/train/multi-node/ray/sft.yaml`，YAML 里关键三段是：
   - 普通训练参数（`model`/`tuner_type`/`dataset` 等）；
   - `use_ray: true`；
   - `device_groups: {nproc_per_node, default: {device, ranks, workers}}`。

2. 模仿它，把 `nproc_per_node` 改成你机器的 GPU 数（如 2），`ranks` 改成 `list(range(0, 2))`，存成 `my_ray_sft.yaml`（示例文件，非项目原有）。

3. （可选）运行 `swift sft my_ray_sft.yaml`。

**需要观察的现象**：启动日志里会出现 `run sh:` 展开后的真实命令（这是 u1-l4 讲过的配置文件展开），其中 `--use_ray` 与 `--device_groups` 被正确传入；随后 Ray 会话初始化，`ResourceManager` 打印节点信息，worker actor 被拉起。

**预期结果**：训练在 Ray 编排下多卡运行。若没有多卡/未装 ray，此命令会失败——这是正常的，请标注「待本地验证」。即便不运行，你也应能解释：**为什么 YAML 里 `device_groups` 写成嵌套 dict，而命令行里要用 JSON 字符串？**（答：YAML 经 `parse_yaml_args` 展开成 `--device_groups '<json>'`，再由 `RayArguments.__post_init__` 的 `json.loads` 还原成 dict。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `try_init_ray` 不复用 `SftArguments` 来解析 `--use_ray`？

**参考答案**：Ray 初始化要尽量早（在拉起重型依赖、构造大参数对象之前），用一个最小 argparse 只解析两个参数，开销最低、时序最早；若等 `SftArguments` 完整解析完再初始化，会拖慢启动并可能在 import 期就触发不必要的副作用。

**练习 2**：`device_groups` 里 `ranks: list(range(0, 4))` 是字符串形式的 Python 表达式，ResourceManager 怎么处理它？

**参考答案**：见 [swift/ray_utils/resource_manager.py:43-52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/resource_manager.py#L43-L52)：先尝试 `int(ranks)`，失败（因为是字符串 `"list(range(0,4))"`）后走 `except`，用 `eval(ranks, {'__builtins__': {'list': list, 'range': range}})` 在受限命名空间里求值，得到 `[0,1,2,3]`。

**练习 3**：若你把 `device_groups` 里两个组的 `ranks` 写重叠了（如都用 `[0,1,2,3]`），会发生什么？

**参考答案**：`ResourceManager` 的断言 `assert len(set(all_ranks)) == len(all_ranks)` 会失败并抛错，防止同一张卡被分给两个角色。

---

### 4.3 megatron-ray 应用：GRPO/GKD 的独立编排栈

#### 4.3.1 概念说明

前面两节讲的是「Swift Ray」（`swift/ray_utils/`），它面向 HF Trainer。但 ms-swift 还有一个更强的训练后端——**Megatron-SWIFT**（u9-l3/u9-l4 讲过），它支持张量/流水/序列并行，适合训超大模型。当 Megatron 跑 GRPO（强化学习）或 GKD（广义知识蒸馏）时，一次训练里**同时存在两类完全不同的角色**：

- **train（训练 worker）**：用 Megatron 做前向/反向，更新策略模型权重。
- **rollout（推理副本）**：用 vLLM 做大规模采样（GRPO 需要为每个 prompt 生成 K 条回答；GKD 需要老师/学生的生成）。
- **teacher（老师模型，仅 GKD）**：可选，独立的 vLLM 推理副本。

这三类角色对 GPU 的需求、对时序的要求完全不同——rollout 是「纯生成」，train 是「前向+反向」，它们的显存占用会冲突。如何把这三类角色优雅地调度到集群的 GPU 上？这就是 **megatron-ray**（`swift/ray/megatron/`）要解决的问题。

**重要澄清**：megatron-ray **不使用 `RayHelper`**。它是一套独立的、更显式的编排栈，核心抽象是：

- `MegatronRayPipeline`：driver 侧总指挥（解析 YAML、建资源池、拉 worker/rollout、构造 trainer）。
- `ResourcePool` + `ResourcePoolManager`：GPU 资源的 placement group 封装。
- `WorkerGroup` + `@dispatch_collect`：一组 Ray actor 的「派发-回收」（概念上类似 `RayHelper.function` 的 dispatch/collect，但是面向 Megatron 的 DP 维度设计的）。
- `MegatronWorker`：每个 Ray actor 内部持有一份 Megatron 训练器。

为什么不再复用 `RayHelper`？官方文档（[docs/source_en/Instruction/Ray.md:103-107](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Ray.md#L103-L107)）解释了 `RayHelper` 的取舍：因为 SWIFT 内部大量复用 transformers/trl 的实现，「以 Ray 角色为中心」去拆解（像 veRL/ROLL 那样）不现实，会让非 Ray 场景支持得很差。所以 `RayHelper` 选了「函数级声明角色」的装饰器路线。而 megatron-ray 是后来为 Megatron RLHF 专门写的全新栈，目标更聚焦（多角色 GPU 调度 + colocate/separate），于是直接基于 Ray 原语构建，不再绕装饰器。

#### 4.3.2 核心流程

megatron-ray 的入口是 `megatron rlhf --use_ray true --config xxx.yaml`。CLI 层的分流很简单——[swift/cli/_megatron/rlhf.py:6-24](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/_megatron/rlhf.py#L6-L24)：

```python
def _use_ray() -> bool:
    if '--use_ray' not in sys.argv:
        return False
    # 从 argv 里摘出 --use_ray [true|false]，返回布尔
    ...

if __name__ == '__main__':
    if _use_ray():
        from swift.ray.megatron.pipeline import main as ray_main
        ray_main()                      # 走 megatron-ray
    else:
        from swift.megatron import megatron_rlhf_main
        megatron_rlhf_main()            # 走 torchrun 版 megatron
```

注意：`--use_ray` 是个「软开关」，被从 `argv` 里 pop 掉后再交给后续逻辑，所以下游的 `MegatronArguments` 看不到它（megatron 侧的 `use_ray` 字段是另一回事，用于校验，见 [swift/megatron/arguments/megatron_args.py:104](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L104)）。

进入 `ray_main()` 后，`MegatronRayPipeline` 的生命周期是（[swift/ray/megatron/pipeline.py:69-94](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py#L69-L94)）：

```
MegatronRayPipeline(config_path)
   __init__: parse_ray_yaml → (RayConfig, group_configs, shared_config)
run():
   init():
      1. _build_dataset() + _compute_train_iters()   # driver 侧先算训练步数
      2. ray.init()
      3. _create_pools()                              # 建 ResourcePool（placement group）
      4. _init_worker_groups()                        # 拉起 train worker 组
      5. _init_rollout_replicas()                     # 拉起 vLLM rollout 副本
      6. _init_teacher_replicas()                     # 可选：GKD 老师
      7. _create_trainer() → GRPOTrainer/GKDTrainer   # driver 侧训练器
   train(): trainer.train()                            # 主循环
   _shutdown(): 关副本、关 worker、销毁 placement group
```

**两种 GPU 放置模式**（这是 megatron-ray 最核心的概念，见 [examples/ray/README.md:32-50](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/ray/README.md#L32-L50)）：

| 模式 | `colocate_groups` | GPU 需求 | 行为 |
|------|-------------------|----------|------|
| **colocate（共享）** | `[[train, rollout]]` | `train.gpus`（train 与 rollout 用同一批卡，gpus 必须相等） | train 与 rollout 轮流占用，靠 `offload_model`/`offload_optimizer`/`sleep_level:1` 释放显存 |
| **separate（独占）** | 不设 | `train.gpus + rollout.gpus`（不相交的两批卡） | train 与 rollout 各占一组，每步把权重推给 rollout |

权重同步方式也随模式变化（[swift/ray/megatron/pipeline.py:345-349](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py#L345-L349)）：colocate 用 `'naive'`（进程内 IPC，因为同一批卡同一进程组），separate 用 `'nccl'`（跨进程组广播）。数据并行的规模可由一个简单关系式算出：

\[ \text{DP} = \frac{\text{gpus}}{\text{TP} \times \text{PP} \times \text{CP}} \]

例如 4 张卡、`tensor_model_parallel_size=2` → DP=2（见 [examples/ray/README.md:128-129](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/ray/README.md#L128-L129)）。

#### 4.3.3 源码精读

**① YAML 三段式解析** —— [swift/ray/megatron/driver_utils.py:59-119](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/driver_utils.py#L59-L119)

```python
@dataclass
class RayConfig:
    rlhf_type: str = 'grpo'
    colocate_groups: List[List[str]] = field(default_factory=list)
    train_gpus: int = 0
    rollout_gpus: int = 0
    teacher_gpus: int = 0
    sleep_level: int = 1
    nnodes: int = 1

def parse_ray_yaml(config_path):
    raw = yaml.safe_load(open(config_path))
    colocate_groups = raw.pop('colocate_groups', [])
    sleep_level = int(raw.pop('sleep_level', 1))
    nnodes = int(raw.pop('nnodes', 1))
    group_configs = {g: raw.pop(g, {}) for g in KNOWN_GROUPS}  # train/rollout/teacher
    gpu_counts = {g: int(cfg.pop('gpus', 0)) for g, cfg in group_configs.items()}
    shared_config = dict(raw)   # 剩下的都是共享参数
    ...
```

`KNOWN_GROUPS = ('train', 'rollout', 'teacher')`（[swift/ray/megatron/driver_utils.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/driver_utils.py#L122)）。YAML 顶层被拆成三部分：Ray 调度参数（`colocate_groups`/`sleep_level`/`nnodes`/各角色 `gpus`）、每个角色的专属参数（`train:`/`rollout:`/`teacher:` 块）、共享训练参数（其余顶层键）。这种设计让一份 YAML 同时描述「怎么调度」和「怎么训练」。

**② colocate 的资源池共享** —— [swift/ray/megatron/pipeline.py:114-145](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py#L114-L145)

```python
for colocated in colocated_sets:
    gpus_by_role = {g: self.group_gpus.get(g, 0) for g in colocated}
    distinct = set(gpus_by_role.values())
    if len(distinct) > 1:
        raise ValueError('Colocated roles must request the same number of gpus ...')
    gpus = distinct.pop()
    pon = self.ray_config.gpus_as_process_on_nodes(gpus)
    shared = ResourcePool(pon, max_colocate_count=len(colocated))  # 共享一个池
    for g in colocated:
        pool_mapping[g] = shared
```

colocate 模式下，`train` 和 `rollout` 共享同一个 `ResourcePool`（`max_colocate_count=2`），所以它们的 gpus 必须相等——这是硬校验。separate 模式则每个角色各建一个池。

**③ ResourcePool：一节点一 placement group** —— [swift/ray/megatron/resource_pool.py:56-80](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/resource_pool.py#L56-L80)

```python
def create(self, device_name='GPU'):
    bundle_template = {device_name: 1, 'CPU': max(self.max_colocate_count, 1)}
    pgs = []
    for n_gpus in self.process_on_nodes:
        bundles = [bundle_template.copy() for _ in range(n_gpus)]
        pg = placement_group(bundles, strategy='STRICT_PACK')   # 强制打包到一台机
        pgs.append(pg)
    ray.get([pg.ready() for pg in pgs])
```

`STRICT_PACK` 策略要求一个 placement group 的所有 bundle 必须落在**同一台物理机**上——这保证了 Megatron 的 TP/PP 通信（NVLink/高速互联）在同一节点内，跨节点只走数据并行。`process_on_nodes` 形如 `[8]`（单机 8 卡）或 `[4,4]`（两机各 4 卡），由 `RayConfig.gpus_as_process_on_nodes` 均分得到（[swift/ray/megatron/driver_utils.py:77-84](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/driver_utils.py#L77-L84)）。

**④ WorkerGroup：派发-回收抽象** —— [swift/ray/megatron/worker_group.py:141-258](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/worker_group.py#L141-L258)

```python
class DispatchMode(str, Enum):
    BROADCAST = 'broadcast'   # 全 worker 相同参数
    DP = 'dp'                 # 按数据并行 rank 取对应分片
    DP_SPLIT = 'dp_split'     # 现场把张量/列表切成 DP 份

class CollectMode(str, Enum):
    ALL = 'all'; DP = 'dp'; DP_FLAT = 'dp_flat'; FIRST = 'first'

def execute(self, method_name, *args, dispatch=BROADCAST, collect=ALL, **kwargs):
    per_worker = self._dispatch(dispatch, args, kwargs)
    futures = [getattr(w, method_name).remote(*a, **kw)
               for w, (a, kw) in zip(self._workers, per_worker)]
    return self._collect(collect, ray.get(futures))
```

这套 `dispatch/collect` 概念上和 `RayHelper.function` 的 `dispatch/collect` 完全一致，但更贴合 Megatron 的 DP 维度——它知道每个 worker 的 `dp_rank` 和 `is_collector`（通过 `build_dispatch_info` 向每个 actor 查询 `get_parallel_info` 得到，[swift/ray/megatron/worker_group.py:176-190](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/worker_group.py#L176-L190)）。worker 方法用 `@dispatch_collect(...)` 声明默认派发/回收策略（如 [swift/ray/megatron/megatron_worker.py:112-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/megatron_worker.py#L112-L126)）：

```python
@dispatch_collect(dispatch='broadcast', collect='first')
def init_teacher_model(self, model_dir): ...

@dispatch_collect(dispatch='dp', collect='first')
def compute_teacher_logits(self, micro_batches): ...
```

**⑤ worker 内部：一份 Megatron 训练器** —— [swift/ray/megatron/megatron_worker.py:65-110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/megatron_worker.py#L65-L110)

每个 `MegatronWorker`（一个 Ray actor）在 `init_actor` 时，用 driver 传来的 cfg dict 构造一份 `MegatronRLHFArguments` 和 `MegatronRLHF` pipeline，再建一个 `_LifecycleTrainer` 持有真正的 Megatron 模型与优化器。也就是说，**actor 内部依然是标准的 Megatron 训练器**，Ray 只负责「把这些训练器分布到不同 GPU、协调它们与 rollout 的时序」。

**⑥ driver 侧训练主循环** —— [swift/ray/megatron/base_trainer.py:234-250](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/base_trainer.py#L234-L250)

```python
def train(self):
    self._prepare_state()
    tg = self.train_group                 # WorkerGroup['train']
    self._build_dataloader()
    args_override = compute_iter_params(self._data_info, tg.dp_size)
    meta = tg.setup(args_override)        # 广播 setup 到所有 train worker
    train_iters = meta['train_iters']
    try:
        iteration = self._train_loop(tg, train_iters, iteration)   # 主循环：rollout→score→train
    finally:
        results = tg.finalize()
```

driver 侧的 `BaseRayTrainer` 同时持有 `worker_groups`（train）和 `rollout_replicas`（vLLM），主循环里交替调用 rollout 采样和 train worker 反向——这正是 GRPO/GKD 的「生成-训练」交错模式（详见 u7-l2 GRPO 算法）。

#### 4.3.4 代码实践

**实践目标**：读懂一份 megatron-ray GRPO colocate 配置，能解释每个角色的 GPU 归属。

**操作步骤**：

1. 打开 [examples/ray/grpo/ray_grpo_colocate.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/ray/grpo/ray_grpo_colocate.yaml) 和 [examples/ray/grpo/run_colocate.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/ray/grpo/run_colocate.sh)。脚本核心是 `megatron rlhf --use_ray true --config ray_grpo_colocate.yaml`。

2. 在 YAML 里找出以下关键行，并写下它们的含义：
   - `colocate_groups: [[train, rollout]]` —— train 与 rollout 共享 GPU。
   - `offload_model: true` / `offload_optimizer: true` / `sleep_level: 1` —— 让空闲角色释放显存。
   - `train: {gpus: 4, ...}` 与 `rollout: {gpus: 4, ...}` —— 两者 gpus 相等（colocate 硬约束）。
   - `rollout: {vllm_tensor_parallel_size: 1, vllm_gpu_memory_utilization: 0.4}` —— rollout 用 vLLM，TP=1。

3. 对比 [examples/ray/grpo/ray_grpo_separate.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/ray/grpo/ray_grpo_separate.yaml)（separate 模式）：它**不设** `colocate_groups`，train 与 rollout 各占 4 张卡（共需 8 张），权重每步用 `nccl` 同步。

4. 用上面的 DP 公式验证：colocate 配置里 `train.gpus=4`、`tensor_model_parallel_size=1` → DP=4。

**需要观察的现象（配置层面）**：两种 YAML 的差异只在 `colocate_groups` 是否存在、`offload_*`/`sleep_level` 是否设置——训练算法本身（GRPO 的 `num_generations`/`beta`/`epsilon` 等）完全一致。这印证了官方文档的结论：Ray 与非 Ray 的**训练功能等价**，差异只在**部署编排**（[docs/source_en/Instruction/Ray.md:12-29](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Ray.md#L12-L29)）。

**预期结果**：你能口述出「colocate 用 4 卡、separate 用 8 卡、单机用非 Ray 最简单」这条选型指南。若要在本地真正跑，需要 4 张以上 GPU 与 megatron extras，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 megatron-ray 不复用 `RayHelper`？

**参考答案**：`RayHelper` 的装饰器路线是为「让 HF Trainer（重度复用 transformers/trl）也能用 Ray」而设计的折中；megatron-ray 是为 Megatron RLHF 专门写的全新栈，需要显式管理多角色（train/rollout/teacher）的 GPU 池与 colocate/separate 模式，这套需求用「Pipeline + WorkerGroup + ResourcePool」的显式抽象更清晰，所以直接基于 Ray 原语构建。

**练习 2**：colocate 模式下，train 与 rollout 共享同一批 GPU，会不会互相把显存撑爆？

**参考答案**：会，所以必须配 `offload_model`/`offload_optimizer`/`sleep_level: 1`。机制是：当前在「训练」时，rollout 的 vLLM 模型被卸载/休眠（释放显存）；切换到「采样」时，反向释放训练权重，唤醒 vLLM。pipeline 用 `_colocate_offload_ctx` 上下文管理器在 vLLM 初始化期间先把 train worker 卸载到 CPU（[swift/ray/megatron/pipeline.py:207-220](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py#L207-L220)）。

**练习 3**：`WorkerGroup.build_dispatch_info` 为什么要向每个 actor 查询 `get_parallel_info`？

**参考答案**：因为 driver 事先不知道每个 actor 在 Megatron 的并行拓扑里是哪个 `dp_rank`、`is_collector`（是否负责收集 DP 结果）。这些信息只有在 actor 内部 `init_actor`、建立 Megatron 进程组之后才能确定，所以 driver 要在 worker 就绪后回查一次，缓存成 `_dp_rank_map`/`_collect_mask`，后续 `_dispatch_dp`/`_collect_dp` 才知道「这块数据该发给哪个 dp_rank」「该从哪个 collector 收结果」。

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「Ray 全景阅读 + 最小启动」任务：

**任务**：在一张表里对比 ms-swift 的两套 Ray 实现，并亲手启动一个 Ray 会话。

**步骤**：

1. **对比表（写在你的学习笔记里）**：填完下表。

   | 维度 | Swift Ray（`swift/ray_utils/`） | megatron-ray（`swift/ray/megatron/`） |
   |------|----------------------------------|----------------------------------------|
   | 面向后端 | HF Trainer（SwiftSft/采样/蒸馏） | Megatron GRPO/GKD |
   | 入口命令 | `swift sft --use_ray true`（经 `try_init_ray`） | `megatron rlhf --use_ray true`（经 `cli/_megatron/rlhf.py` 分流） |
   | 核心抽象 | `RayHelper` + `@worker`/`@function` 装饰器 | `MegatronRayPipeline` + `WorkerGroup` + `ResourcePool` |
   | 角色定义 | 函数级（装饰器声明 group） | 角色级（train/rollout/teacher 三类） |
   | 设备分组 | `device_groups`（ranks/workers） | `colocate_groups` + 各角色 `gpus` |
   | 双模透明 | 有（单机装饰器退化空操作） | 无（专门为多角色调度设计，单机可直接用非 Ray 的 `megatron rlhf`） |

2. **启动 Ray 会话**：执行 4.1.4 里的 `ray_probe.py`，确认 `ray_inited = True`。若你的环境有 ray 但无 GPU，把 `device_groups` 改成 CPU 版：

   ```yaml
   # 示例 device_groups（CPU）
   nproc_per_node: 2
   default:
     device: CPU
     ranks: 2          # CPU 组 ranks 是整数
     workers: [default]
   ```

3. **跟踪一次调用**：在 `ray_probe.py` 成功的基础上，扩展一个被 `@RayHelper.worker`/`@RayHelper.function` 装饰的最小类（参考 [docs/source_en/Instruction/Ray.md:109-150](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Ray.md#L109-L150) 的 `MyTrainer` 例子），先在「不 init Ray」时跑（应本地直调），再在「init Ray」后跑（应远程派发），对比两次行为。

**验收标准**：能口头解释「为什么单机时装饰器像不存在」；能在对比表里准确写出两套栈的入口命令；`ray_probe.py` 成功初始化 Ray 会话（或明确标注待本地验证的卡点）。

## 6. 本讲小结

- ms-swift 有**两套独立的 Ray 实现**：`swift/ray_utils/`（Swift Ray，面向 HF Trainer）和 `swift/ray/megatron/`（megatron-ray，面向 Megatron GRPO/GKD），二者不互相调用。
- `RayHelper` 的「双模透明」靠运行时判断 `ray_inited()`/`is_worker()`：`@RayHelper.worker` 在 driver 上把类包成 `ray.remote` 并篡改 `__init__` 去拉 actor；`@RayHelper.function` 在 driver 上把方法调用转成 `execute_all_sync` 远程派发，在 worker 上本地执行。
- `dispatch`（all/slice/自定义）控制「参数怎么分」，`collect`（none/flatten/自定义）控制「结果怎么收」；`slice` 是天然的「数据并行」。
- `RayArguments` 只有三字段（`use_ray`/`ray_exp_name`/`device_groups`），经多继承混入 `BaseArguments`；真正的初始化由 `try_init_ray()` 在 CLI 早期用一个最小 argparse 触发。
- megatron-ray 用 `MegatronRayPipeline` 做总编排，`ResourcePool`（`STRICT_PACK` 一节点一 PG）管 GPU，`WorkerGroup` + `@dispatch_collect` 管派发回收；核心概念是 **colocate（共享 GPU，需 offload）vs separate（独占 GPU，nccl 同步）**。
- Ray 与非 Ray 训练功能等价，Ray 的价值是**自动化多机/多角色编排**——单机优先用非 Ray，多机集群用 Ray（[docs/source_en/Instruction/Ray.md:23-29](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Ray.md#L23-L29)）。

## 7. 下一步学习建议

- **回到 GRPO 算法（u7-l2）**：本讲的 megatron-ray 是「壳」，真正的 rollout→score→advantage→train 循环在 `swift/rl_core/` 与 `swift/ray/megatron/grpo_trainer.py` 里，建议对照阅读，理解 driver 如何驱动 rollout 副本与 train worker 交错工作。
- **深入 Megatron 并行（u9-l4）**：megatron-ray 的 `WorkerGroup` 依赖 `dp_rank`/`is_collector`，这些概念来自 Megatron 的 TP/PP/DP/CP 划分；若还不熟，重读 u9-l4 的并行参数体系。
- **阅读 ROLL 与 veRL**：`RayHelper` 注释里写了借鉴 ROLL，而 megatron-ray 的 ResourcePool/WorkerGroup 思路与 veRL 相近；对比阅读能加深对「RL 训练系统如何拆角色」的理解。
- **动手扩展**：尝试用 `@dispatch_collect` 写一个自定义的 worker 方法（参考 [swift/ray/megatron/megatron_worker.py:112-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/megatron_worker.py#L112-L126)），或用 `register_ray_trainer`（[swift/ray/megatron/pipeline.py:27-44](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray/megatron/pipeline.py#L27-L44)）注册一个自定义的 Ray RLHF 算法，作为本单元的收尾实战。
