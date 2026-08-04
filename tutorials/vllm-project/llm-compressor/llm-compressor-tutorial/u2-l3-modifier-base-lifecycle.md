# Modifier 基类生命周期

## 1. 本讲目标

本讲是进入「压缩算法实现」之前最关键的一讲。前面两讲(u2-l1、u2-l2)我们读完了「会话容器」`CompressionSession` 与「事件发动机」`CompressionLifecycle`,知道了**谁**在推进压缩流程。本讲要回答的是:**每一个具体的压缩动作(modifier)在被生命周期驱动时,内部到底经历了什么**。

`Modifier` 是全库所有压缩算法(量化、剪枝、GPTQ、AWQ……)的抽象基类。读完本讲你应该:

1. 掌握 `Modifier` 基类的**双生命周期**——校准生命周期(`on_calibration_start` → `on_sequential_epoch_end` → `on_calibration_end`)与训练生命周期(`on_start` → `on_update` → `on_end`)。
2. 理解四个带下划线的状态标志 `initialized_` / `finalized_` / `started_` / `ended_` 以及 `requires_calibration_data` 的作用。
3. 理解 `Modifier` 只要求子类**必须实现 `on_initialize`**、其余钩子可选实现的契约。
4. 学会手动驱动一个 modifier 走完 `initialize → update_event → finalize`,观察钩子触发顺序。

> 本讲只读「基类骨架」。具体算法(例如 GPTQ 如何在 `on_sequential_epoch_end` 里用量化 Hessian)留给第四单元;基类如何把事件分发到对应钩子,是本讲的全部重点。

## 2. 前置知识

### 2.1 模板方法模式(Template Method)

`Modifier` 基类用的是经典的**模板方法模式**:基类定义好「骨架流程」(什么时候先做什么、再做什么),并把「具体怎么做」开放成一个个可重写的**钩子(hook)**方法交给子类。子类不需要关心「我现在该被调用了没有」,只需要在合适的钩子里填入自己的逻辑。

在本库里:

- **公开方法(public method)**:`initialize` / `update_event` / `finalize`。这些**由生命周期(`CompressionLifecycle`)调用**,带着状态校验和分发逻辑,子类一般不要重写。
- **钩子(hook)**:`on_initialize` / `on_event` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` / `on_start` / `on_update` / `on_end` / `on_finalize`。这些**给子类重写**,基类在公开方法里按事件类型决定调用哪个钩子。

### 2.2 事件回顾

承接 u2-l2,生命周期通过 `Event` 携带 `EventType` 来通知 modifier「现在发生了什么」。本讲会频繁用到以下事件:

| EventType | 含义 | 触发方 |
|---|---|---|
| `CALIBRATION_START` | 一次校准开始 | basic / sequential pipeline |
| `SEQUENTIAL_EPOCH_END` | 一个子图(逐层)校准完成 | sequential pipeline |
| `CALIBRATION_END` | 整个模型校准结束 | basic / sequential pipeline |
| `BATCH_START` / `BATCH_END` | 训练 batch 的起止 | 训练式管线 |

### 2.3 pydantic 与「下划线属性」小知识

`Modifier` 是一个 **pydantic v2 模型**(它继承 `HooksMixin`,后者继承 `pydantic.BaseModel`,见本讲 4.2)。在 pydantic v2 里,**以下划线 `_` 开头的属性不会被当作模型字段**,而是普通实例属性。这解释了为什么状态标志都写成 `initialized_`(带下划线):它们是「可变运行时状态」,不该参与 pydantic 的校验与序列化;而 `index` / `start` / `end` / `update` 这些不带下划线、出现在 recipe 配置里的参数,才是被 pydantic 校验的**真正字段**。

> 记住这个规则,后面读源码时,看到 `_hooks`、`initialized_`、`_HOOKS_DISABLED` 这类名字,你就知道它们是「绕过 pydantic 的私有状态」,而不是配置项。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [modifier.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py) | `Modifier` 抽象基类。定义全部公开方法与钩子,是本讲的绝对主角。 |
| [interface.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/interface.py) | `ModifierInterface`,纯抽象接口(ABC),规定所有 modifier 必须实现哪些公开方法。 |
| [hooks.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py) | `HooksMixin`,modifier 注册/禁用/清理 PyTorch forward hook 的工具混入。 |

此外会少量引用上一讲读过的:

- [lifecycle.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py) —— 生命周期如何调用 `mod.update_event`。
- [session_functions.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) —— `LifecycleCallbacks` 如何把 `modules` 透传到钩子。

## 4. 核心概念与源码讲解

### 4.1 ModifierInterface:所有 modifier 的接口契约

#### 4.1.1 概念说明

`ModifierInterface` 是一个纯抽象基类(继承 `abc.ABC`),它**不包含任何实现**,只声明「一个 modifier 必须对外提供哪些公开方法」。它的存在意义是:给生命周期(`CompressionLifecycle`)一个**类型契约**——只要手里拿的是 `ModifierInterface`,就一定能对它调用 `initialize` / `finalize` / `update_event`,也能用 `initialized` / `finalized` 属性查询状态。

它定义了三个抽象方法 + 两个抽象 property:

| 成员 | 性质 | 说明 |
|---|---|---|
| `initialized` | 抽象 property | 是否已初始化 |
| `finalized` | 抽象 property | 是否已收尾 |
| `initialize(state, **kwargs)` | 抽象方法 | 初始化 modifier |
| `finalize(state, **kwargs)` | 抽象方法 | 收尾 modifier |
| `update_event(state, event, **kwargs)` | 抽象方法 | 根据事件更新 modifier |

#### 4.1.2 核心流程

接口本身没有流程,但它**约束了流程**:生命周期里那段遍历 recipe 的代码,正是依赖这个契约。回顾 u2-l2,`CompressionLifecycle.initialize` 会逐个调用 `mod.initialize(state=...)`,`CompressionLifecycle.event` 会逐个调用 `mod.update_event(state=..., event=..., **kwargs)`。

#### 4.1.3 源码精读

接口的定义非常短,全部是抽象声明:

[modifier.py:9-62(interface.py)](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/interface.py#L9-L62) —— `ModifierInterface` 声明了 `initialized` / `finalized` 两个抽象 property,以及 `initialize` / `finalize` / `update_event` 三个抽象方法,方法体只是 `raise NotImplementedError()`。

注意:`on_initialize` 这类**钩子并不出现在接口里**。接口只规定「公开方法」,钩子是 `Modifier` 基类自己引入的实现细节。所以一个 modifier 对外承诺的只有这五个;其余钩子是给算法作者用的扩展点。

#### 4.1.4 代码实践

打开接口文件,数一数 `@abstractmethod` 装饰器的个数(应为 5 个:2 个 property + 3 个方法)。然后在生命周期文件里搜索 `mod.update_event`,确认生命周期正是通过这个接口方法驱动所有 modifier 的。这是一个「源码阅读型」小练习,帮助你建立「接口契约 → 生命周期调用」的对应关系。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `on_calibration_start` 不在 `ModifierInterface` 里,而 `update_event` 在?

**参考答案**:`update_event` 是**对外公开的入口**,生命周期必须能对任意 modifier 调用它,所以放进接口契约;`on_calibration_start` 是 `Modifier` 基类内部的**分发钩子**,由 `update_event` 根据 `event.type_` 决定是否调用,属于实现细节,无需对外承诺,因此不进接口。

---

### 4.2 Modifier 基类:继承结构、类属性与状态标志

#### 4.2.1 概念说明

`Modifier` 是所有具体算法 modifier 的父类,它的类声明是:

```python
class Modifier(ModifierInterface, HooksMixin):
```

也就是说,一个 `Modifier` 同时拥有三重身份:

1. **`ModifierInterface`**:满足生命周期所需的公开契约(`initialize` / `finalize` / `update_event` / `initialized` / `finalized`)。
2. **`HooksMixin`**:具备注册和管理 PyTorch forward hook 的能力(校准时收集激活统计靠它,见 4.6)。
3. **`Modifier` 自身**:定义了双生命周期骨架、状态标志与全部钩子。

`Modifier` 最重要的设计是**双生命周期**:同一个基类既支持「校准式」压缩(PTQ,一次校准完成,如 GPTQ/AWQ),也支持「训练式」压缩(带 optimizer 的微调,如部分剪枝)。两条生命周期的钩子互不冲突,由 `update_event` 根据事件类型分流。

#### 4.2.2 核心流程

一个 modifier 从被构造到被销毁,粗略经历:

```text
构造(__init__)  →  initialize(state)  →  [多次] update_event(state, event)  →  finalize(state)
```

其中 `update_event` 每收到一个事件,就按事件类型分派到「校准钩子链」或「训练钩子链」。四类状态标志记录当前走到哪一步:

- `initialized_`:已调用 `on_initialize` 且返回 True。
- `finalized_`:已调用 `on_finalize` 且返回 True。
- `started_`:校准链里随 `CALIBRATION_START` 置位;训练链里随首个满足条件的 `BATCH_START` 置位。
- `ended_`:校准链里随 `CALIBRATION_END` 置位;训练链里随满足条件的 `BATCH_END` 置位。

还有一个影响**管线选择**的类属性 `requires_calibration_data`,它在 u1-l4 提到过:管线注册器用它判断该 modifier 是否需要校准数据,从而决定走 sequential 还是 datafree 管线。

#### 4.2.3 源码精读

类声明与一组类属性,集中体现了上面三重身份与状态标志:

[modifier.py:14-55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L14-L55) —— `Modifier(ModifierInterface, HooksMixin)` 的三重继承;`model_config = ConfigDict(extra="forbid")` 表示**构造时禁止传入未声明的字段**(防拼错);`requires_calibration_data = False` 是影响管线选择的开关;`index/group/start/end/update` 是 recipe 可配置的字段;而 `initialized_/finalized_/started_/ended_` 都带下划线,是 pydantic 私有运行时状态。

注意类文档里直接写明了生命周期骨架:

[modifier.py:20-28](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L20-L28) —— 文档列出 `1. initialize` → `2. on_event →(分派到各 on_* 钩子)` → `5. finalize` 的骨架,并提示「需要校准数据的子类必须把 `requires_calibration_data` 改成 True」。

> 关键提示:`extra="forbid"` 意味着如果你在 recipe 里给一个 modifier 传了它没有的字段,pydantic 会直接报错。这对调试「字段名拼错」非常友好。

#### 4.2.4 代码实践

在 Python 里 `from llmcompressor.modifiers import Modifier`,然后直接尝试两个「反例」,体会 `extra="forbid"` 与下划线属性的差异:

1. `Modifier()` 能否构造?(能,因为 `on_initialize` 是抽象方法,但 pydantic 不拦截抽象方法,只有实例化并调用时才会因未实现而报错——实际你应继承后再用。)
2. 若给某子类传入一个不存在的字段如 `typo_field=1`,会得到 pydantic 的 `ValidationError`。

> 这一步是「待本地验证」:不同 pydantic 版本错误信息略有差异,但「拒绝未知字段」的行为稳定。

#### 4.2.5 小练习与答案

**练习 1**:`Modifier` 为什么同时继承 `ModifierInterface` 和 `HooksMixin`,而不是把 hook 能力直接写进 `Modifier`?

**参考答案**:分离关注点。`ModifierInterface` 只管「生命周期契约」,`HooksMixin` 只管「PyTorch hook 的注册/禁用/清理」。把 hook 能力做成独立 mixin,既让 `Modifier` 代码聚焦于生命周期分发,也允许将来别的、不需要完整生命周期的类复用 hook 管理。

**练习 2**:`start` 和 `started_` 看起来很像,它们有什么本质区别?

**参考答案**:`start` 是**配置字段**(不带下划线,被 pydantic 校验),表示「训练式压缩从第几步开始」,默认 `None`;`started_` 是**运行时状态标志**(带下划线),表示「这个 modifier 是否已经触发了 `on_start`」,默认 `False`。前者是输入,后者是结果。

---

### 4.3 三大模板方法:initialize / update_event / finalize 与状态校验

#### 4.3.1 概念说明

这三个公开方法是「模板方法模式」里的**骨架方法**:它们负责状态校验、调用钩子、维护状态标志,而把真正的算法逻辑下放到 `on_*` 钩子。算法作者(写 GPTQModifier 的人)几乎只写钩子,不碰这三个方法。我们逐个看它们做了什么。

#### 4.3.2 核心流程

**`initialize(state)`**:

```text
1. 校验:未初始化、未收尾,否则报错
2. self.initialized_ = self.on_initialize(state)      # 子类必须实现
3. 构造一个 fake BATCH_START(global_step=0) 事件
4. 若 should_start(fake_event) 为真 → 调用 on_start 并置 started_=True
```

第 3、4 步是一个容易被忽略的细节:**对于 `start` 配置为 0(或为空但有特殊需求)的训练式 modifier,初始化时就立刻 `on_start`**,不必等到第一个真实 batch 事件。

**`finalize(state)`**:

```text
1. 校验:不能收尾两次、必须已初始化
2. self.finalized_ = self.on_finalize(state)          # 默认返回 True
```

**`update_event(state, event)`**(本讲最核心):它先跑一个 **catch-all 钩子** `on_event`,再按事件类型分流到校准链或训练链,详见 4.4 与 4.5。

三个方法都用「前置状态校验」保护自己,例如不能初始化两次、不能更新未初始化的 modifier。这种防御式编程保证了即使管线调用顺序出错,也能尽早报错而非悄悄产生错误结果。

#### 4.3.3 源码精读

`initialize` 的实现,含状态校验与「初始化即触发 start」的小机关:

[modifier.py:71-95](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L71-L95) —— 先拒绝「已初始化/已收尾」的情况,再调用 `on_initialize` 并把返回值赋给 `initialized_`;随后构造 `fake_start_event = Event(type_=EventType.BATCH_START, global_step=0)`,若 `should_start` 为真则立即 `on_start`。

`finalize` 的实现,简洁但带严格校验:

[modifier.py:97-112](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L97-L112) —— 拒绝重复收尾、拒绝收尾未初始化的 modifier,然后调用 `on_finalize`(默认实现见后)并把返回值赋给 `finalized_`。

`update_event` 的前半段(catch-all 与状态校验),是分流逻辑的入口:

[modifier.py:114-134](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L114-L134) —— 先校验「必须已初始化、未收尾」,然后**无条件调用 `on_event`**(catch-all 钩子,无论什么事件都会触发一次)。这就是 u2-l2 提到的「先 catch-all 的 `on_event`,再按类型分流」。

#### 4.3.4 代码实践

写一个只重写 `on_initialize` 的空壳 modifier(完整脚本见 4.7 综合实践),调用 `initialize(state)` 两次,观察第二次抛出的 `RuntimeError: Cannot initialize a modifier that has already been initialized`。这能帮你记住「基类的状态校验是强制的」。

#### 4.3.5 小练习与答案

**练习 1**:`initialize` 里为什么要构造一个 `fake_start_event` 去触发 `on_start`?

**参考答案**:某些训练式 modifier 的 `start` 配置为 0,意味着「压缩从第 0 步开始」。为了让这类 modifier 在校准/训练真正开始前就完成 `on_start`(例如挂载 hook、记录初始状态),基类在 `initialize` 末尾用一个 `global_step=0` 的假事件主动检查并触发一次 `on_start`,避免管线还得额外处理「第 0 步启动」这一边界情况。

**练习 2**:`on_finalize` 在基类里的默认返回值是什么?这意味着什么?

**参考答案**:默认 `return True`(见 4.5.3 的源码引用)。意味着「子类若不重写 `on_finalize`,`finalize` 仍会成功并把 `finalized_` 置为 `True`」。所以收尾对绝大多数 modifier 是可选的。

---

### 4.4 校准生命周期:三个钩子与 modules 上下文

#### 4.4.1 概念说明

校准生命周期是 PTQ(训练后量化)的主力。一次校准通常这样推进:

```text
CALIBRATION_START            → on_calibration_start   (挂校准 hook、启用量化)
  对每个子图(sequential):
    前向若干 batch 收集统计
    SEQUENTIAL_EPOCH_END     → on_sequential_epoch_end (用统计更新本子图权重/Hessian)
CALIBRATION_END              → on_calibration_end      (移除 hook、冻结量化参数)
```

这三个钩子的语义是:**开始**做准备工作(挂 hook),**逐层**做真正的压缩计算,**结束**做收尾(卸 hook、固定参数)。其中 `on_sequential_epoch_end` 比较特别——它会**收到一个 `modules` 参数**,即本次处理的子图里去重后的模块列表,这样算法(如 GPTQ)就能知道「这一轮要量化哪些 Linear」。

#### 4.4.2 核心流程

`update_event` 收到校准事件时的分流逻辑非常清晰,且每条分支都以 `return` 结束(互斥):

```text
if event.type_ == CALIBRATION_START:
    on_calibration_start(...);  started_ = True;  return
if event.type_ == SEQUENTIAL_EPOCH_END:
    on_sequential_epoch_end(state, event, modules, **kwargs);  return
if event.type_ == CALIBRATION_END:
    on_calibration_end(...);  ended_ = True;  return
```

注意:`on_sequential_epoch_end` 的签名里 `modules` 是一个**显式位置参数**(不是 `**kwargs`),因此调用方必须传 `modules`。这一点在手动驱动时要特别小心(见实践)。

`modules` 是怎么传过来的?链路是:

```text
sequential pipeline
  → LifecycleCallbacks.sequential_epoch_end(modules)        # 带 modules
  → session.event(SEQUENTIAL_EPOCH_END, modules=modules)
  → lifecycle.event(...) 构建 event 后调用
  → mod.update_event(state, event, modules=modules)         # modules 走 **kwargs
  → modifier.update_event 把 modules 透传给 on_sequential_epoch_end
```

#### 4.4.3 源码精读

`update_event` 中校准链的三条分支:

[modifier.py:136-150](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L136-L150) —— 三条 `if` 分支分别处理 `CALIBRATION_START` / `SEQUENTIAL_EPOCH_END` / `CALIBRATION_END`,前者和后者还会顺手维护 `started_` / `ended_` 标志。

三个校准钩子的(默认空)实现与签名,其中 `on_sequential_epoch_end` 带 `modules` 参数:

[modifier.py:271-306](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L271-L306) —— `on_calibration_start` / `on_sequential_epoch_end(state, event, modules, **kwargs)` / `on_calibration_end`,默认实现都是 `pass`,留给子类按需重写。

`modules` 参数的来源——回调与生命周期透传:

[session_functions.py:157-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L157-L165) —— `LifecycleCallbacks.sequential_epoch_end(modules)` 把 `modules` 作为 kwarg 传给 `cls.event`,最终流到 `update_event` 的 `**kwargs`,再透传给钩子。

[lifecycle.py:201-204](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/lifecycle.py#L201-L204) —— `event = Event(type_=event_type)` 之后 `mod.update_event(state=self.state, event=event, **kwargs)`,这里 `**kwargs` 就含 `modules`,是「钩子能拿到子图模块」的关键一环。

sequential pipeline 在子图前向后触发该事件:

[sequential/pipeline.py:160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L160) —— `LifecycleCallbacks.sequential_epoch_end(subgraph.submodules(model))`,即「对一个子图跑完校准前向后,把它的模块列表上报」。

#### 4.4.4 代码实践

手动驱动一个空壳 modifier 走完整校准链(完整脚本见 4.7)。要点:触发 `SEQUENTIAL_EPOCH_END` 时**必须传 `modules=[]`**,否则钩子因缺少必填参数而报 `TypeError`。你能依次看到 `on_initialize → on_calibration_start → on_sequential_epoch_end → on_calibration_end → on_finalize` 的打印。

#### 4.4.5 小练习与答案

**练习 1**:`on_calibration_start` 和 `on_calibration_end` 通常是成对出现的,它们各自适合做什么?

**参考答案**:`on_calibration_start` 适合做「挂载校准 hook、开启量化、重置统计」等准备工作;`on_calibration_end` 适合做「移除 hook、根据统计计算最终 scale/zero_point 并冻结、清理临时状态」等收尾工作。两者通过 `HooksMixin`(4.6)的 `register_hook` / `remove_hooks` 配合。

**练习 2**:为什么 `on_sequential_epoch_end` 要单独把 `modules` 作为显式参数,而 `on_calibration_start` 没有?

**参考答案**:因为 `on_sequential_epoch_end` 处理的是「当前这一个子图」,必须知道「这一轮具体涉及哪些模块」才能做逐层计算;而 `on_calibration_start` / `on_calibration_end` 是「整轮校准」的起止,作用范围是整个模型(可从 `state.model` 取),不需要额外的模块列表。

---

### 4.5 训练生命周期:on_start / on_update / on_end 与 start/end 门控

#### 4.5.1 概念说明

训练生命周期服务于「带 optimizer 微调」式的压缩(例如某些剪枝/蒸馏)。它通过 `start` / `end` / `update` 三个配置字段,把压缩限制在一个**步数区间**内:

- `start`:从第几步开始压缩。
- `end`:到第几步结束压缩。
- `update`:每隔几步更新一次(步长)。

基类用 `should_start(event)` / `should_end(event)` 两个门控函数,结合当前事件的 `current_index`,判断「现在该不该进入压缩、该不该退出压缩」。在区间内,每个 `BATCH_*` 事件都会触发一次 `on_update`。

#### 4.5.2 核心流程

`update_event` 中训练链的逻辑(在确认不是校准事件后执行):

```text
if BATCH_START 且 未started 且 should_start:
    on_start;  started_ = True;  on_update;  return          # 进入压缩
if BATCH_END 且 未ended 且 should_end:
    on_end;    ended_ = True;    on_update;  return          # 退出压缩
if started 且 未ended:
    on_update                                                # 区间内持续更新
```

门控函数 `should_start` 用半开区间判定:

\\[ \\text{should\\_start}(e) = (\\text{start} \\neq \\text{None}) \\land (\\text{start} \\le e.\\text{current\\_index}) \\land (\\text{end} = \\text{None} \\lor e.\\text{current\\_index} < \\text{end}) \\]

即「已设置 start、且当前步落在 \\([\\text{start}, \\text{end})\\) 内」。`should_end` 则简单得多:

\\[ \\text{should\\_end}(e) = (\\text{end} \\neq \\text{None}) \\land (e.\\text{current\\_index} \\ge \\text{end}) \\]

> 提醒:`current_index` 在「非 epoch 模式」(未设 `steps_per_epoch`)时就是 `global_step`(整数);在 epoch 模式下是一个带小数的「epoch 进度」。PTQ 校准一般不设 epoch,所以门控主要服务训练式压缩。

#### 4.5.3 源码精读

`update_event` 的训练链三分支:

[modifier.py:152-176](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L152-L176) —— 分别处理「进入(BATCH_START + should_start)」「退出(BATCH_END + should_end)」「区间内持续(on_update)」,每次进入/退出后都附带一次 `on_update`,并维护 `started_` / `ended_`。

两个门控函数:

[modifier.py:177-196](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L177-L196) —— `should_start` 在 `start is None` 时直接返回 `False`(未配置则不启动),否则按半开区间判定;`should_end` 在 `end is None` 时返回 `False`。

训练钩子与 `on_finalize` 的默认实现(全部默认 `pass`/`True`):

[modifier.py:213-269](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L213-L269) —— `on_finalize` 默认 `return True`;`on_start` / `on_update` / `on_end` 默认 `pass`。可见训练链同样是「全可选」的。

#### 4.5.4 代码实践

写一个训练式 modifier,设置 `start=0, end=2`,然后手动喂入一串 `BATCH_START` / `BATCH_END` 事件(逐步递增 `global_step`),打印每次触发的钩子。预期:第 0 步 `BATCH_START` 触发 `on_start + on_update`;中间步触发 `on_update`;到达 `end` 的 `BATCH_END` 触发 `on_end + on_update`。由于「非 epoch 模式」下 `current_index == global_step`,你可以直接用 `Event(type_=EventType.BATCH_START, global_step=k)` 来模拟第 k 步。

> 待本地验证:具体打印行数取决于你喂入的事件序列,但「进入只一次、退出只一次、区间内每次 update」的模式是确定的。

#### 4.5.5 小练习与答案

**练习 1**:一个 modifier 配置 `start=5, end=10`,第 3 步的 `BATCH_START` 会触发 `on_start` 吗?第 5 步呢?

**参考答案**:第 3 步不会(`3 < start=5`,不满足 `start <= current`)。第 5 步会(`5 >= start=5` 且 `5 < end=10`,落在 \\([5,10)\\) 内,`should_start` 为真,且 `started_` 仍为 `False`)。

**练习 2**:`on_update` 在一次完整训练生命周期里大概被调用多少次?

**参考答案**:进入时 1 次(随 `on_start` 后附带)、退出时 1 次(随 `on_end` 后附带)、区间内每个 `BATCH_*` 事件 1 次。所以总次数 = 区间内 batch 事件数 + 2(若 start/end 命中真实事件)。注意:区间外的 batch 事件既不进 `on_start` 也不进 `on_update`。

---

### 4.6 HooksMixin:校准钩子的注册、全局禁用与清理

#### 4.6.1 概念说明

`HooksMixin` 解决的是「校准时如何收集激活/权重统计」的基础设施问题。量化算法需要在某些模块上挂 **forward hook**(例如捕获输入激活喂给 observer),但直接用 PyTorch 的 `module.register_forward_hook` 有两个痛点:

1. 校准过程中有时需要**临时关闭所有 hook**(例如做一次「不触发校准、只为捕获量化后输出」的前向)。
2. 一个 modifier 可能挂很多 hook,需要统一**登记与清理**,避免泄漏。

`HooksMixin` 通过「包装注册」+「类级禁用开关」+「句柄登记表」三件套解决它们。这就是为什么 u2-l2 / 4.4 反复强调「校准钩子要用 `self.register_hook` 而不是裸的 `module.register_*_hook`」。

#### 4.6.2 核心流程

```text
挂载:  handle = modifier.register_hook(module, hook_fn, hook_type="forward")
       # 内部把 hook_fn 包成 wrapped_hook,登记 handle 到 self._hooks
禁用:  with HooksMixin.disable_hooks():          # 全类开关打开
           model.forward(...)                     # wrapped_hook 检测到开关,直接 return
       # 退出 with,开关关闭,hook 恢复
保留:  with HooksMixin.disable_hooks(keep={handle}):
           model.forward(...)                     # 仅 keep 集合里的 hook 仍生效
清理:  modifier.remove_hooks()                   # 移除并从 self._hooks 剔除
```

关键在于 `wrapped_hook`:它在每次被调用时检查类级开关 `_HOOKS_DISABLED`,若开启且本句柄不在「保留集」`_HOOKS_KEEP_ENABLED` 里,就**直接返回不执行原 hook**。

#### 4.6.3 源码精读

类级开关与每实例句柄表(注意都是下划线私有属性):

[hooks.py:45-50](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L45-L50) —— `_HOOKS_DISABLED` / `_HOOKS_KEEP_ENABLED` 是挂在类上的全局开关与保留集;`_hooks` 是每个 modifier 实例自己的句柄集合。

`disable_hooks` 上下文管理器,支持嵌套与 `keep`:

[hooks.py:52-67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L52-L67) —— 进入时置 `_HOOKS_DISABLED=True` 并把 `keep` 并入保留集,退出时恢复;多个 `with` 嵌套时保留集取并集。

`register_hook` 的核心:包装 + 登记:

[hooks.py:69-106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L69-L106) —— 用 `@wraps(hook)` 定义 `wrapped_hook`,它在调用时检查禁用开关(见 [hooks.py:89-99](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L89-L99));真正注册由 `_get_register_function` 拿到的函数完成,句柄加入 `self._hooks`。

`remove_hooks` 清理:

[hooks.py:108-121](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L108-L121) —— 遍历句柄调用 `.remove()`,再从 `self._hooks` 剔除;不传参则移除该 modifier 的全部 hook。

> 这正好对应校准生命周期:`on_calibration_start` 里 `register_hook` 挂上 observer hook,`on_calibration_end` 里 `remove_hooks` 卸掉。`HooksMixin.disable_hooks` 则在 sequential pipeline「第二遍不触发校准的前向」里大显身手(见 sequential/pipeline.py 中 `with HooksMixin.disable_hooks():`)。

#### 4.6.4 代码实践

构造一个空壳 modifier 与一个 `torch.nn.Linear`,用 `modifier.register_hook(linear, fn, "forward")` 挂一个打印 hook,然后分别:(a) 直接前向、(b) 在 `with HooksMixin.disable_hooks():` 里前向、(c) 调用 `modifier.remove_hooks()` 后再前向。观察打印是否出现。预期:(a) 出现、(b) 不出现、(c) 不出现。

#### 4.6.5 小练习与答案

**练习 1**:`disable_hooks` 是「实例级」还是「类级」禁用?这意味着什么?

**参考答案**:类级(看 `_HOOKS_DISABLED` 是 `ClassVar`)。这意味着 `with HooksMixin.disable_hooks()` 会**同时禁用所有 modifier 实例**挂的 hook。这样设计是因为校准的「不触发 hook 的前向」通常需要对全模型生效,而非只针对某一个 modifier。

**练习 2**:`keep` 参数解决什么问题?

**参考答案**:在「整体禁用 hook」时,有时希望**保留少数特定 hook 继续生效**(例如只保留某几个用于捕获输出的 hook,关掉所有校准统计 hook)。`keep={handle}` 把这些句柄放进保留集,`wrapped_hook` 检测到句柄在保留集里就照常执行。

---

## 5. 综合实践

把本讲所有要点串起来:手写一个空壳 modifier,重写**全部钩子**让它们各自打印一行,然后手动驱动它走完**校准生命周期**,最后再演示一个**训练生命周期**的片段。目标是亲眼看一遍钩子的触发顺序,把「模板方法 + 双生命周期」从概念变成肌肉记忆。

### 5.1 实践目标

- 验证 `Modifier` 是 pydantic 模型,子类只需重写钩子即可。
- 观察校准链钩子顺序:`on_initialize → on_calibration_start → on_sequential_epoch_end → on_calibration_end → on_finalize`。
- 体会 `on_initialize` 是唯一**必须**实现的钩子(不实现会因 `@abstractmethod` 报错)。
- 体会 `update_event(SEQUENTIAL_EPOCH_END)` 必须带 `modules` 参数。

### 5.2 操作步骤

把下面脚本存为 `trace_modifier.py`(示例代码,非项目原有文件),在装好 `llmcompressor` 的环境里运行。

```python
# 示例代码:手动驱动一个空壳 Modifier,观察钩子顺序
from llmcompressor.modifiers import Modifier
from llmcompressor.core import State
from llmcompressor.core.events import Event, EventType


class TracingModifier(Modifier):
    \"\"\"重写全部钩子,每个钩子只打印自己被调用\"\"\"

    def on_initialize(self, state, **kwargs):
        print(\"  -> on_initialize\")
        return True

    def on_finalize(self, state, **kwargs):
        print(\"  -> on_finalize\")
        return True

    def on_event(self, state, event, **kwargs):
        print(f\"  -> on_event(catch-all, type={event.type_})\")

    def on_calibration_start(self, state, event, **kwargs):
        print(\"  -> on_calibration_start\")

    def on_sequential_epoch_end(self, state, event, modules, **kwargs):
        print(f\"  -> on_sequential_epoch_end(modules={modules})\")

    def on_calibration_end(self, state, event, **kwargs):
        print(\"  -> on_calibration_end\")


# 1) 构造 modifier 与一个空的 state
mod = TracingModifier()
state = State()

# 2) 校准生命周期:initialize -> CALIBRATION_START -> SEQUENTIAL_EPOCH_END -> CALIBRATION_END -> finalize
print(\"== initialize ==\")
mod.initialize(state)

print(\"== CALIBRATION_START ==\")
mod.update_event(state, Event(type_=EventType.CALIBRATION_START))

print(\"== SEQUENTIAL_EPOCH_END (注意必须传 modules) ==\")
mod.update_event(state, Event(type_=EventType.SEQUENTIAL_EPOCH_END), modules=[])

print(\"== CALIBRATION_END ==\")
mod.update_event(state, Event(type_=EventType.CALIBRATION_END))

print(\"== finalize ==\")
mod.finalize(state)

print(\"== 状态标志 ==\")
print(\"initialized_ =\", mod.initialized_, \"finalized_ =\", mod.finalized_,
      \"started_ =\", mod.started_, \"ended_ =\", mod.ended_)
```

### 5.3 需要观察的现象

- 每个事件都会先打印一行 `on_event(catch-all, ...)`,再打印对应的具体钩子——印证「catch-all 在前,分流在后」。
- `on_sequential_epoch_end` 能拿到空的 `modules=[]`,印证 `modules` 由 `**kwargs` 透传。
- 末尾状态标志应为 `initialized_=True, finalized_=True, started_=True, ended_=True`。

### 5.4 预期结果

钩子顺序(忽略 catch-all 的细节)形如:

```text
on_initialize
on_event + on_calibration_start
on_event + on_sequential_epoch_end(modules=[])
on_event + on_calibration_end
on_finalize
```

### 5.5 进阶:换成训练生命周期

把第 2) 段换成下面这段,设置 `start=0, end=2`,喂入若干 batch 事件:

```python
train_mod = TracingModifier(start=0, end=2)
train_mod.on_start = lambda state, event, **kw: print(\"  -> on_start\")
train_mod.on_update = lambda state, event, **kw: print(\"  -> on_update\")
train_mod.on_end = lambda state, event, **kw: print(\"  -> on_end\")
state2 = State()
train_mod.initialize(state2)
for step in range(3):
    train_mod.update_event(state2, Event(type_=EventType.BATCH_START, global_step=step))
    train_mod.update_event(state2, Event(type_=EventType.BATCH_END,   global_step=step))
```

预期:第 0 步 `BATCH_START` 触发 `on_start + on_update`;各步触发 `on_update`;第 2 步 `BATCH_END`(达到 `end=2`)触发 `on_end + on_update`。

> 待本地验证:不同 `start/end` 与事件序列会产生不同的具体打印,但「进入一次、退出一次、区间内持续 update」的模式稳定可复现。

## 6. 本讲小结

- `Modifier(ModifierInterface, HooksMixin)` 是所有压缩算法的基类,三重身份分别管「生命周期契约」「hook 管理」「双生命周期骨架」。
- 它采用模板方法模式:`initialize` / `update_event` / `finalize` 是带状态校验的骨架方法,真正的算法逻辑放在 `on_*` 钩子里;**子类必须实现 `on_initialize`**,其余钩子全部默认空实现、可选重写。
- 状态标志 `initialized_` / `finalized_` / `started_` / `ended_` 因带下划线而成为 pydantic 私有运行时状态;配置字段 `start` / `end` / `update` / `index` 才是被校验的字段。
- `update_event` 先无条件跑 catch-all `on_event`,再按事件类型分流:**校准链**(`CALIBRATION_START` → `SEQUENTIAL_EPOCH_END` → `CALIBRATION_END`)服务 PTQ,**训练链**(`BATCH_START`/`BATCH_END` + `should_start`/`should_end` 门控)服务微调式压缩,两条链互斥。
- `on_sequential_epoch_end` 的 `modules` 参数由管线经 `LifecycleCallbacks` → `session.event` → `lifecycle.event` → `update_event` 的 `**kwargs` 透传而来,让逐层算法知道「本轮处理哪些模块」。
- `HooksMixin` 提供「包装注册 + 类级禁用 + 句柄登记」三件套,是校准 hook(`register_hook` / `disable_hooks` / `remove_hooks`)的基础设施,对应 `on_calibration_start` 挂 hook、`on_calibration_end` 卸 hook 的成对用法。

## 7. 下一步学习建议

- **紧接着读 u2-l4(ModifierFactory)**:看本讲的 `Modifier` 子类是如何被**自动发现与注册**的——工厂会遍历 `llmcompressor.modifiers` 子包,收集所有以 `Modifier` 结尾的类。
- **然后读 u2-l5(Recipe)**:看多个 modifier 如何被 recipe 编排成有序列表,并被 `CompressionLifecycle.initialize` 逐个 `initialize`。
- **进入第三单元**:u3-l1 会精读 `QuantizationModifier`——它是 `Modifier` 的第一个真实子类,你将看到它如何重写 `on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` 来完成量化。本讲是读懂它的一切前提。
- **扩展阅读**:想理解 `should_start` 里 `current_index` 的 epoch 小数语义,可回头读 [event.py:164-181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/events/event.py#L164-L181) 的 `current_index` property;想理解 sequential 管线何时触发 `SEQUENTIAL_EPOCH_END`,可读 `src/llmcompressor/pipelines/sequential/pipeline.py`(u3-l5 会专题讲解)。
