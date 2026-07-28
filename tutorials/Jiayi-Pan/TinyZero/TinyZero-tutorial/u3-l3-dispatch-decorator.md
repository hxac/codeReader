# Dispatch 机制：注册与数据分发

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `@register` 装饰器如何用一个「魔数属性」`MAGIC_ATTR` 给方法打上 `dispatch_mode` 标记，以及 `WorkerGroup` 又是如何在创建时扫描这些标记、把方法重新绑定成「分发—执行—收集」的统一壳子。
- 读懂 `Dispatch` / `Execute` 两个枚举，并能对照 `get_predefined_dispatch_fn` 这张映射表，说出每个模式对应的 `dispatch_fn`（分发函数）与 `collect_fn`（收集函数）。
- 区分 `ONE_TO_ALL`（广播：所有 worker 拿同一份输入）与 `DP_COMPUTE_PROTO`（按数据并行切分：`chunk` 切开 → 各算各的 → `concat` 拼回），并理解 `_split_args_kwargs_data_proto` / `collect_dp_compute_data_proto` 如何与上一讲 [u3-l1 DataProto](u3-l1-dataproto-protocol.md) 的 `chunk`/`concat` 严丝合缝。
- 能在 `fsdp_workers.py` 里准确定位 `init_model` / `update_actor` / `generate_sequences` 用了哪种 Dispatch，并解释「为什么初始化用广播、训练用切分」。

## 2. 前置知识

本讲是「数据协议与单控制器」单元的第三篇，承接 [u3-l2 Single Controller 与 Ray 资源池](u3-l2-single-controller-ray-pool.md)。上一讲我们已经建立了这条调用链：

- driver 进程不裸调 `ray.get(actor.method.remote(...))`，而是通过 `WorkerGroup` 调方法。
- group 把每个被 `@register` 标记的方法，用 `func_generator` 包成了同一个三段式骨架：**dispatch（分发）→ execute（执行）→ collect（收集）**。
- 当时我们故意留下了悬念：「dispatch / collect 具体怎么切数据」由方法上的 `@register(dispatch_mode=...)` 决定——这正是本讲的主题。

需要先回忆的几个基础概念（都来自前两讲）：

- **DataProto**：veRL 的统一数据货箱，由 `batch`（张量）、`non_tensor_batch`（非张量）、`meta_info`（全局元信息）三段组成；它能被 `chunk(chunks=n)` 沿第 0 维等分成 `n` 份，也能被 `concat(list)` 沿第 0 维拼回来。
- **worker / rank / world_size**：一个 worker 就是一个 Ray actor、一个独立进程；`world_size` 是这组 worker 的进程总数，也就是数据要被切成的份数。
- **数据并行（DP）**：把一个大 batch 平均切成 `world_size` 份，每个 worker 处理一份，再汇总结果。

一句话定位本讲：**`@register` 是方法上的「配送方式标签」，`Dispatch` 是标签的取值集合，`dispatch_*` / `collect_*` 函数是真正搬货的工人**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/single_controller/base/decorator.py` | **本讲主角**。定义魔数 `MAGIC_ATTR`、`Dispatch`/`Execute` 枚举、所有 `dispatch_*` / `collect_*` 函数、映射表 `get_predefined_dispatch_fn`，以及装饰器 `register` 本身。 |
| `verl/single_controller/base/worker_group.py` | `WorkerGroup._bind_worker_method`：扫描类里所有带 `MAGIC_ATTR` 的方法，查出对应的 dispatch/collect/execute 函数，用 `func_generator` 重新绑定到 group 上。 |
| `verl/single_controller/ray/base.py` | `func_generator`（三段式骨架）与 `RayWorkerGroup.execute_all_async`（真正按 rank 切分参数并发起远程调用）。 |
| `verl/workers/fsdp_workers.py` | 真实用例：`ActorRolloutRefWorker` / `CriticWorker` / `RewardModelWorker` 的方法上大量使用 `@register`，是本讲实践的对象。 |
| `verl/protocol.py` | `DataProto.chunk` / `DataProto.concat`：Dispatch 切分与合并的底层实现。 |

## 4. 核心概念与源码讲解

### 4.1 `@register` 装饰器与 MAGIC_ATTR 标记机制

#### 4.1.1 概念说明

回忆一下问题：driver 调用 `wg.update_actor(data)` 时，group 怎么知道这个方法该「广播」还是「切分」？答案分成两层。

1. **打标签**：开发者在 Worker 子类的方法上写 `@register(dispatch_mode=Dispatch.XXX)`，装饰器并不改变方法的功能，只是在函数对象上**偷偷挂一个属性**，记录它的配送方式。
2. **读标签**：`RayWorkerGroup` 创建时遍历这个类的所有方法，凡是带这个属性的，就按属性里的 `dispatch_mode` 查出对应的 `dispatch_fn` / `collect_fn`，再用 `func_generator` 生成一个包装函数，覆盖（`setattr`）到 group 的同名方法上。

这个属性名是一个故意起得很难撞名的字符串，叫 **`MAGIC_ATTR`**：

[verl/single_controller/base/decorator.py:22-22](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L22-L22) — 定义魔数属性名 `attrs_3141562937`。注释解释：用一个魔数是为了避免和用户自定义函数上已有的同名属性冲突。

之所以要这样「绕一圈」用属性标记，而不是让开发者直接传 dispatch 函数，是为了**解耦**：开发者只写业务方法 + 一行装饰器，所有「数据怎么搬」的复杂性都被 `WorkerGroup` 在运行时注入，业务代码保持干净。

#### 4.1.2 核心流程

`@register` 打标签、`_bind_worker_method` 读标签的完整流程：

```text
开发者写：
    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def update_actor(self, data: DataProto): ...

register() 做的事：
    inner = 原 update_agent 的包装（@wraps 保留函数名）
    setattr(inner, MAGIC_ATTR, {'dispatch_mode', 'execute_mode', 'blocking'})
    返回 inner

RayWorkerGroup 创建时：
    _bind_worker_method(cls, func_generator)
      for 每个方法 in dir(cls):
          if hasattr(方法, MAGIC_ATTR):               # 只绑被 @register 标记的
              读出 dispatch_mode / execute_mode / blocking
              dispatch_fn, collect_fn = get_predefined_dispatch_fn(dispatch_mode)
              execute_fn = self.execute_all / self.execute_rank_zero
              func = func_generator(self, 方法名, dispatch_fn, collect_fn, execute_fn, blocking)
              setattr(self, 方法名, func)              # 用壳子覆盖原方法
```

于是 `wg.update_actor(data)` 实际跑的是 `func_generator` 生成的壳子，而不是 worker 类里那个原始方法。原始方法只在每个 worker 进程**内部**被远程调用时才执行。

#### 4.1.3 源码精读

装饰器本体：它接受 `dispatch_mode` / `execute_mode` / `blocking` / `materialize_futures` 四个参数，返回一个 `decorator`，`decorator` 再返回包了 `@wraps` 的 `inner`，并把四个参数存进 `MAGIC_ATTR`：

[verl/single_controller/base/decorator.py:394-410](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L394-L410) — `register` 装饰器。`@wraps(func)` 保留原函数名（否则 `setattr` 时找不到对应方法名）；`inner` 里的 `_materialize_futures` 会在 worker 内部真正执行前把 `DataProtoFuture` 解包成 `DataProto`；最后 `setattr(inner, MAGIC_ATTR, attrs)` 完成打标签。注意默认值是 `Dispatch.ALL_TO_ALL`、`Execute.ALL`、`blocking=True`，所以**裸写 `@register`（不传参）等价于「直通 + 全员执行 + 阻塞」**。

读标签的代码在 `WorkerGroup._bind_worker_method`：

[verl/single_controller/base/worker_group.py:150-158](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker_group.py#L150-L158) — `for method_name in dir(cls)` 扫描所有方法，`if hasattr(method, MAGIC_ATTR)` 只挑出被 `@register` 标记的，再用 `getattr(method, MAGIC_ATTR)` 取出那个字典。

[verl/single_controller/base/worker_group.py:161-194](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker_group.py#L161-L194) — 拿到 `dispatch_mode` 后：若它是 `Dispatch` 枚举，走 `get_predefined_dispatch_fn` 查表（下一节讲）；若它是 `dict`（用户自定义），直接取 `'dispatch_fn'`/`'collect_fn'`。然后用 `func_generator` 生成壳子并 `setattr(self, method_name, func)` 覆盖到 group 上。

注意一个常被忽略的细节：`colocate`（合体）场景下，`create_colocated_worker_cls` 也会把内层 worker 类的方法重新绑定到外层 `WorkerDict` 上，并且**显式地把 `MAGIC_ATTR` 也拷过去**，好让外层 group 仍能识别这些方法。见：

[verl/single_controller/ray/base.py:393-405](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L393-L405) — `_bind_workers_method_to_parent` 里 `if hasattr(method, MAGIC_ATTR)` 同样只绑被标记的方法，并 `setattr(func, MAGIC_ATTR, getattr(method, MAGIC_ATTR))` 把标签透传，使合体后的进程对外仍是一组「带标签」的方法。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`@register` 只打标签、不改功能」。

**操作步骤**（纯 Python，无需 GPU/Ray）：

1. 在任意 Python 解释器里执行下面这段「示例代码」（不是项目原有代码）：

   ```python
   from verl.single_controller.base.decorator import register, Dispatch, MAGIC_ATTR

   class MyWorker:
       @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
       def update_actor(self, data):
           return "trained"

       @register()  # 用默认值
       def ping(self, x):
           return x

   for name in ["update_actor", "ping"]:
       fn = getattr(MyWorker, name)
       print(name, "→", getattr(fn, MAGIC_ATTR))
   ```

2. 观察打印的字典，确认 `update_actor` 的 `dispatch_mode` 是 `Dispatch.DP_COMPUTE_PROTO`，而 `ping` 用了默认的 `Dispatch.ALL_TO_ALL`。

**需要观察的现象**：两个方法都被挂上了 `MAGIC_ATTR`，且 `update_actor(data)` 仍能正常返回 `"trained"`（说明装饰器没有破坏原功能）。

**预期结果**：打印形如 `{'dispatch_mode': <Dispatch.DP_COMPUTE_PROTO: 9>, 'execute_mode': <Execute.ALL: 0>, 'blocking': True}`。若你的环境未安装 verl，可改为 `待本地验证`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MAGIC_ATTR` 要用一个看似随机的长数字字符串，而不是直接叫 `"dispatch_mode"`？

**参考答案**：因为这个属性会被挂在「用户自定义的函数对象」上。如果叫 `"dispatch_mode"`，万一用户的函数本身恰好也有同名属性就会冲突、被覆盖。用一个几乎不可能撞名的魔数字符串（`attrs_3141562937`）作为命名空间隔离，是最朴素的防冲突手段。

**练习 2**：`@register()`（不传任何参数）和完全不写 `@register`，对一个方法来说有什么区别？

**参考答案**：完全不写时，方法不会被 `MAGIC_ATTR` 标记，`_bind_worker_method` 扫描时会跳过它，于是 group 上**不存在**这个方法的壳子，driver 无法通过 `wg.xxx()` 远程调用它。写了 `@register()` 则用默认值（`ALL_TO_ALL` + `ALL` + 阻塞）打上标签，group 会为它生成一个「直通」壳子，可被 driver 调用。

---

### 4.2 Dispatch / Execute 枚举与 predefined 映射表

#### 4.2.1 概念说明

`MAGIC_ATTR` 里的 `dispatch_mode` 取值来自 `Dispatch` 枚举。可以把它理解成一份「配送方式目录」：每种方式对应一对「怎么发（`dispatch_fn`）」和「怎么收（`collect_fn`）」。这张目录就在 `get_predefined_dispatch_fn` 里写死。

`execute_mode` 取值来自 `Execute` 枚举，它只控制「让谁干」：

- `Execute.ALL`：所有 worker 都执行（对应 `execute_all`）。
- `Execute.RANK_ZERO`：只有 rank 0 执行（对应 `execute_rank_zero`）。

`Dispatch` 枚举则同时控制「数据怎么搬」和「结果怎么收」。其取值可以按命名规律分成几组：

| 命名规律 | 含义 | 典型场景 |
| --- | --- | --- |
| `ONE_TO_ALL` | 一份输入广播给所有 worker；输出原样返回 | `init_model`、`save_checkpoint` |
| `ALL_TO_ALL` | 直通：参数由调用方自己组织成 `world_size` 长度的 list | 自定义分发、`execute_func_rank_zero` |
| `DP_COMPUTE*` | 数据并行：按 `world_size` 切分 | 训练/前向计算 |
| `MEGATRON_*` | Megatron 后端的 tp/pp 切分 | `strategy=megatron` |
| 带 `_PROTO` 后缀 | 在 `DataProto` 上做 `chunk`/`concat` | 输入是 `DataProto` |
| 不带 `_PROTO` 后缀 | 输入是用户预先切好的 list/tuple，框架不切 | 输入是普通序列 |

TinyZero 用的是 FSDP 后端，所以真正会碰到的是 `ONE_TO_ALL`、`ALL_TO_ALL`、`DP_COMPUTE_PROTO` 这三种；`MEGATRON_*` 留给 [u7-l4 Megatron 后端](u7-l4-megatron-backend-rm-worker.md)。

#### 4.2.2 核心流程

`_bind_worker_method` 拿到 `dispatch_mode` 后，靠 `get_predefined_dispatch_fn` 这张「查表函数」拿到一对函数：

```text
dispatch_mode (枚举)
      │
      ▼
get_predefined_dispatch_fn(dispatch_mode)
      │
      ▼  返回一个 dict：
{ 'dispatch_fn': <把整体数据切成每 rank 一份的函数>,
  'collect_fn' : <把各 rank 输出合并回整体的函数> }
```

这张表是一个普通字典，键是 `Dispatch` 枚举值，值是 `{'dispatch_fn', 'collect_fn'}`。所以「加一种新配送方式」理论上只要往表里加一个键值对（或用自定义 dict 模式）。

此外还有一种**自定义模式**：`@register(dispatch_mode={'dispatch_fn': my_dispatch, 'collect_fn': my_collect})`。`_check_dispatch_mode` 允许 `dispatch_mode` 是 `Dispatch` 或 `dict`，后者让你完全绕开预定义表，自己写切分与合并逻辑。

#### 4.2.3 源码精读

两个枚举的定义：

[verl/single_controller/base/decorator.py:25-42](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L25-L42) — `Dispatch`（12 个值）与 `Execute`（2 个值）。注意 `Dispatch.RANK_ZERO = 0` 虽然定义了，但在 `get_predefined_dispatch_fn` 里**没有对应条目**——它是个预留值，目前未启用。

查表函数，本讲最重要的一张表：

[verl/single_controller/base/decorator.py:300-347](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L300-L347) — `get_predefined_dispatch_fn`。逐条对照可以看到：`ONE_TO_ALL` → `(dispatch_one_to_all, collect_all_to_all)`；`DP_COMPUTE_PROTO` → `(dispatch_dp_compute_data_proto, collect_dp_compute_data_proto)`；`ALL_TO_ALL` → `(dispatch_all_to_all, collect_all_to_all)`。这张表就是把「枚举标签」翻译成「搬运工人」的字典。

Execute 的查表函数更简单，只返回一个方法名字符串：

[verl/single_controller/base/decorator.py:350-363](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L350-L363) — `get_predefined_execute_fn` 返回 `{'execute_fn_name': 'execute_all'}` 或 `'execute_rank_zero'`，`_bind_worker_method` 再用 `getattr(self, wg_execute_fn_name)` 从 group 上取出真正的执行函数（见 `worker_group.py:174-183`）。

自定义模式的合法性校验：

[verl/single_controller/base/decorator.py:366-372](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L366-L372) — `_check_dispatch_mode` 允许 `dispatch_mode` 是 `Dispatch` 或 `dict`；若是 `dict`，必须同时含 `'dispatch_fn'` 和 `'collect_fn'` 两个键。仓库里的 `tests/ray/test_worker_group_basics.py` 就用自定义 dict 模式实现了一个 `two_to_all_dispatch_fn`。

#### 4.2.4 代码实践

**实践目标**：把 `get_predefined_dispatch_fn` 当成一本「配送方式手册」来查。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 `verl/single_controller/base/decorator.py` 的 `get_predefined_dispatch_fn`（300-347 行）。
2. 填下面这张表（答案在下一小节）：

   | `Dispatch` 模式 | `dispatch_fn` | `collect_fn` | 是否对 DataProto 做 chunk/concat |
   | --- | --- | --- | --- |
   | `ONE_TO_ALL` | ？ | ？ | ？ |
   | `ALL_TO_ALL` | ？ | ？ | ？ |
   | `DP_COMPUTE` | ？ | ？ | ？ |
   | `DP_COMPUTE_PROTO` | ？ | ？ | ？ |
   | `DP_COMPUTE_METRIC` | ？ | ？ | ？ |

3. 注意观察 `DP_COMPUTE_METRIC`：它的 `dispatch_fn` 复用了 `dispatch_dp_compute_data_proto`，但 `collect_fn` 用的是 `collect_dp_compute`（**不 concat**）。思考这意味着什么。

**需要观察的现象**：你会发现「带 `_PROTO` 的 dispatch_fn」都调用 `_split_args_kwargs_data_proto` 做 `chunk`，而「带 `_PROTO` 的 collect_fn」都走 `_concat_data_proto_or_future` 做 `concat`；不带 `_PROTO` 的则假定输入已经是 list、不切分。

**预期结果**：填表答案——`ONE_TO_ALL`/`dispatch_one_to_all`/`collect_all_to_all`/否；`ALL_TO_ALL`/`dispatch_all_to_all`/`collect_all_to_all`/否；`DP_COMPUTE`/`dispatch_dp_compute`/`collect_dp_compute`/否；`DP_COMPUTE_PROTO`/`dispatch_dp_compute_data_proto`/`collect_dp_compute_data_proto`/是；`DP_COMPUTE_METRIC`/`dispatch_dp_compute_data_proto`/`collect_dp_compute`/「分发时切，收集时不 concat」。

#### 4.2.5 小练习与答案

**练习 1**：`DP_COMPUTE_METRIC` 的 `collect_fn` 故意用 `collect_dp_compute`（不 concat），为什么？提示：metric 长什么样？

**参考答案**：metric 通常是一个字典（如 `{'actor/pg_loss': ...}`）或标量，每个 worker 都算出一份**整体相同含义**的统计量，driver 需要的是「每 rank 一份的列表」以便自己求平均/求 max，而不是把它们沿 batch 维 `concat` 成一个更大的 DataProto。所以分发时仍按 DataProto 切（让每个 worker 只在自己那份数据上算 metric），但收集时保持 list 形态、不 concat。

**练习 2**：如果一个方法既不传 `DataProto`、也不需要任何切分，只想让所有 worker 用相同参数各跑一次，该用哪个模式？

**参考答案**：`Dispatch.ONE_TO_ALL`。它的 `dispatch_one_to_all` 会把每个参数复制 `world_size` 份，`collect_all_to_all` 原样返回各 worker 输出的列表，完全契合「相同输入、各自执行」的需求。

---

### 4.3 ONE_TO_ALL vs DP_COMPUTE_PROTO：广播与切分

#### 4.3.1 概念说明

这是本讲最核心的对比，也是理解 `fit()` 训练主循环数据流的关键。

- **`ONE_TO_ALL`（广播）**：driver 的一份输入，**原封不动**地发给每一个 worker。所有 worker 拿到完全相同的参数，各自独立执行，结果互不拼接。适合「每个 worker 都要做同一件事」的场景，比如各自从磁盘加载同一份模型权重（`init_model`）、各自保存自己的 checkpoint（`save_checkpoint`）。

- **`DP_COMPUTE_PROTO`（数据并行切分）**：driver 的一份大 `DataProto`，先沿第 0 维 `chunk` 成 `world_size` 份，第 `i` 份发给第 `i` 个 worker；各 worker 在自己的分片上计算，输出再沿第 0 维 `concat` 拼回一份完整的 `DataProto`。适合「数据要并行处理、结果要汇总」的场景，比如用整个 batch 更新策略（`update_actor`）、生成回答（`generate_sequences`）、算价值（`compute_values`）。

用一个直觉比喻：

- `ONE_TO_ALL` 像**广播通知**：「全体注意，按同一份说明书各自初始化！」
- `DP_COMPUTE_PROTO` 像**分发任务包**：「这堆活儿平均分给在座各位，干完把成果拼一起交回来。」

注意 `DP_COMPUTE_PROTO` 的「切」和「拼」正是 [u3-l1 DataProto](u3-l1-dataproto-protocol.md) 讲过的 `DataProto.chunk` 与 `DataProto.concat`。Dispatch 层只是把它们包进了协议里。这也解释了为什么 `chunk` 要求等分（`len(self) % chunks == 0`）：因为 `world_size` 个 worker 必须各拿一份一样大的数据。

#### 4.3.2 核心流程

`ONE_TO_ALL` 的数据流：

```text
driver: wg.init_model()            # 无数据参数
   dispatch_one_to_all: 无参数 → 无需复制
   execute_all:         每个 worker 调 .init_model.remote()，各自加载模型
   (blocking) ray.get:  等所有 worker 加载完
   collect_all_to_all:  原样返回 [None, None, ...]（init_model 无返回值）
```

`DP_COMPUTE_PROTO` 的数据流（以 `update_actor(data)` 为例）：

```text
driver: wg.update_actor(data: DataProto)   # data.batch_size = N
   dispatch_dp_compute_data_proto:
       data.chunk(chunks=world_size)        # 切成 world_size 份，每份 N/world_size 行
   execute_all:
       worker_i.update_actor.remote(第 i 份)  # 每个 worker 在自己分片上算
   (blocking) ray.get:                      # 等所有 worker 算完
   collect_dp_compute_data_proto:
       DataProto.concat([各 worker 的输出])   # 沿 dim=0 拼回完整 DataProto
```

用最简形式写出这两种模式对单个 `DataProto` 参数 `x`（batch 维大小为 \(N\)，world_size 为 \(W\)）的变换：

\[ \text{ONE\_TO\_ALL}: \quad x \;\mapsto\; [\underbrace{x, x, \dots, x}_{W \text{ 份}}] \quad (\text{不拼接输出}) \]

\[ \text{DP\_COMPUTE\_PROTO}: \quad x \;\mapsto\; [x_0, x_1, \dots, x_{W-1}] \;\mapsto\; f_0(x_0), \dots, f_{W-1}(x_{W-1}) \;\mapsto\; \text{concat}(\cdot) \]

其中 \(x_i = x.\text{chunk}(W)[i]\)，要求 \(N \bmod W = 0\)。

#### 4.3.3 源码精读

先看通用的切分助手 `_split_args_kwargs_data_proto`，它是所有「`_PROTO` 分发函数」的公共底座：

[verl/single_controller/base/decorator.py:45-57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L45-L57) — 对每个位置参数和关键字参数，断言它是 `DataProto` 或 `DataProtoFuture`，然后调用 `arg.chunk(chunks=chunks)`，返回「每 rank 一份」的列表。`dispatch_dp_compute_data_proto` 传进去的 `chunks` 就是 `worker_group.world_size`。

`ONE_TO_ALL` 的分发与收集，非常简短：

[verl/single_controller/base/decorator.py:60-71](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L60-L71) — `dispatch_one_to_all` 把每个参数 `arg` 变成 `[arg] * world_size`（复制 \(W\) 份）；`collect_all_to_all` 直接 `return output`（原样返回各 worker 输出的列表，不合并）。这正是「广播输入、不拼输出」。

`DP_COMPUTE_PROTO` 的分发函数：

[verl/single_controller/base/decorator.py:272-276](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L272-L276) — `dispatch_dp_compute_data_proto` 一行核心：调用 `_split_args_kwargs_data_proto(worker_group.world_size, *args, **kwargs)`，即「按 world_size 对每个 DataProto 做 chunk」。

`DP_COMPUTE_PROTO` 的收集函数，负责把各 rank 的输出拼回来：

[verl/single_controller/base/decorator.py:289-297](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L289-L297) — `collect_dp_compute_data_proto` 先断言每个输出是 `DataProto` 或 `ObjectRef`（异步 future），再调 `collect_dp_compute`（只做长度校验、不变形），最后用 `_concat_data_proto_or_future` 沿 dim=0 拼接。

拼接助手同时兼容同步与异步两种情况：

[verl/single_controller/base/decorator.py:129-144](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L129-L144) — `_concat_data_proto_or_future`：若输出是 `DataProto` 就 `DataProto.concat(output)`（同步）；若是 `ray.ObjectRef`（即异步模式下的 future）就 `DataProtoFuture.concat(output)`（延迟到真正 `get` 时才拼）。这就是 `blocking=False` 也能工作的原因。

最后，把这一切串起来的壳子还是 [u3-l2](u3-l2-single-controller-ray-pool.md) 讲过的 `func_generator`：

[verl/single_controller/ray/base.py:36-46](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/base.py#L36-L46) — `func` 依次执行 `dispatch_fn` → `execute_fn` → （若 `blocking` 则 `ray.get`）→ `collect_fn`。`ONE_TO_ALL` 和 `DP_COMPUTE_PROTO` 的区别，全在于这里注入的 `dispatch_fn`/`collect_fn` 不同，壳子本身完全一样。

#### 4.3.4 代码实践

**实践目标**：用一个最小 DataProto，亲眼看到 `ONE_TO_ALL` 复制、`DP_COMPUTE_PROTO` 切分再拼接的区别。

**操作步骤**（示例代码，单进程模拟，无需 Ray/GPU）：

```python
# 示例代码：直接调用底层 dispatch/collect 函数，模拟 group 行为
from verl.protocol import DataProto
import torch
from types import SimpleNamespace

# 造一个假的 worker_group，只提供 world_size 属性
fake_wg = SimpleNamespace(world_size=2)

# 构造一个 batch_size=4 的 DataProto（仅含一个张量列）
data = DataProto.from_dict(tensors={'x': torch.arange(8).reshape(4, 2)})

# ---- 模拟 ONE_TO_ALL 的 dispatch ----
from verl.single_controller.base.decorator import dispatch_one_to_all
args, _ = dispatch_one_to_all(fake_wg, data)
print("ONE_TO_ALL 分发份数:", len(args), "每份相同?", torch.equal(args[0].batch['x'], args[1].batch['x']))

# ---- 模拟 DP_COMPUTE_PROTO 的 dispatch ----
from verl.single_controller.base.decorator import dispatch_dp_compute_data_proto, collect_dp_compute_data_proto
splits, _ = dispatch_dp_compute_data_proto(fake_wg, data)
print("DP_COMPUTE_PROTO 切分:", [int(s.batch.batch_size[0]) for s in splits])

# 假装每个 worker 原样返回自己的分片，再 collect 拼回
merged = collect_dp_compute_data_proto(fake_wg, list(splits))
print("拼回后 batch_size:", int(merged.batch.batch_size[0]),
      "与原始一致?", torch.equal(merged.batch['x'], data.batch['x']))
```

**需要观察的现象**：
- `ONE_TO_ALL` 分发出 2 份，且两份内容完全相同（是复制，不是切分）。
- `DP_COMPUTE_PROTO` 把 4 行切成 2 份、每份 2 行。
- `collect` 把两份 2 行拼回 4 行，且与原始张量逐元素相等。

**预期结果**：依次打印 `2 True`、`[2, 2]`、`4 True`。若环境未装 verl/torch，标注「待本地验证」，但你应当能根据源码推断出这些结果。

#### 4.3.5 小练习与答案

**练习 1**：如果把上面的 `data` 改成 `batch_size=5`、`world_size=2`，`dispatch_dp_compute_data_proto` 会发生什么？

**参考答案**：会报错。因为 `DataProto.chunk` 要求等分（`protocol.py:491-492` 断言 `len(self) % chunks == 0`），而 5 不能被 2 整除。实际训练里，`fit()` 在切分前会用 `pad_dataproto_to_divisor` 把 batch 补到 `world_size` 的整数倍，正是为了避免这个问题（见 [u3-l1](u3-l1-dataproto-protocol.md)）。

**练习 2**：为什么 `ONE_TO_ALL` 的 `collect_fn` 用的是「什么都不做」的 `collect_all_to_all`，而不是像 `DP_COMPUTE_PROTO` 那样 concat？

**参考答案**：因为 `ONE_TO_ALL` 场景下各 worker 的输出**不是同一个 batch 的分片**（没有可沿 dim=0 拼接的语义）。例如 `init_model` 各 worker 都返回 `None`，`save_checkpoint` 各 worker 各存各的盘，这些输出之间是「并列」关系而非「拼合」关系，driver 直接拿到一个列表即可，强行 concat 反而会出错。

---

### 4.4 真实用法：fsdp_workers 里的 @register 决议

#### 4.4.1 概念说明

理论讲完，回到 TinyZero 真实代码。`verl/workers/fsdp_workers.py` 里的三个 Worker 类（`ActorRolloutRefWorker`、`CriticWorker`、`RewardModelWorker`）几乎每个对外方法都标了 `@register`。这些标签不是随便选的，而是**由「这个方法的输入是不是需要被切分的数据」决定的**：

- 输入是「全员的配置/路径」，每个 worker 各干各的 → `ONE_TO_ALL`。
- 输入是「一个要并行处理的大 `DataProto`」→ `DP_COMPUTE_PROTO`。

记住这条判据，你就能在不读 dispatch 实现的前提下，反推任何方法的配送方式。

#### 4.4.2 核心流程

以 `ActorRolloutRefWorker` 为例，它的对外方法和标签对应关系：

| 方法 | 标签 | 输入 | 为什么 |
| --- | --- | --- | --- |
| `init_model(self)` | `ONE_TO_ALL` | 无数据参数 | 每个 worker 用同一份 config 各自加载模型 |
| `update_actor(self, data)` | `DP_COMPUTE_PROTO` | 训练 batch | 数据并行：切 batch → 各自更新 → 拼 metric |
| `generate_sequences(self, prompts)` | `DP_COMPUTE_PROTO` | prompt batch | 数据并行：切 prompt → 各自生成 → 拼 response |
| `compute_ref_log_prob(self, data)` | `DP_COMPUTE_PROTO` | batch | 数据并行：切 batch → 各自算 ref log prob → 拼回 |
| `save_checkpoint(self, local_path, ...)` | `ONE_TO_ALL` | 路径字符串 | 每个 worker 拿同一份路径，但只有 rank 0 真存盘 |

可以看到：凡是参数里有 `data: DataProto` 的，都是 `DP_COMPUTE_PROTO`；凡是只有 `self` 或路径/配置的，都是 `ONE_TO_ALL`。

#### 4.4.3 源码精读

`ActorRolloutRefWorker` 的初始化与更新方法，标签对比鲜明：

[verl/workers/fsdp_workers.py:284-285](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L284-L285) — `init_model` 用 `@register(dispatch_mode=Dispatch.ONE_TO_ALL)`。方法签名是 `def init_model(self)`，**没有数据参数**，每个 worker 只依据 `self.config` 加载属于自己的那份 FSDP 模型、rollout 引擎、ref policy。

[verl/workers/fsdp_workers.py:355-356](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L355-L356) — `update_actor` 用 `@register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)`。签名 `def update_actor(self, data: DataProto)`，`data` 是整个训练 batch，必须切分到各 worker 做数据并行更新。

[verl/workers/fsdp_workers.py:400-401](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L400-L401) — `generate_sequences` 同样是 `DP_COMPUTE_PROTO`，输入 `prompts: DataProto`。

`CriticWorker` 完全遵循同一规律：

[verl/workers/fsdp_workers.py:651-652](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L651-L652) — Critic 的 `init_model` 也是 `ONE_TO_ALL`（无数据参数，各自建价值网络）。

[verl/workers/fsdp_workers.py:698-699](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L698-L699) — Critic 的 `update_critic` 是 `DP_COMPUTE_PROTO`（输入 `data: DataProto`）。

甚至基类 `Worker` 自己也用了两种较少见的模式，可作为延伸阅读：

[verl/single_controller/base/worker.py:178-186](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/worker.py#L178-L186) — `execute_with_func_generator` 用 `DP_COMPUTE_PROTO_WITH_FUNC`（把一个 Python 函数也广播给各 worker），`execute_func_rank_zero` 用 `ALL_TO_ALL` + `Execute.RANK_ZERO`（只在 rank 0 上跑任意函数）。这展示了「自定义 execute_mode + 带函数的分发」两种进阶用法。

#### 4.4.4 代码实践

**实践目标**（本讲的实践任务）：在 `fsdp_workers.py` 中定位 `update_actor`、`generate_sequences`、`init_model` 的 Dispatch，并解释「为什么 `init_model` 用 `ONE_TO_ALL` 而 `update_actor` 用 `DP_COMPUTE_PROTO`」。

**操作步骤**：

1. 用下面命令在仓库根目录搜索所有 `@register` 标签及紧跟的方法名（只读分析，不改动源码）：

   ```bash
   grep -n -A1 "@register(dispatch_mode" verl/workers/fsdp_workers.py
   ```

2. 从输出中摘出 `ActorRolloutRefWorker` 的三行：
   - `init_model`（约 284 行）→ `Dispatch.ONE_TO_ALL`
   - `update_actor`（约 355 行）→ `Dispatch.DP_COMPUTE_PROTO`
   - `generate_sequences`（约 400 行）→ `Dispatch.DP_COMPUTE_PROTO`

3. 阅读这三个方法签名，回答下面两个问题（见「预期结果」）。

**需要观察的现象**：`init_model(self)` 没有任何数据参数；而 `update_actor(self, data: DataProto)` 和 `generate_sequences(self, prompts: DataProto)` 都接受一个 `DataProto`。

**预期结果（关键解释）**：

- **为什么 `init_model` 用 `ONE_TO_ALL`**：初始化阶段没有「需要被切分的训练数据」。每个 worker 要做的，是依据同一份 `self.config` 各自从磁盘加载模型权重、构建 FSDP 包裹、建 rollout 引擎。这是一件「全员各干一遍、输入完全相同」的事，所以用广播：`dispatch_one_to_all` 把（空的）参数复制 `world_size` 份，每个 worker 跑同一个 `init_model`，互不依赖、无需汇总。如果误用 `DP_COMPUTE_PROTO`，框架会对（不存在的）`DataProto` 参数做 `chunk`，直接报错。

- **为什么 `update_actor` 用 `DP_COMPUTE_PROTO`**：`update_actor` 的输入 `data` 是一整个训练 batch（`batch_size` 可能上千），必须做**数据并行**——把它切成 `world_size` 份，第 `i` 个 worker 只在自己的分片上算 PPO 梯度并更新，再把各 worker 产出的 metric 拼回交给 driver。这正是 `dispatch_dp_compute_data_proto`（`chunk`）+ `collect_dp_compute_data_proto`（`concat`）的用途。如果误用 `ONE_TO_ALL`，每个 worker 都会拿到**完整 batch**重复计算，既浪费算力，又会让梯度更新步数翻倍、结果错误。

- **`generate_sequences` 同理**：prompt batch 要切给各 worker 并行生成，再拼回完整 response，所以也是 `DP_COMPUTE_PROTO`。

一句话总结判据：**有 `DataProto` 要并行处理 → `DP_COMPUTE_PROTO`；无数据、全员同构 → `ONE_TO_ALL`**。

> 备注：若你已配置好 Ray + GPU，可进一步运行 `tests/ray/test_worker_group_basics.py`，它用一个最小 Worker 演示了 `ONE_TO_ALL`/`ALL_TO_ALL`/自定义 dict 三种模式，是「待本地验证」的可运行补充。

#### 4.4.5 小练习与答案

**练习 1**：`ActorRolloutRefWorker.save_checkpoint` 用的是哪种 Dispatch？为什么？提示：看它的签名和 477 行附近的标签。

**参考答案**：用的是 `ONE_TO_ALL`（见 `fsdp_workers.py:477-478`）。签名 `save_checkpoint(self, local_path, hdfs_path=None)` 没有要切分的 `DataProto`，只有路径字符串——每个 worker 拿到同一份路径；方法内部再用 `if self.rank == 0` 判定只有 rank 0 真正落盘，其余 worker 只参与 `torch.distributed.barrier()` 同步。所以「全员收到广播、但内部自决谁干活」。

**练习 2**：如果新增一个方法 `get_actor_weights(self) -> DataProto`，希望每个 worker 都返回自己那份模型权重、再由 driver concat 汇总，该用哪种 Dispatch？为什么不是 `DP_COMPUTE_PROTO`？

**参考答案**：这是个**易错点**。`DP_COMPUTE_PROTO` 不合适，因为它的 `dispatch_dp_compute_data_proto` 会对**输入**做 `chunk`，而这里**没有输入 DataProto 要切**（签名只有 `self`）。正确做法是用 `ALL_TO_ALL`（输入无 DataProto、直通；各 worker 各返回一个 `DataProto`），但默认 `collect_all_to_all` 不 concat。若想自动 concat，应走自定义 dict 模式：`@register(dispatch_mode={'dispatch_fn': dispatch_all_to_all, 'collect_fn': collect_dp_compute_data_proto})`——输入直通、输出按 DataProto concat。这正体现了 `_PROTO` 系列「输入有 DataProto 才切」的设计前提。

---

## 5. 综合实践

把本讲三件事（打标签、查表、切分/广播）串起来，完成一次「Dispatch 侦探」任务：

1. **定位**：在 `verl/workers/fsdp_workers.py` 中找出 `CriticWorker` 和 `RewardModelWorker` 的所有 `@register` 方法，填一张表（方法名 / Dispatch / 输入是否含 `DataProto`）。
2. **验证判据**：检查「含 `DataProto` 输入 → `DP_COMPUTE_PROTO`；否则 → `ONE_TO_ALL`」这条规律是否对所有方法都成立（提示：`RewardModelWorker.compute_rm_score` 在 983 行，应符合规律）。
3. **追壳子**：挑 `compute_values`（`fsdp_workers.py:673-674`，`DP_COMPUTE_PROTO`），在脑中（或纸面上）画出 driver 调用 `critic_wg.compute_values(data)` 时，`func_generator` 的四步分别发生什么：`dispatch` 时 `data` 被切成几份？`execute` 时每个 worker 收到什么？`collect` 时如何拼回？返回给 `fit()` 的是什么？
4. **延伸**：阅读 `tests/ray/test_colocated_workers.py`（约 30、43 行各有一个 `DP_COMPUTE_PROTO` 的 `add`/`sub` 方法），理解它在 colocate 场景下如何验证「切分 → 各算 → 拼回」的正确性。

完成后，你应当能对着任意一个带 `@register` 的方法，立刻说出它的数据在 driver 与 worker 之间是怎么流动的——这正是阅读 [u4 PPO 训练主流程](u4-l1-main-ppo-entry.md) 各阶段调用前必须具备的能力。

## 6. 本讲小结

- `@register` 不改变方法功能，只是用魔数属性 `MAGIC_ATTR` 把 `dispatch_mode`/`execute_mode`/`blocking` 挂在函数对象上；`WorkerGroup._bind_worker_method` 扫描这些标签，用 `func_generator` 把方法重绑成「分发—执行—收集」的壳子。
- `Dispatch` 枚举是「配送方式目录」，`get_predefined_dispatch_fn` 把每个枚举值翻译成一对 `dispatch_fn`/`collect_fn`；`Execute` 枚举只决定「全员执行（ALL）还是只 rank 0 执行（RANK_ZERO）」。还支持用自定义 dict 绕开预定义表。
- `ONE_TO_ALL` = 广播：`dispatch_one_to_all` 把参数复制 `world_size` 份，`collect_all_to_all` 原样返回列表，适合 `init_model`/`save_checkpoint` 这类「无数据、全员同构」的场景。
- `DP_COMPUTE_PROTO` = 数据并行：`dispatch_dp_compute_data_proto` 调 `DataProto.chunk(world_size)` 切分，`collect_dp_compute_data_proto` 调 `DataProto.concat` 拼回，底层是 `_split_args_kwargs_data_proto` 与 `_concat_data_proto_or_future`（后者兼容同步 `DataProto` 与异步 `ObjectRef`）。
- 真实判据：**方法参数里有 `data: DataProto` 要并行处理 → `DP_COMPUTE_PROTO`；只有 `self` 或路径/配置 → `ONE_TO_ALL`**。TinyZero 的 `fsdp_workers.py` 完全遵守这条规律。
- Dispatch 层是 [u3-l1 DataProto](u3-l1-dataproto-protocol.md) 的 `chunk`/`concat` 与 [u3-l2 WorkerGroup](u3-l2-single-controller-ray-pool.md) 的 `func_generator` 之间的粘合层：它把「货箱怎么切、怎么拼」固化成可声明的协议。

## 7. 下一步学习建议

下一讲进入 [u4 PPO 训练主流程](u4-l1-main-ppo-entry.md)，从 `main_ppo` 入口开始阅读 driver 是如何**调用**这些被 `@register` 绑定的方法的。届时你会反复看到这样的调用：

- `actor_rollout_wg.generate_sequences(batch)` —— 触发 `DP_COMPUTE_PROTO`，prompt 被切分、response 被拼回；
- `critic_wg.compute_values(batch)` —— 同样 `DP_COMPUTE_PROTO`；
- 各 worker group 的 `init_model()` —— 触发 `ONE_TO_ALL`。

建议在进入 u4 前，先回头确认你已经能回答：**driver 调一个 group 方法时，数据在哪一步被切、哪一步被拼、哪一步阻塞等待**。如果想更深入，可继续阅读：

- `tests/ray/test_worker_group_basics.py`：最小化演示 `ONE_TO_ALL`/`ALL_TO_ALL`/自定义 dict 三种模式，是理解 Dispatch 的最佳可运行样例。
- `examples/ray/tutorial.ipynb`：官方教程 notebook，含一个自定义 `two_to_all_dispatch_fn` 的例子，展示如何手写 dispatch 函数。
- `docs/workers/fsdp_workers.rst`：官方对 FSDP 各方法 Dispatch 选择的一句话说明，可作为本讲的对照参考。
