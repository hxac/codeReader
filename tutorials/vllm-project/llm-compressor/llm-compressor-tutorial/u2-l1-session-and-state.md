# CompressionSession 与 State

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `CompressionSession` 在 llm-compressor 里扮演的「会话容器」角色，以及它和 `State`、`CompressionLifecycle`、`Recipe` 之间的持有关系。
- 掌握 `session.initialize() / event() / finalize() / reset()` 四个方法各自做了什么、返回值是什么（`ModifiedState`）。
- 读懂 `State` 这个 dataclass 的每个字段含义，理解 `Data`、`Hardware`、`ModifiedState` 三个配套数据类。
- 理解 `active_session() / create_session() / reset_session()` 这组全局会话函数，以及它们背后的「线程局部存储」机制。
- 能写一段最小代码，拿到当前会话、打印它的 `State` 默认字段，并对比 `reset()` 前后生命周期标志的变化。

## 2. 前置知识

本讲是进入 `core`（核心引擎）的第一篇。在开始前，请先在脑海里建立下面几个来自前面讲义的印象：

- **modifier / recipe / oneshot 三件套**（来自 u1-l1、u1-l2）：`modifier` 封装一个压缩动作，`recipe` 是一串有序 `modifier`，`oneshot` 是把它们跑起来的主入口。
- **oneshot 的三阶段**（来自 u1-l4）：`pre_process` → `apply_recipe_modifiers` → `post_process`，其中校准阶段会驱动一个全局的 `CompressionSession`。
- **「门面只转发」**（来自 u1-l3）：顶层包/子包的 `__init__.py` 往往只做转发与聚合，真正的逻辑在更深的文件里；判断公开 API 要看「实际 import」而不是只看 `__all__`。

如果这些概念你还不太熟，建议先快速回看 u1-l3 和 u1-l4 再继续。本讲会回答一个关键问题：**oneshot 跑起来的时候，模型、校准数据、recipe 到底被放在哪里，又是谁在推进它们？** 答案就是 `CompressionSession` 和它持有的 `State`。

## 3. 本讲源码地图

本讲只围绕 `src/llmcompressor/core/` 下的四个文件：

| 文件 | 作用 | 本讲用到的关键符号 |
| --- | --- | --- |
| [src/llmcompressor/core/session.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py) | 定义会话容器 `CompressionSession` | `CompressionSession`、`initialize/finalize/event/reset/reset_stage` |
| [src/llmcompressor/core/state.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py) | 定义压缩状态的数据载体 | `State`、`Data`、`Hardware`、`ModifiedState` |
| [src/llmcompressor/core/session_functions.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) | 提供全局会话函数与回调门面 | `create_session`、`active_session`、`reset_session`、`LifecycleCallbacks` |
| [src/llmcompressor/core/__init__.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/__init__.py) | 聚合并重新导出 `core` 子包的公开符号 | 上述所有符号对外暴露的统一入口 |

补充理解会用到两个相邻文件（不在本讲最小模块内，但 `CompressionSession` 依赖它们）：
- [src/llmcompressor/core/lifecycle.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py) 里的 `CompressionLifecycle`，是 session 真正委托干活的对象（精读留到 u2-l2）。
- [src/llmcompressor/core/events/event.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py) 里的 `EventType`，是 `event()` 方法的入参类型。

## 4. 核心概念与源码讲解

### 4.1 CompressionSession：持有 lifecycle 与 state 的会话容器

#### 4.1.1 概念说明

把一次压缩任务想象成一次「开会」：

- 会议室里有一份**正在压缩的模型**、一份**校准数据**、一份**recipe**，这些是会议的「资料」，统一放在 `State` 里。
- 会议有一个**议程推进者**（`CompressionLifecycle`），它负责按顺序调用每个 modifier 的初始化、事件、收尾方法。
- `CompressionSession` 就是这间「会议室」本身：它不亲自压缩模型，而是把 lifecycle 和 state 装在一起，对外提供 `initialize / event / finalize / reset` 这样语义清晰的入口。

关键直觉：**`CompressionSession` 是个很薄的壳**。它几乎没有自己的状态，只持有一个 `_lifecycle`，而 `state`、`recipe`、生命周期标志全都住在 lifecycle 里面。session 的方法基本是「转发给 lifecycle，再把结果包成 `ModifiedState` 还给调用者」。

#### 4.1.2 核心流程

session 的四个核心方法构成一条标准的「压缩生命周期」调用链：

```
session.initialize(recipe=..., model=..., calib_data=...)
    └── lifecycle.initialize(...)
          ├── state.update(model=..., calib_data=...)   # 把资料放进 state
          ├── recipe = Recipe.create_instance(...)       # 编译 recipe
          └── for mod in recipe.modifiers: mod.initialize(state)
          └── lifecycle.initialized_ = True
    └── 返回 ModifiedState(model, optimizer, loss, modifier_data)

session.event(EventType.CALIBRATION_START, ...)
    └── lifecycle.event(event_type, ...)
          └── 校验事件顺序 → 遍历 recipe.modifiers 调 mod.update_event(state, event)
    └── 返回 ModifiedState(...)

session.finalize(...)
    └── lifecycle.finalize(...)
          └── 遍历 recipe.modifiers 调 mod.finalize(state)
          └── lifecycle.finalized = True
    └── 返回 ModifiedState(...)

session.reset()
    └── lifecycle.reset()
          └── finalizes 残留未收尾的 modifier → 再把 lifecycle 各字段重置为默认值
```

三个「对外」方法（`initialize/event/finalize`）的返回值统一是 `ModifiedState`，它把「当前 state 里的 model/optimizer/loss」和「各 modifier 本次返回的数据」打包，方便调用方（比如 oneshot）拿到结果。

session 与它依赖对象的**持有关系**如下（这是本讲最重要的一张图）：

```
CompressionSession            (core/session.py，普通 class)
   └── _lifecycle: CompressionLifecycle   (core/lifecycle.py，@dataclass)
          ├── state:    State             ← session.state 实际指向这里
          ├── recipe:   Recipe
          ├── initialized_: bool          ← 注意带下划线
          ├── finalized:   bool           ← 注意没有下划线（命名不一致，见 4.1.3 提示）
          ├── global_step: int
          ├── _last_event_type / _event_order   ← 事件顺序校验
          └── initialize() / event() / finalize() / reset()
```

也就是说：`session.state` 并不是 session 自己的字段，而是 `session._lifecycle.state`。这一点在源码里一目了然，但初读时很容易忽略，记住它能帮你省掉很多困惑。

#### 4.1.3 源码精读

**session 只持有 lifecycle。** 构造函数只有一行，`state` / `recipe` 都不在 session 自己身上：

[session.py:L49-L50](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L49-L50) —— `__init__` 仅创建一个 `CompressionLifecycle`。

```python
def __init__(self):
    self._lifecycle = CompressionLifecycle()
```

**`state` 是个只读属性，转手返回 lifecycle 的 state：**

[session.py:L63-L72](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L63-L72) —— 这印证了上一节的持有关系图。

```python
@property
def state(self) -> State:
    ...
    return self._lifecycle.state
```

同理 `lifecycle` 也是只读属性（[session.py:L52-L61](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L52-L61)），直接返回 `self._lifecycle`。

**`initialize()`：转发 + 包装返回值。** 它接收一大堆参数（recipe、model、optimizer、各种 data、调度参数等），全部交给 lifecycle，再把 lifecycle 的返回值和当前 state 里的 model/optimizer/loss 一起包成 `ModifiedState`：

[session.py:L74-L145](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L74-L145) —— 关键是末尾的 `ModifiedState(...)`：

```python
mod_data = self._lifecycle.initialize(recipe=recipe, ..., **kwargs)
return ModifiedState(
    model=self.state.model,
    optimizer=self.state.optimizer,
    loss=self.state.loss,
    modifier_data=mod_data,
)
```

**`event()` 和 `finalize()`** 结构完全一样（[session.py:L165-L189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L165-L189)、[session.py:L147-L163](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L147-L163)）：转发给 lifecycle，包成 `ModifiedState` 返回。注意 `event()` 的注释里还留了一句 `# TODO: is this supposed to be a different type?`，说明返回的 `loss` 字段在事件路径上的语义尚有遗留疑问，阅读时心里有数即可。

**`reset()` 同样只转发：**

[session.py:L191-L195](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L191-L195) —— 真正的重置逻辑在 lifecycle 里。

```python
def reset(self):
    """Reset the session to its initial state"""
    self._lifecycle.reset()
```

那 lifecycle.reset() 做了什么？它会先尝试 finalize 掉还没收尾的 modifier，再调用 `self.__init__()` 把 dataclass 的所有字段重置回默认值（[lifecycle.py:L54-L71](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L54-L71)）。所以一次 reset 之后，`initialized_` 变回 `False`、`finalized` 变回 `False`、`global_step` 变回 `0`、`recipe.modifiers` 变回空列表。

**一个真实调用链（最重要）。** oneshot 在校准阶段正是这样使用全局 session 的：

[oneshot.py:L229-L270](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L229-L270) —— 先 `reset()`，再 `initialize(...)`，中间直接读写 `session.state`，最后 `finalize()`。

```python
session = active_session()
session.reset()
...
session.initialize(model=..., recipe=..., calib_data=calibration_dataloader, ...)
session.state.enable_compile = self.dataset_args.enable_compile   # 直接写 state 字段
pipeline = CalibrationPipeline.from_modifiers(session.lifecycle.recipe.modifiers, ...)
pipeline(model, calibration_dataloader, ...)
session.finalize()
```

这段代码同时演示了三件事：①用 `active_session()` 拿到全局会话；②`session.state` 是可以直接被外部读写的「共享黑板」（这里写入了 `enable_compile`）；③`session.lifecycle.recipe.modifiers` 是管线挑选校准策略的依据。这正好把本讲三个最小模块串了起来。

> **阅读提示（命名不一致）**：lifecycle 上有两个布尔标志，一个叫 `initialized_`（带下划线），另一个叫 `finalized`（不带）。`reset_stage()` 里写的就是这两种不同写法（[session.py:L197-L202](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L197-L202)）。读源码时别被这个不一致绊倒——它不是笔误，而是历史遗留。

#### 4.1.4 代码实践

**目标**：亲手调用 session 的 `initialize / reset`，观察 `ModifiedState` 返回值与 lifecycle 标志的变化。

**操作步骤**（不需要 GPU，纯 CPU 即可）：

1. 新建一个 `exp_session.py`，写入下面的内容。
2. 运行 `python exp_session.py`。

```python
# 示例代码
from llmcompressor import create_session, active_session

with create_session() as session:
    print("session 类型:", type(session).__name__)           # CompressionSession
    print("lifecycle 类型:", type(session.lifecycle).__name__) # CompressionLifecycle

    # initialize 不传 recipe 也会把 initialized_ 置 True（创建一个空 Recipe）
    ret = session.initialize()
    print("initialize 返回:", ret)                            # ModifiedState(model=None, ...)
    print("initialized_ =", session.lifecycle.initialized_)   # True

    session.reset()
    print("reset 后 initialized_ =", session.lifecycle.initialized_)  # False
    print("reset 后 global_step =", session.lifecycle.global_step)    # 0
    print("reset 后 modifiers =", session.lifecycle.recipe.modifiers) # []
```

**需要观察的现象**：`initialize()` 在没有 recipe 时也能成功执行（内部创建空 `Recipe()`），并把 `initialized_` 翻成 `True`；`reset()` 之后这个标志被清掉，`global_step` 归零，`recipe.modifiers` 变成空列表。

**预期结果**：脚本依次打印 `True`、`False`、`0`、`[]`，且 `initialize()` 的返回值是一个 `ModifiedState` 对象。如果 `initialize()` 不传参数时报错，请检查你的 llmcompressor 版本与本讲 HEAD（`2d7a7ea`）一致。结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`CompressionSession.__init__` 里没有 `self._state = ...`，那为什么 `session.state` 还能拿到一个 `State` 对象？

**参考答案**：因为 `session.state` 是一个只读属性，它返回 `self._lifecycle.state`；而 `CompressionLifecycle` 是 dataclass，其 `state` 字段用了 `field(default_factory=State)`，所以 lifecycle 一被创建，`state` 就是一个空的 `State()` 实例。

**练习 2**：`session.reset()` 之后立刻调用 `session.event(EventType.CALIBRATION_START)` 会发生什么？为什么？

**参考答案**：会抛出 `ValueError: Cannot invoke event before initializing`。因为 `lifecycle.event()` 开头会检查 `if not self.initialized_` 就报错（[lifecycle.py:L165-L167](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L165-L167)），而 reset 把 `initialized_` 置回了 `False`。这提示我们事件必须发生在 `initialize` 与 `finalize` 之间。

### 4.2 State / Data / Hardware / ModifiedState：压缩状态的数据载体

#### 4.2.1 概念说明

如果说 `CompressionSession` 是会议室，那 `State` 就是会议室中央那块「共享黑板」：所有压缩相关的资料都写在它上面，modifier 在执行时都来这块黑板上读写。它解决的问题是——**让一长串 modifier 共享同一份上下文，而不必把 model/data/hardware 在它们之间来回传参**。

`State` 周围还有三个小数据类：

- `Data`：把训练/验证/测试/校准四种数据集分槽放好。
- `Hardware`：记录设备、rank、world_size、是否分布式等硬件信息。
- `ModifiedState`：session 三个方法对外返回的「结果快照」，携带 model/optimizer/loss 和各 modifier 的返回数据。

它们都是 `@dataclass`，是纯数据容器，没有复杂逻辑。

#### 4.2.2 核心流程

`State` 在生命周期中的流转：

```
session.initialize(model=..., calib_data=..., ...)
    └── state.update(model=..., calib_data=..., ...)   # 把资料写进 state.data.calib 等
          └── 数据默认会被 deepcopy 一份（copy_data=True）

各 modifier 执行时：
    mod.initialize(state)        # 从 state 读 model、往 state 写自己的中间结果
    mod.update_event(state, ...) # 同上，state 是共享上下文

外部也可直接读写 state（如 oneshot 里 session.state.enable_compile = True）

session 最终把 state.model/optimizer/loss 包进 ModifiedState 返回
```

`state.update()` 的「写」语义要点：
- 只在参数非 `None` 时才覆盖对应字段（防御式赋值）。
- 对 `train/val/test/calib` 四种数据，默认 `copy_data=True` 会做 `deepcopy`，避免外部修改污染 state。
- 额外的 `kwargs`（比如 `device`）会被拣出来塞进 `hardware`，剩下的原样返回（供 lifecycle 继续传递给 modifier）。

#### 4.2.3 源码精读

**`State` 的字段全家福**（默认值就是一张「空压缩现场」的快照）：

[state.py:L97-L108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L97-L108) —— 字段含义见注释。

```python
model: Any = None
teacher_model: Any = None
optimizer: Any = None
optim_wrapped: bool = None
loss: Any = None
batch_data: Any = None
data: Data = field(default_factory=Data)        # 训练/校准等数据集
hardware: Hardware = field(default_factory=Hardware)
loss_masks: list[torch.Tensor] | None = None
current_batch_idx: int = -1                     # 还没开始任何 batch
sequential_prefetch: bool = False               # 逐层校准时是否预取
enable_compile: bool = False                    # 是否开 torch.compile
```

其中 `data` 与 `hardware` 用 `default_factory`，保证每个 `State` 实例都有自己独立的 `Data()` / `Hardware()`，而不是共享同一个类属性。

**`compression_ready` 属性**——一个简单的就绪判断：

[state.py:L110-L120](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L110-L120) —— 注意它要求 model **和** optimizer 都非 None。在 PTQ/oneshot 这类「无优化器」场景里它通常为 `False`，所以**别拿它当「可以开始压缩」的硬性闸门**，它更多是为训练式压缩准备的语义。

```python
@property
def compression_ready(self) -> bool:
    ready = self.model is not None and self.optimizer is not None
    return ready
```

**`update()` 的防御式赋值与 deepcopy：**

[state.py:L188-L207](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L188-L207) —— 关键片段：

```python
if model is not None:
    self.model = model
...
if calib_data is not None:
    self.data.calib = calib_data if not copy_data else deepcopy(calib_data)

if "device" in kwargs:
    self.hardware.device = kwargs["device"]

return kwargs      # 剩余 kwargs 交给上层继续处理
```

注意它把 `device` 这种硬件信息单独挑出来写进 `hardware`，而把其余 `kwargs` 原样返回——这就是为什么 oneshot 能往 `initialize` 里塞 `sequential_targets` 这种自定义参数并让它们流到 modifier。

**三个配套数据类：**

- `Data`：四个可空字段 `train / val / test / calib`（[state.py:L18-L37](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L18-L37)）。oneshot 主要用到的是 `calib`。
- `Hardware`：`device / devices / rank / world_size / local_rank / local_world_size / distributed / distributed_strategy`（[state.py:L40-L70](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L40-L70)），是 DDP 量化的依据（详见 u6-l1）。
- `ModifiedState`：`model / optimizer / loss / modifier_data`（[state.py:L210-L248](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/state.py#L210-L248)），是 session 对外返回的标准「结果包裹」。

#### 4.2.4 代码实践

**目标**：实例化一个空 `State`，把它的默认字段打印出来，并亲手调用 `update()` 看数据是怎么被写进去（且被 deepcopy）的。

**操作步骤**：

```python
# 示例代码
from copy import deepcopy
from llmcompressor.core import State

s = State()
print("model:", s.model)                 # None
print("current_batch_idx:", s.current_batch_idx)   # -1
print("data:", s.data)                   # Data(train=None, val=None, test=None, calib=None)
print("hardware:", s.hardware)           # Hardware(device=None, ...)

calib = {"input_ids": [1, 2, 3]}
s.update(calib_data=calib, copy_data=True)
print("state.data.calib:", s.data.calib)            # {'input_ids': [1, 2, 3]}
print("与原对象同源?", s.data.calib is calib)        # False（被 deepcopy 了）
```

**需要观察的现象**：`update(calib_data=...)` 之后 `state.data.calib` 被填上；但因为 `copy_data=True`，state 里存的是一份深拷贝，`is` 比较为 `False`。

**预期结果**：最后一行打印 `False`。若把 `copy_data` 改为 `False`，则该行应打印 `True`。结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `State` 的 `data` 和 `hardware` 字段要用 `field(default_factory=...)`，而不能直接写成 `data = Data()`？

**参考答案**：因为 `Data()` / `Hardware()` 是可变对象。如果直接写成类属性默认值，所有 `State` 实例会共享同一个 `Data` 对象，一个实例改了 `data.calib`，另一个实例也跟着变。`default_factory` 保证每个实例新建时各自得到一个独立的空 `Data()` / `Hardware()`。

**练习 2**：oneshot 场景里通常没有 optimizer，那 `state.compression_ready` 会是什么值？这意味着什么？

**参考答案**：会是 `False`（因为 `optimizer is None`）。这说明 `compression_ready` 并不是「oneshot 能否压缩」的判据，它面向的是带优化器的训练式压缩；oneshot 走的是校准管线，不依赖这个标志。不要被它的名字误导。

### 4.3 全局会话函数与线程局部存储

#### 4.3.1 概念说明

到目前为止，`CompressionSession` 还只是一个普通类，要用它你得自己 `new` 一个、再一路传给 modifier 和管线。但 llm-compressor 选择了另一种风格：**全局默认会话**。

[session_functions.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) 提供了三个顶层函数：

- `active_session()`：返回「当前激活的会话」。几乎所有内部代码都通过它拿会话，而不是自己持有引用。
- `create_session()`：一个上下文管理器（`with create_session() as session:`），在上下文内临时把「激活会话」换成一个新的、干净的会话，退出后恢复原来的。
- `reset_session()`：把当前激活会话重置。

这样设计的好处是：modifier、pipeline、oneshot 这些分散在各处的代码，不需要彼此传递 session 引用，只要调 `active_session()` 就能访问同一份上下文。它还通过 `threading.local()` 做了线程隔离，避免多线程下互相踩到对方的会话。

此外，这个文件还提供了一个 `LifecycleCallbacks` 门面类（模块末尾赋值 `callbacks = LifecycleCallbacks`），把常见事件封装成了 `batch_start / calibration_start / sequential_epoch_end / calibration_end` 等classmethod，方便管线代码用语义化的方式触发事件。

#### 4.3.2 核心流程

线程局部存储的工作模型：

```
# 模块被 import 时（只执行一次）
_global_session = CompressionSession()          # 一个全局兜底会话
_local_storage = threading.local()
_local_storage.session = _global_session        # 默认指向全局会话

# 任意线程调用 active_session()
def active_session():
    return getattr(_local_storage, "session", _global_session)   # 总能拿到一个

# 进入 create_session() 上下文
orig = _local_storage.session                   # 记住旧的
_local_storage.session = CompressionSession()   # 换成全新会话
yield new_session                               # with 体内 active_session() 拿到的是新会话
finally:
    _local_storage.session = orig               # 退出时恢复旧会话
```

要点：
- `active_session()` **永远**返回一个会话——即使没人调用过 `create_session()`，它也返回模块加载时建好的 `_global_session`。所以 oneshot 里直接 `session = active_session()` 也总是安全的。
- `create_session()` 的「临时替换」只在 `with` 块内生效。一旦退出 `with`，激活会话又变回之前那一个。**这是初学者最容易踩的坑**：你以为出了 `with` 还在用新会话，其实已经回去了。
- `reset_session()` 等价于 `active_session()._lifecycle.reset()`，作用在「当前」激活会话上。

#### 4.3.3 源码精读

**模块级全局对象：**

[session_functions.py:L31-L33](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L31-L33) —— 全局会话与线程局部存储的初始化。

```python
_global_session = CompressionSession()
_local_storage = threading.local()
_local_storage.session = _global_session
```

**`create_session()` 是上下文管理器，用 try/finally 保证恢复：**

[session_functions.py:L36-L52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L36-L52) —— 注意 `finally` 里把 `session` 还原成 `orig_session`。

```python
@contextmanager
def create_session() -> Generator[CompressionSession, None, None]:
    global _local_storage
    orig_session = getattr(_local_storage, "session", None)
    new_session = CompressionSession()
    _local_storage.session = new_session
    try:
        yield new_session
    finally:
        _local_storage.session = orig_session
```

**`active_session()` 永远有兜底：**

[session_functions.py:L55-L60](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L55-L60) —— `getattr` 的第三参数是兜底，保证不抛错。

```python
def active_session() -> CompressionSession:
    global _local_storage
    return getattr(_local_storage, "session", _global_session)
```

**`reset_session()` 直接调用 lifecycle.reset：**

[session_functions.py:L63-L68](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L63-L68) —— 等价于 `session.reset()`，只是作用在当前激活会话上。

**`LifecycleCallbacks` 门面与 `callbacks` 别名：**

[session_functions.py:L71-L178](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L71-L178) —— 它把 `EventType` 翻译成语义化的方法名。例如 `calibration_start()` 内部就是 `cls.event(EventType.CALIBRATION_START, **kwargs)`。还有一个保护：`event()` 方法会拒绝 `INITIALIZE` / `FINALIZE`，提示这两个必须用 `session.initialize()` / `session.finalize()`，不能当成普通事件触发：

```python
@classmethod
def event(cls, event_type: EventType, **kwargs) -> ModifiedState:
    if event_type in [EventType.INITIALIZE, EventType.FINALIZE]:
        raise ValueError(f"Cannot invoke {event_type} event. Use the corresponding method instead.")
    return active_session().event(event_type, **kwargs)
```

**`core/__init__.py` 重新导出全部公开符号：**

[core/__init__.py:L9-L38](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/__init__.py#L9-L38) —— 它把 `CompressionSession / State / Data / Hardware / ModifiedState / create_session / active_session / reset_session / callbacks / LifecycleCallbacks` 等都聚合导出，所以我们可以直接 `from llmcompressor import create_session, active_session`。

> **阅读提示（承接 u1-l3）**：`core/__init__.py` 的 `__all__` 里列出了一个 `apply`，但该文件并没有 `import apply` 或定义它。因此 `from llmcompressor.core import apply` 实际会失败——这是历史遗留。再次印证那条原则：**判断一个符号能不能用，看 import 语句，而不是只看 `__all__`**。

#### 4.3.4 代码实践

**目标**：验证 `create_session()` 的「上下文内替换、退出后恢复」行为，以及 `active_session()` 的兜底语义。

**操作步骤**：

```python
# 示例代码
from llmcompressor import create_session, active_session

outer = active_session()
print("进入前 active_session id:", id(outer))

with create_session() as session:
    inner = active_session()
    print("with 内 active_session id:", id(inner))
    print("inner 就是 with 拿到的 session?", inner is session)   # True
    print("与外层是同一个会话?", inner is outer)                 # False

after = active_session()
print("退出后 active_session id:", id(after))
print("退出后恢复成外层会话?", after is outer)                  # True
```

**需要观察的现象**：`with` 体内 `active_session()` 拿到的是一个全新的会话（与 `session` 同一对象，与外层不同）；退出 `with` 后 `active_session()` 又变回进入前的那个会话。

**预期结果**：分别打印 `True`、`False`、`True`。如果你把 `with create_session()` 误写成 `session = create_session()`（不加 `with`），`session` 会是一个未启动的上下文管理器对象而不是 `CompressionSession`，`active_session()` 也不会改变——这是常见误用，请留意。结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么不写 `with create_session()`，直接 `active_session()` 也不会报错？

**参考答案**：因为模块在 import 时就创建了 `_global_session` 并把 `_local_storage.session` 指向它（[session_functions.py:L31-L33](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L31-L33)）。`active_session()` 用 `getattr(_local_storage, "session", _global_session)` 兜底，所以任何时候都至少能拿到这个全局会话。

**练习 2**：`LifecycleCallbacks.event(EventType.INITIALIZE)` 会发生什么？为什么要这样设计？

**参考答案**：会抛 `ValueError`（[session_functions.py:L85-L89](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L85-L89)）。因为 `INITIALIZE` / `FINALIZE` 不是普通事件，它们有专用的 `session.initialize()` / `session.finalize()`，且 lifecycle 内部对它们有额外的状态校验（例如「未初始化不能 finalize」「不能 finalize 两次」）。强制走专用方法可以避免这些校验被绕过。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿任务（即本讲的实践任务）。

**任务**：调用 `create_session` 建一个干净会话，用 `active_session()` 拿到它，打印其 `state` 的类型与默认字段，再 `initialize` 一次让生命周期「亮起来」，最后 `reset()`，对比 reset 前后 lifecycle 标志的变化。

**操作步骤**：

```python
# 示例代码
from llmcompressor import create_session, active_session

with create_session() as session:
    sess = active_session()
    assert sess is session, "active_session() 在 with 内应返回同一个 session"

    # 1) 打印 state 的类型与默认字段
    state = sess.state
    print("state 类型:", type(state).__name__)
    print("默认字段:")
    print("  model             =", state.model)
    print("  optimizer         =", state.optimizer)
    print("  current_batch_idx =", state.current_batch_idx)
    print("  enable_compile    =", state.enable_compile)
    print("  data              =", state.data)
    print("  hardware          =", state.hardware)

    # 2) initialize 让生命周期亮起来（不传 recipe 也行）
    sess.initialize()
    print("\n[reset 前]")
    print("  initialized_      =", sess.lifecycle.initialized_)
    print("  finalized         =", sess.lifecycle.finalized)
    print("  global_step       =", sess.lifecycle.global_step)

    # 3) reset 后对比
    sess.reset()
    print("\n[reset 后]")
    print("  initialized_      =", sess.lifecycle.initialized_)
    print("  finalized         =", sess.lifecycle.finalized)
    print("  global_step       =", sess.lifecycle.global_step)
    print("  recipe.modifiers  =", sess.lifecycle.recipe.modifiers)
```

**需要观察的现象**：
- `state` 是 `State` 实例，默认字段里 `model/optimizer` 为 `None`，`current_batch_idx` 为 `-1`，`data` 是空 `Data`，`hardware` 是空 `Hardware`。
- reset 前 `initialized_=True`；reset 后 `initialized_=False`、`global_step=0`、`recipe.modifiers=[]`。

**预期结果**：脚本无报错地跑完，并按上述现象打印。整个过程中没有用到 GPU，也没有真实模型，说明 session/state 这一层是与具体模型解耦的纯调度/容器层。结果待本地验证。

**延伸思考（可选）**：把上面的 `sess.initialize()` 换成带一个真实 recipe 的调用（例如 `sess.initialize(recipe=[QuantizationModifier(...)])`），再观察 `sess.lifecycle.recipe.modifiers` 在 reset 前后的数量变化。这会引出 u2-l2（lifecycle 的事件系统）和 u2-l3（modifier 基类）的内容。

## 6. 本讲小结

- `CompressionSession` 是一个很薄的会话容器，只持有 `_lifecycle`；`session.state` 实际上是 `session._lifecycle.state`，`state`/`recipe`/生命周期标志都住在 `CompressionLifecycle` 里。
- session 的 `initialize / event / finalize` 三个方法都是「转发给 lifecycle + 把结果包成 `ModifiedState`」；`reset()` 转发给 `lifecycle.reset()`，后者会收尾残留 modifier 并重置所有字段。
- `State` 是压缩现场的「共享黑板」，承载 `model/optimizer/loss/batch_data`，并通过 `Data`（四种数据集）和 `Hardware`（设备/分布式信息）分槽存放；`update()` 做防御式赋值并默认 deepcopy 数据。
- `ModifiedState` 是 session 对外返回的标准「结果包裹」，包含 model/optimizer/loss 与各 modifier 的返回数据。
- `active_session()` 通过线程局部存储始终返回一个会话（默认是 import 时建好的 `_global_session`）；`create_session()` 是上下文管理器，在 `with` 内临时替换激活会话、退出后恢复——这是最易踩的坑。
- `LifecycleCallbacks`（别名 `callbacks`）把 `EventType` 封装成语义化方法，并禁止把 `INITIALIZE/FINALIZE` 当普通事件触发；判断可导入符号请看 import，`core/__init__.py` 的 `__all__` 里的 `apply` 是无法导入的遗留项。

## 7. 下一步学习建议

本讲只读了 session 与 state 这层「容器」，但真正驱动 modifier 的是 `CompressionLifecycle`。下一讲 **u2-l2 CompressionLifecycle 与事件系统** 会深入：

- `lifecycle.initialize/finalize/event` 如何逐个调用 modifier 的对应方法；
- `EventType` 全集与事件顺序校验（`_validate_event_order`）；
- `global_step` 在生命周期中的作用。

之后 **u2-l3 Modifier 基类生命周期** 会从「被驱动」的一方看同一套机制：modifier 的 `initialize/update_event/finalize` 与各种 `on_*` 钩子。建议把本讲综合实践里 `sess.initialize()` 的调用，和 u2-l2 的 lifecycle 源码、u2-l3 的 modifier 源码对照着读，你会看到「一次 initialize 调用」是如何在三处代码之间传递的。
