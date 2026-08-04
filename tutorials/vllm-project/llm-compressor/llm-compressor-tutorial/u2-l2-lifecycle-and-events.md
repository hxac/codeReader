# CompressionLifecycle 与事件系统

> 本讲是「核心引擎」单元的第二篇，承接 [u2-l1 会话与状态](u2-l1-session-and-state.md)。
> 上一讲我们知道了「模型、校准数据、recipe 存在哪里、谁在推进」，本讲回答更关键的一个问题：
> **它们是怎么被推进的？** 答案就是 `CompressionLifecycle` 与它的事件系统。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `CompressionLifecycle` 的三个核心方法 `initialize` / `finalize` / `event` 各自做什么，以及它们如何逐个驱动 recipe 里每个 modifier 的对应方法。
- 列举 `EventType` 的全部取值，并区分「训练生命周期事件」「校准生命周期事件」「生命周期端点」三类。
- 解释事件顺序校验（event order validation）的规则：哪些事件参与校验、哪些不参与、`_last_event_type` 如何推进。
- 说清 `global_step` 在生命周期里的作用，以及为什么在 PTQ（训练后量化）校准场景里它通常是「沉默」的。
- 看懂 `LifecycleCallbacks` 这个语义化门面，并能指出每个事件由哪个 pipeline 在什么时机触发。

## 2. 前置知识

本讲需要你先建立两个直觉（若不熟悉可先看 [u2-l1](u2-l1-session-and-state.md)）：

1. **会话持有生命周期**。`CompressionSession` 是个很薄的容器，它内部持有一个 `CompressionLifecycle` 对象 `_lifecycle`；我们常说的 `session.state`、`session.lifecycle` 其实都是转发给这个 lifecycle。也就是说，**真正干活的是 lifecycle，session 只是门面**。

2. **modifier 是压缩动作的单元**。一个 recipe 由若干个 modifier 有序组成（如 `QuantizationModifier`、`GPTQModifier`）。lifecycle 的工作，本质就是「在合适的时机，把合适的事件，递给 recipe 里的每一个 modifier」。

如果你还没接触过 modifier 基类，本讲会顺带用到它的几个钩子（`on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end`），更完整的 modifier 生命周期留待 [u2-l3](u2-l3-modifier-base-lifecycle.md) 精读。本讲你只需知道：**每个事件最终会调用 modifier 上同名（语义对应）的方法**。

> 一个关键术语：**钩子（hook）**。钩子就是「框架在固定时机回调你的函数」。lifecycle 不直接改模型，它只负责「按时发事件」，至于事件来了做什么，由每个 modifier 自己的钩子决定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/core/events/event.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py) | 定义 `EventType` 枚举（事件词汇表）和 `Event` 数据类（一次事件的载体） |
| [src/llmcompressor/core/lifecycle.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py) | `CompressionLifecycle`：事件系统的「发动机」，实现 `initialize`/`finalize`/`event` 与顺序校验 |
| [src/llmcompressor/core/session_functions.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) | `LifecycleCallbacks`：把裸 `EventType` 封装成语义化方法（`calibration_start()` 等），是 pipeline 触发事件的入口 |
| [src/llmcompressor/core/session.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py) | `CompressionSession`：把 `initialize`/`finalize`/`event` 转发给 lifecycle，并把返回值包成 `ModifiedState` |
| [src/llmcompressor/modifiers/modifier.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py) | `Modifier` 基类的 `update_event`：事件的「分发器」，把一个事件翻译成具体钩子调用 |
| src/llmcompressor/pipelines/{basic,sequential,data_free,independent}/pipeline.py | 校准管线：**事件真正的发源地**，在固定时机调用 `LifecycleCallbacks.xxx()` |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，自底向上：

1. **4.1** 先认识事件的「词汇」——`EventType` 与 `Event`。
2. **4.2** 再看 lifecycle 的两端——`initialize` 与 `finalize`。
3. **4.3** 接着看 lifecycle 的中段——`event` 与顺序校验、`global_step`。
4. **4.4** 最后把视角拉到 pipeline，看「事件是谁、在什么时候触发的」。

### 4.1 EventType 与 Event：事件系统的词汇表

#### 4.1.1 概念说明

事件系统需要两样东西：

- **有哪些事件？** —— 用 `EventType` 枚举固定下来，就像一份「广播节目单」。任何人（pipeline、optimizer 回调）都只能从这份节目单里点播，不能凭空发明事件。
- **一次事件长什么样？** —— 用 `Event` 数据类承载，它除了「我是什么类型」，还带着 `global_step`、`steps_per_epoch` 等上下文，供 modifier 判断「我现在该不该动手」。

把这两者分开，是典型的「类型（what）与实例（one occurrence）分离」设计：`EventType.CALIBRATION_START` 是一个永恒存在的概念，而 `Event(type_=EventType.CALIBRATION_START)` 是「这一次校准开始」的具体 occurrence。

#### 4.1.2 核心流程

`EventType` 按语义分成四组：

```
生命周期端点：INITIALIZE / FINALIZE
训练(batch)生命周期：BATCH_START / LOSS_CALCULATED / OPTIM_PRE_STEP / OPTIM_POST_STEP / BATCH_END
校准生命周期：     CALIBRATION_START / SEQUENTIAL_EPOCH_END / CALIBRATION_END
```

一个极其重要的规则（后面 4.3 会反复用到）：**`INITIALIZE` 和 `FINALIZE` 虽然写在枚举里，但它们不能当普通事件触发**，必须通过 lifecycle 的 `initialize()` / `finalize()` 方法走。枚举里保留它们，只是为了让「整个生命周期的端点」也有一个统一的名字空间。

`Event` 的关键派生量是 `current_index`，它决定训练生命周期里 modifier 是否「到了该起作用的时刻」。当事件不是按 epoch 组织时：

\[
\text{current\_index} = \text{global\_step}
\]

当按 epoch 组织（`steps_per_epoch` 不为空）时：

\[
\text{epoch} = \left\lfloor \frac{\text{global\_step}}{\text{steps\_per\_epoch}} \right\rfloor,\qquad
\text{current\_index} = \frac{\text{global\_step}}{\text{steps\_per\_epoch}}
\]

modifier 的 `should_update(start, end, update)` 就是用 `current_index` 与 `start`/`end`/`update` 比较来决定「这一步要不要更新」。

#### 4.1.3 源码精读

`EventType` 是一个 `@unique` 枚举，保证每个名字只出现一次。四组取值与上面流程图一一对应：

[src/llmcompressor/core/events/event.py:L21-L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py#L21-L58) —— `EventType` 枚举的全部取值（注意 `SEQUENTIAL_EPOCH_END` 的文档注释点明它专供 sequential pipeline 使用）。

`Event` 是一个普通 dataclass，`type_` 默认为 `None`，其余字段都是训练生命周期的计数量：

[src/llmcompressor/core/events/event.py:L61-L91](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py#L61-L91) —— `Event` 数据类字段，`global_step`/`global_batch` 默认 0，`steps_per_epoch`/`batches_per_step` 默认 `None`。

`should_update` 是 modifier 训练调度的判定核心（满足 start、未超过 end、且命中 update 间隔才返回 True）：

[src/llmcompressor/core/events/event.py:L207-L236](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py#L207-L236) —— `should_update`：用 `current_index` 与 `start`/`end`/`update` 比较。

> 小提示：`current_index` 的取值逻辑见 [event.py:L164-L181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py#L164-L181)。本讲后面会看到，在 PTQ 校准场景里 modifier 通常不设 `start`/`end`，所以这套 index 计算基本「沉默」——它主要为训练式压缩服务。

#### 4.1.4 代码实践

**实践目标**：把 `EventType` 的全部取值打印出来，并按「端点 / 训练 / 校准」分类。

**操作步骤**（无需 GPU，纯 Python）：

```python
# 示例代码
from llmcompressor.core.events import EventType, Event

for et in EventType:
    print(et.name, "=", et.value)

# 构造一个具体的 occurrence
e = Event(type_=EventType.CALIBRATION_START)
print("type:", e.type_, "global_step:", e.global_step, "epoch_based:", e.epoch_based)
```

**预期结果**：会列出 10 个枚举值（`INITIALIZE`/`FINALIZE`/`BATCH_START`/`LOSS_CALCULATED`/`BATCH_END`/`CALIBRATION_START`/`SEQUENTIAL_EPOCH_END`/`CALIBRATION_END`/`OPTIM_PRE_STEP`/`OPTIM_POST_STEP`），且 `e.epoch_based` 为 `False`（因为没设 `steps_per_epoch`）。若你的版本枚举值与此不同，以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `INITIALIZE`/`FINALIZE` 在枚举里，却不能通过 `session.event(...)` 触发？
  - **答案**：它们是生命周期的端点，需要做「编译 recipe、初始化所有 modifier、置位 `initialized_`」等一次性的大动作，逻辑放在专门的 `initialize()`/`finalize()` 方法里。把它们挡在 `event()` 之外，是为了防止用户在压缩中途误触导致状态错乱（见 4.3 的源码校验）。

- **练习 2**：`Event.global_step` 和 `Event.current_index` 有什么关系？
  - **答案**：非 epoch 模式下 `current_index` 就等于 `global_step`；epoch 模式下 `current_index` 是 `global_step / steps_per_epoch` 的小数表示，用来支持「按 epoch 分段」的训练调度。

---

### 4.2 CompressionLifecycle 的 initialize 与 finalize

#### 4.2.1 概念说明

`CompressionLifecycle` 是一个 `@dataclass`，它持有压缩现场的两件核心物品：

- `state: State` —— 共享黑板（模型、数据、硬件等，见 u2-l1）。
- `recipe: Recipe` —— 当前要执行的 modifier 有序列表。

它对外暴露三个方法，对应一次压缩的「头—中—尾」：

- `initialize()`：**头**。编译 recipe、把 state 更新好、逐个调用每个 modifier 的 `initialize`。
- `event()`：**中**。每来一个事件，逐个调用每个 modifier 的 `update_event`。这是被调用最频繁的方法。
- `finalize()`：**尾**。逐个调用每个 modifier 的 `finalize`，收尾。

注意三个标志位：`initialized_`、`finalized`（lifecycle 层）以及每个 modifier 自己的 `initialized`/`finalized`。它们共同保证「初始化只能一次、终结只能一次、事件必须夹在两者之间」。

#### 4.2.2 核心流程

```
initialize(recipe, ...)
  ├─ state.update(**kwargs)            # 先把 model/data 等写进黑板
  ├─ 若已 initialized_ → 直接返回        # 幂等保护
  ├─ recipe = Recipe.create_instance()  # 编译 recipe（接受路径/字符串/Modifier实例）
  ├─ for mod in recipe.modifiers:
  │     mod.initialize(state, ...)      # 逐个初始化 modifier
  └─ initialized_ = True

event(event_type, global_step, ...)      # 见 4.3

finalize(...)
  ├─ 若未 initialized_ → 报错
  ├─ 若已 finalized → 报错               # 不能 finalize 两次
  ├─ for mod in recipe.modifiers:
  │     mod.finalize(state, ...)         # 逐个收尾
  └─ finalized = True
```

`reset()` 是兜底：把所有「已初始化但未终结」的 modifier 强制 finalize（出错只告警不抛），再 `__init__()` 把字段全部复位。

#### 4.2.3 源码精读

lifecycle 的字段定义，注意三个状态标志与事件顺序相关的私有字段：

[src/llmcompressor/core/lifecycle.py:L20-L52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L20-L52) —— `CompressionLifecycle` 字段：`state`/`recipe` 为业务字段，`initialized_`/`finalized` 为状态位，`_last_event_type`/`_event_order` 服务于顺序校验（4.3），`global_step` 是全局步数计数器。

`initialize` 的核心是「编译 recipe + 逐个初始化 modifier」：

[src/llmcompressor/core/lifecycle.py:L73-L115](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L73-L115) —— `initialize`：先 `state.update`，再幂等地编译 recipe 并调用每个 `mod.initialize(state)`，最后置 `initialized_=True`。

`finalize` 带「前后置校验」：

[src/llmcompressor/core/lifecycle.py:L117-L149](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L117-L149) —— `finalize`：未初始化报错、重复终结报错，否则逐个 `mod.finalize(state)` 并置 `finalized=True`。

`reset` 的「尽力收尾」语义（异常只 warning，不中断 reset）：

[src/llmcompressor/core/lifecycle.py:L54-L71](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L54-L71) —— `reset`：对已初始化且未终结的 modifier 尝试 finalize，失败只告警，最后 `__init__()` 复位。

> 顺带看清 session 的转发：[session.py:L74-L145](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L74-L145) 的 `CompressionSession.initialize` 把参数转给 `_lifecycle.initialize`，再把结果包成 `ModifiedState` 返回。`finalize`/`event` 同理（[session.py:L147-L189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L147-L189)）。所以「session 方法 → lifecycle 方法 → modifier 钩子」是一条清晰的三级链。

#### 4.2.4 代码实践

**实践目标**：在一个全新 session 上，用空 recipe 走一遍 `initialize → finalize`，观察状态位变化。

**操作步骤**（无需 GPU）：

```python
# 示例代码
from llmcompressor.core import active_session

session = active_session()
session.lifecycle.reset()           # 先复位，避免被历史调用污染
print("before init:", session.lifecycle.initialized_, session.lifecycle.finalized)

session.initialize()                # 空 recipe
print("after init :", session.lifecycle.initialized_, session.lifecycle.finalized)
print("modifiers  :", session.lifecycle.recipe.modifiers)   # 预期 []

session.finalize()
print("after final:", session.lifecycle.initialized_, session.lifecycle.finalized)
```

**预期结果**：

```
before init: False False
after init : True False
modifiers  : []
after final: True True
```

再试着调用 `session.finalize()` 第二次，应当抛出 `ValueError: Cannot finalize more than once`（待本地验证）。

#### 4.2.5 小练习与答案

- **练习 1**：如果不调用 `initialize()` 直接调用 `finalize()` 会怎样？
  - **答案**：`finalize` 第一条校验 `if not self.initialized_` 会抛 `ValueError("Cannot finalize before initializing")`。事件 `event()` 也有同样的前置校验。

- **练习 2**：`initialize()` 被调用两次安全吗？
  - **答案**：安全但无副作用。源码里 `if self.initialized_: return`，第二次调用直接返回，不会重复编译 recipe 或重复初始化 modifier（代码里还留了 `TODO: do not initialize twice`，说明这是有意为之的幂等保护）。

---

### 4.3 CompressionLifecycle.event 与事件顺序校验

#### 4.3.1 概念说明

`event()` 是 lifecycle 的「心脏」：pipeline 每发生一件事（开始校准、一层校准完、校准结束……），就调一次 `event()`，lifecycle 负责把这件事广播给 recipe 里的每一个 modifier。

但它不是无脑广播，而是带三层保护：

1. **状态保护**：未初始化或已终结时不允许触发事件。
2. **端点保护**：禁止把 `INITIALIZE`/`FINALIZE` 当事件触发。
3. **顺序保护**：训练生命周期事件（batch/loss/optim）必须按固定顺序出现。

第三点是本讲最精巧的设计，值得单独理解。

#### 4.3.2 核心流程

`event(event_type, global_step=0, **kwargs)` 的执行流程：

```
1. 校验：未 initialized_ → 报错
2. 校验：已 finalized   → 报错
3. 校验：event_type 是 INITIALIZE/FINALIZE → 报错（请用专门方法）
4. 校验：_validate_event_order(event_type) 不通过 → 报错
5. 特判：LOSS_CALCULATED 必须带 loss 参数
6. 若 global_step is not None：self.global_step = global_step
7. event = Event(type_=event_type)
8. for mod in recipe.modifiers:
       mod.update_event(state, event, **kwargs)   # 广播
```

**顺序校验规则**（`_validate_event_order`）：

- 只有出现在 `_event_order` 列表里的事件才参与校验。该列表是：

\[
[\text{BATCH\_START},\ \text{LOSS\_CALCULATED},\ \text{OPTIM\_PRE\_STEP},\ \text{OPTIM\_POST\_STEP},\ \text{BATCH\_END}]
\]

- 三个**校准事件**（`CALIBRATION_START`/`SEQUENTIAL_EPOCH_END`/`CALIBRATION_END`）**不在列表里**，因此永远通过校验，也**不会更新** `_last_event_type`。也就是说，校准事件和训练事件是「两条互不干扰的轨道」。
- 对训练事件，记上一次事件为 \(l\)、本次为 \(c\)，在 `_event_order` 中的下标为 \(\text{idx}(\cdot)\)，则：

\[
\text{valid} =
\begin{cases}
l \ne \text{BATCH\_START}, & c = \text{BATCH\_START} \quad\text{(不允许连续两个 BATCH\_START)}\\
\text{idx}(l) \le \text{idx}(c), & \text{otherwise}
\end{cases}
\]

- 校验通过才把 `_last_event_type` 推进为当前事件。

`_last_event_type` 的初值是 `BATCH_END`（对应「上一个 batch 刚结束，可以开始新 batch」的状态），这保证了 `initialize()` 之后第一个合法的训练事件是 `BATCH_START`。

#### 4.3.3 源码精读

`_event_order` 与 `_last_event_type` 的定义（注意初值是 `BATCH_END`）：

[src/llmcompressor/core/lifecycle.py:L39-L49](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L39-L49) —— 事件顺序基准列表与「上一次事件」游标。

`event()` 的三层校验 + 广播主体：

[src/llmcompressor/core/lifecycle.py:L151-L213](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L151-L213) —— `event`：状态/端点/顺序三层校验后，更新 `self.global_step`，构造 `Event` 并对每个 modifier 调 `update_event`。

> 关于 `global_step` 的一个**忠实观察**（容易踩坑）：第 6 步确实把传入的 `global_step` 存到了 `self.global_step`（lifecycle 的计数器，见 [lifecycle.py:L197-L199](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L197-L199)），但第 7 步构造的 `Event` 对象用的是 `Event(type_=event_type)`——**没有把 global_step 传进去**，所以分发给 modifier 的 `Event.global_step` 恒为默认值 0。这意味着：lifecycle 自己维护着一个运行计数 `self.global_step`，而发给 modifier 的 `Event` 携带的是它自己的 `global_step`/`current_index`（此处为 0），并被 `should_start`/`should_end`/`should_update` 用于训练生命周期门控。在 PTQ 校准场景里，校准类 modifier 通常不设 `start`/`end`/`update`，这套门控基本不生效，真正驱动它们的是 4.4 将讲的校准钩子。请以本地源码为准理解这一细节。

顺序校验的实现：

[src/llmcompressor/core/lifecycle.py:L215-L230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L215-L230) —— `_validate_event_order`：不在列表的事件直接放行且不更新游标；`BATCH_START` 特判不能紧跟自己；其余按下标 `idx(last) <= idx(curr)` 判定。

#### 4.3.4 代码实践

**实践目标**：故意乱序触发训练事件，观察顺序校验的报错；同时验证校准事件不受此约束。

**操作步骤**（无需 GPU）：

```python
# 示例代码
from llmcompressor.core import active_session, EventType

session = active_session()
session.lifecycle.reset()
session.initialize()   # 空 recipe；_last_event_type 初值为 BATCH_END

# (1) 故意乱序：initialize 之后直接点播 OPTIM_PRE_STEP
#     idx(BATCH_END)=4 > idx(OPTIM_PRE_STEP)=2 → 应当报错
try:
    session.event(EventType.OPTIM_PRE_STEP)
except ValueError as e:
    print("乱序报错:", e)

# (2) 校准事件不参与顺序校验，连续触发也合法
session.event(EventType.CALIBRATION_START)
session.event(EventType.SEQUENTIAL_EPOCH_END, modules=[])
session.event(EventType.CALIBRATION_END)
print("校准三连发: 通过")

# (3) 端点保护：不能把 INITIALIZE 当事件
try:
    session.event(EventType.INITIALIZE)
except ValueError as e:
    print("端点报错:", e)
```

**预期结果**（具体报错文案以本地为准，待本地验证）：

- 步骤 (1) 抛 `ValueError`，文案形如：`Lifecycle events must appear following order: [...]. Instead, EventType.BATCH_END was called before EventType.OPTIM_PRE_STEP`。
- 步骤 (2) 正常通过，打印「校准三连发: 通过」。
- 步骤 (3) 抛 `ValueError`，文案形如：`Cannot invoke EventType.INITIALIZE event. Use the corresponding method instead.`。

#### 4.3.5 小练习与答案

- **练习 1**：连续触发两次 `BATCH_START` 会怎样？为什么单独对它特判？
  - **答案**：第二次会报错。特判 `l != BATCH_START` 是因为一个 batch 必须先 `BATCH_START` 再（经 LOSS/OPTIM）走到 `BATCH_END` 才能开始下一个 batch；连续两个 `BATCH_START` 意味着上一个 batch 没正常关闭。

- **练习 2**：为什么 `CALIBRATION_START`、`SEQUENTIAL_EPOCH_END`、`CALIBRATION_END` 可以不按顺序校验？
  - **答案**：它们不在 `_event_order` 里，`_validate_event_order` 对不在列表的事件直接返回 `True` 且不更新游标。这是因为校准事件的「正确顺序」由 pipeline 本身的调用顺序保证（见 4.4），不需要 lifecycle 再做一次校验；同时也让校准轨道与训练轨道互不干扰。

- **练习 3**：`LOSS_CALCULATED` 事件有什么额外约束？
  - **答案**：`event()` 里有特判：调用时必须在 kwargs 里带非空的 `loss`，否则抛 `ValueError("Loss must be provided for loss calculated event")`（[lifecycle.py:L189-L193](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L189-L193)）。

---

### 4.4 事件由谁触发：LifecycleCallbacks 与 pipeline 的协作

#### 4.4.1 概念说明

到目前为止我们只说了「事件会被广播」，但**事件是谁产生的？** 答案是：**校准管线（pipeline）**。

为了让 pipeline 的代码好读，项目提供了 `LifecycleCallbacks`（别名 `callbacks`）这个语义化门面。它把裸 `EventType` 封装成一个个有名字的方法：

- `LifecycleCallbacks.calibration_start()` —— 内部调 `event(EventType.CALIBRATION_START)`
- `LifecycleCallbacks.sequential_epoch_end(modules)` —— 内部调 `event(EventType.SEQUENTIAL_EPOCH_END, modules=...)`
- `LifecycleCallbacks.calibration_end()` —— 内部调 `event(EventType.CALIBRATION_END)`
- 以及训练用的 `batch_start` / `loss_calculated` / `optim_pre_step` / `optim_post_step` / `batch_end`。

它的每个方法都转发到 `active_session().event(...)`。这样 pipeline 里只需写 `LifecycleCallbacks.calibration_start()`，而不必关心当前激活的是哪个 session、不必手写 `EventType.XXX`。

而事件最终如何变成对模型的具体操作？这要靠 modifier 基类的 `update_event`——它是事件的「分发器」，把一个 `EventType` 翻译成具体钩子调用。

#### 4.4.2 核心流程：事件的一生

```
pipeline 在某时机调用:
    LifecycleCallbacks.calibration_start()
        │  (转发)
        ▼
    active_session().event(EventType.CALIBRATION_START)        # session.py
        │  (转发)
        ▼
    CompressionLifecycle.event(EventType.CALIBRATION_START)    # lifecycle.py：校验+广播
        │  对每个 modifier:
        ▼
    Modifier.update_event(state, event)                        # modifier.py：分发器
        │  根据 event.type_ 调用对应钩子:
        ├─ on_event(event)                         # catch-all，每个事件都先调
        ├─ CALIBRATION_START     → on_calibration_start
        ├─ SEQUENTIAL_EPOCH_END  → on_sequential_epoch_end(modules)
        └─ CALIBRATION_END       → on_calibration_end
```

关键点：`update_event` 是一个「先 catch-all，再按类型分流」的分发器。每个事件都会先触发 `on_event`，再触发与类型对应的那个钩子（详见 [modifier.py:L114-L175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L114-L175)）。子类按需重写感兴趣的钩子即可。

#### 4.4.3 源码精读

`LifecycleCallbacks` 的语义化方法，注意它们都 `return active_session().event(...)`：

[src/llmcompressor/core/session_functions.py:L146-L175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L146-L175) —— `calibration_start`/`sequential_epoch_end`/`calibration_end` 三个校准回调。

端点保护在门面层也做了一遍（双重保险）：

[src/llmcompressor/core/session_functions.py:L76-L91](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L76-L91) —— `LifecycleCallbacks.event`：再次拒绝把 `INITIALIZE`/`FINALIZE` 当事件，然后转发。

modifier 端的分发器（事件 → 钩子的翻译表）：

[src/llmcompressor/modifiers/modifier.py:L114-L175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L114-L175) —— `Modifier.update_event`：先 `on_event`（catch-all），再按 `event.type_` 分流到校准钩子或训练钩子。

**事件真正的发源地——三个校准 pipeline**。下面这张表把「事件 ↔ 触发点 ↔ 时机」对齐（行号均为本讲 HEAD）：

| 事件 | 触发的 pipeline | 触发点（文件:行） | 时机 |
| --- | --- | --- | --- |
| `CALIBRATION_START` | Sequential / Basic / DataFree | sequential:L113 / basic:L57 / data_free:L37 | 校准 epoch 开始、前向之前 |
| `SEQUENTIAL_EPOCH_END` | Sequential（每子图一次）/ Basic / DataFree | sequential:L160 / basic:L75 / data_free:L38 | 一个子图/层用校准数据跑完一遍之后 |
| `CALIBRATION_END` | Sequential / Basic / DataFree | sequential:L181 / basic:L76 / data_free:L39 | 整个模型校准 epoch 结束、收尾前 |
| `BATCH_START`/`LOSS_CALCULATED`/`OPTIM_PRE_STEP`/`OPTIM_POST_STEP`/`BATCH_END` | 训练生命周期（optimizer 回调） | 见 `LifecycleCallbacks.batch_start` 等 | 训练式压缩的每一步；oneshot PTQ 不走这条路 |
| `INITIALIZE`/`FINALIZE` | 不能用 event 触发 | lifecycle:L173-L181 | 生命周期端点，须用 `initialize()`/`finalize()` |

对照源码看 sequential pipeline 的三处触发：

- [sequential/pipeline.py:L113](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L113) —— 校准开始前触发 `CALIBRATION_START`。
- [sequential/pipeline.py:L145-L160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L145-L160) —— 每个子图先跑一遍前向（触发校准 hook），随后在该子图结束时触发 `SEQUENTIAL_EPOCH_END(subgraph.submodules(model))`，把这一子图的模块列表传给 modifier。
- [sequential/pipeline.py:L181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L181) —— 全部子图处理完，触发 `CALIBRATION_END`。

对照 basic / data_free（更简单，没有逐子图循环，整模型一次走完）：

- [basic/pipeline.py:L57](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L57)、[basic/pipeline.py:L75-L76](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L75-L76) —— 依次 `calibration_start` → 跑完所有 batch → `sequential_epoch_end(全部模块)` → `calibration_end`。
- [data_free/pipeline.py:L37-L39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L37-L39) —— 无需数据，三个事件「背靠背」连发。

最后看 IndependentPipeline 的特殊之处——它**不自己触发事件**，而是把 recipe 里的 modifier 拆开，**为每个 modifier 单独推断并运行一条子管线**，由那条子管线（sequential/basic/datafree 之一）去触发事件：

[src/llmcompressor/pipelines/independent/pipeline.py:L36-L45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L36-L45) —— 逐个 modifier：临时把 recipe 的 modifiers 替换成单个 modifier，推断其专属 pipeline 并运行，从而「每个 modifier 拥有独立的校准 epoch」。

#### 4.4.4 代码实践

**实践目标**：写一个只打印日志的 `TraceModifier`，亲手用 `session.event(...)` 驱动它，把「事件 → 钩子」的分发过程可视化。

**操作步骤**（无需 GPU）：

```python
# 示例代码
from llmcompressor.modifiers import Modifier
from llmcompressor.core import active_session, EventType

class TraceModifier(Modifier):
    def on_initialize(self, state, **kwargs) -> bool:
        print("  -> on_initialize")
        return True

    def on_event(self, state, event, **kwargs):
        print(f"  -> on_event (type={event.type_})")

    def on_calibration_start(self, state, event, **kwargs):
        print("  -> on_calibration_start")

    def on_sequential_epoch_end(self, state, event, modules, **kwargs):
        print(f"  -> on_sequential_epoch_end (modules={len(modules)})")

    def on_calibration_end(self, state, event, **kwargs):
        print("  -> on_calibration_end")

session = active_session()
session.lifecycle.reset()
session.initialize(recipe=[TraceModifier()])

print("[dispatch CALIBRATION_START]")
session.event(EventType.CALIBRATION_START)
print("[dispatch SEQUENTIAL_EPOCH_END]")
session.event(EventType.SEQUENTIAL_EPOCH_END, modules=[1, 2, 3])
print("[dispatch CALIBRATION_END]")
session.event(EventType.CALIBRATION_END)
session.finalize()
```

**预期结果**（待本地验证）：

```
  -> on_initialize
[dispatch CALIBRATION_START]
  -> on_event (type=EventType.CALIBRATION_START)
  -> on_calibration_start
[dispatch SEQUENTIAL_EPOCH_END]
  -> on_event (type=EventType.SEQUENTIAL_EPOCH_END)
  -> on_sequential_epoch_end (modules=3)
[dispatch CALIBRATION_END]
  -> on_event (type=EventType.CALIBRATION_END)
  -> on_calibration_end
```

注意每次事件都先打印 `on_event`（catch-all），再打印对应钩子——这正是 `update_event` 「先 catch-all、再分流」的体现。

#### 4.4.5 小练习与答案

- **练习 1**：为什么不直接在 pipeline 里写 `active_session().event(EventType.CALIBRATION_START)`，而要包一层 `LifecycleCallbacks`？
  - **答案**：语义化和解耦。`LifecycleCallbacks.calibration_start()` 读起来就是「校准开始」，可读性远好于裸枚举；同时门面层还做了一次端点保护，并隐藏了「当前激活 session 是哪个」的细节，让 pipeline 代码聚焦于校准流程本身。

- **练习 2**：IndependentPipeline 自己触发事件吗？
  - **答案**：不直接触发。它把每个 modifier 单独拎出来运行一条推断出的子管线（sequential/basic/datafree），事件由那条子管线触发。这样每个 modifier 都有「属于自己的一整个校准 epoch」，互不抢数据。

- **练习 3**：在 sequential pipeline 里，`SEQUENTIAL_EPOCH_END` 为什么要把 `subgraph.submodules(model)` 当参数传进去？
  - **答案**：因为像 GPTQ 这类算法需要在「一个子图（一组层）校准完」时，拿到这组层去执行按块的权重量化（见 `on_sequential_epoch_end(modules)` 的签名与文档）。事件不只是「通知」，还顺带把上下文（哪些模块）递给 modifier。

---

## 5. 综合实践

把本讲四个模块串起来：**用 TraceModifier 模拟一次「sequential 风格」的事件序列，并验证顺序校验只在训练轨道生效**。

**任务**：

1. 复用 4.4.4 的 `TraceModifier`，新建一个干净 session。
2. 按真实 sequential pipeline 的顺序手动触发事件：

   ```python
   # 示例代码（接上）
   from llmcompressor.core import active_session, EventType
   session = active_session(); session.lifecycle.reset()
   session.initialize(recipe=[TraceModifier()])

   session.event(EventType.CALIBRATION_START)                  # 校准开始
   for sub in ["layer0", "layer1"]:                            # 模拟两个子图
       session.event(EventType.SEQUENTIAL_EPOCH_END, modules=[sub])
   session.event(EventType.CALIBRATION_END)                    # 校准结束
   session.finalize()
   ```

3. 在上面的序列里**插入**一次训练事件（例如在两个 `SEQUENTIAL_EPOCH_END` 之间调用 `session.event(EventType.BATCH_START)`），观察：训练事件和校准事件是否互相干扰？（预期：不干扰，因为校准事件不更新 `_last_event_type`，训练事件按自己轨道校验。）
4. 再故意制造一次**训练事件乱序**（如 `initialize()` 后立刻 `session.event(EventType.OPTIM_POST_STEP)`），确认会抛顺序错误。
5. 把你的观察整理成一页笔记：画一张「事件时间线」，标出哪些事件由 lifecycle 校验顺序、哪些不校验。

**预期结果**：步骤 2 顺利跑完，每个事件都触发 `on_event` + 对应钩子；步骤 3 中插入的 `BATCH_START` 不影响校准事件，但若再乱序触发训练事件就会被拦下；步骤 4 抛 `ValueError`。若实际行为与此不符，以本地源码与输出为准（待本地验证）。

> 这个练习帮你建立本讲最核心的脑图：**lifecycle 是发动机，事件是燃料，pipeline 是司机，modifier 是干活的人**。

## 6. 本讲小结

- `CompressionLifecycle` 是事件系统的发动机，`initialize` / `event` / `finalize` 对应「头—中—尾」，每个方法都把工作广播给 recipe 里的每一个 modifier。
- `EventType` 把全部事件固定为一份「节目单」，分四组：端点、训练（batch/loss/optim）、校准（start/sequential_epoch_end/end）。`INITIALIZE`/`FINALIZE` 不能当事件触发。
- `event()` 带三层保护：状态保护（必须夹在 initialize/finalize 之间）、端点保护、顺序保护；其中顺序保护**只约束训练事件**，校准事件走独立轨道、不更新 `_last_event_type`。
- 顺序校验的核心是 `_event_order` 列表与 `_last_event_type` 游标：`BATCH_START` 不能连续，其余满足 `idx(last) <= idx(curr)`。
- `global_step` 在 lifecycle 上是运行计数器，但分发给 modifier 的 `Event` 对象此处未携带它（恒为默认 0），训练门控主要服务于训练式压缩；PTQ 校准靠校准钩子驱动。
- `LifecycleCallbacks` 是语义化门面，pipeline 是事件真正的发源地：sequential/basic/datafree 各自在固定时机触发 `CALIBRATION_START`/`SEQUENTIAL_EPOCH_END`/`CALIBRATION_END`，independent 则把每个 modifier 委派给一条子管线。

## 7. 下一步学习建议

- **下一步精读 modifier 基类**：本讲只用到 `update_event` 的分发，建议接着读 [u2-l3 Modifier 基类生命周期](u2-l3-modifier-base-lifecycle.md)，把 `on_start`/`on_update`/`on_end` 训练钩子与 `should_start`/`should_end` 的门控彻底搞懂。
- **看真实算法如何挂钩子**：读完 u2-l3 后，可跳到 [u4-l1 GPTQ](u4-l1-gptq-algorithm.md) 或 [u3-l1 QuantizationModifier](u3-l1-quantization-modifier-and-scheme.md)，看 `on_calibration_start`/`on_sequential_epoch_end` 在真实算法里到底做了什么（挂 observer、累积 Hessian、量化权重）。
- **回到 pipeline**：想深入「事件是谁触发的」可读 [u3-l5 SequentialPipeline 逐层校准深析](u3-l5-sequential-pipeline.md)，看子图切分与两遍前向如何与 `SEQUENTIAL_EPOCH_END` 配合。
