# 注册表驱动的插件架构

## 1. 本讲目标

AMCT 要同时支持十几种大模型、若干种量化算法、三种量化数据类型、多种求解器——如果用一堆 `if model_name == "qwen3": ...` 来分发，代码会迅速变成难以维护的意大利面。AMCT 的解法是**注册表（Registry）**：一个统一的「名字 → 对象」字典，配合装饰器，让插件在被 import 时**自动登记**自己。

学完本讲，你应当能够：

- 说清 `Registry` 基类的 `register / get / get_item / list_all` 接口各自做什么、装饰器的两种写法怎么兼容。
- 画出 MODEL / SOLVER / DTYPE / ALGO 四大注册表分别定义在哪个文件、由哪个 `register_*` 函数填充、各自装的是什么。
- 看懂「`_REGISTERED` 幂等保护 + setup 里惰性触发」这套副作用注册模式，并理解它为何能避免重复注册报错。
- 自己动手用 `Registry` 类注册一个自定义对象，并复现「重复注册抛错」的行为。

本讲承接 [u3-l2](u3-l2-workflow-skeleton.md) 讲过的「`setup()` 第一行调用 `_register_components()`」，把那四行注册函数背后到底注册了什么、怎么注册的彻底讲透。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（不熟悉也没关系，下面会顺带解释）：

- **注册表模式（Registry Pattern）**：维护一个全局字典 `{名字: 对象}`，代码运行时按名字查表取出对象，而不是用 `if/elif` 硬编码分发。它把「新增一个插件」的成本从「改分发逻辑」降到「写一行装饰器」。
- **装饰器（Decorator）**：Python 里形如 `@SOMETHING` 的语法糖。`@ALGO_REGISTRY.register(name="lwc")` 放在 `class LWC:` 上方，等价于「定义完 LWC 后，把它作为参数传给 `register(...)` 执行一次」。本讲会看到它如何被用来「顺手登记」。
- **import 的副作用（side-effect import）**：Python 中 `import` 一个模块会**执行该模块顶层代码**。AMCT 故意利用这一点——import 一个算法类的目的不是「拿到这个类」（代码里常标 `# noqa: F401` 表示「我知道它没被用」），而是「触发它类定义上方的 `@...register` 装饰器，把它登记进注册表」。
- **幂等（idempotent）**：同一个操作做一次和做多次效果相同。注册函数被设计成「第二次调用直接 return，什么也不做」。

如果你已经学过 [u1-l3](u1-l3-directory-structure.md) 的目录地图和 [u3-l2](u3-l2-workflow-skeleton.md) 的 Workflow 编排骨架，本讲会非常顺。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `amct_pytorch/common/utils/registry_factory.py` | **地基**。定义 `RegistryItem` 数据类与 `Registry` 基类，所有注册表都基于它。 |
| `amct_pytorch/common/models/__init__.py` | 创建 `MODEL_REGISTRY`（模型适配器注册表）。 |
| `amct_pytorch/common/models/llm/__init__.py` | 定义 `register_llm_models()`，逐个 import 十余个模型类完成登记。 |
| `amct_pytorch/common/optimization/__init__.py` | 创建 `SOLVER_REGISTRY` 并定义 `register_solvers()`（求解器注册表）。 |
| `amct_pytorch/algorithms/registry_factory.py` | 定义 `QuantAlgorithmRegistry`（`Registry` 的子类）并创建 `ALGO_REGISTRY`（算法注册表）。 |
| `amct_pytorch/algorithms/quant/__init__.py` | 定义 `register_algorithms()`，import 各可学习算法完成登记。 |
| `amct_pytorch/quantization/dtypes/__init__.py` | 创建 `DTYPE_REGISTRY` 并定义 `register_dtype()`（量化数据类型注册表）。 |
| `amct_pytorch/workflows/llm_ptq.py` | `_register_components()` 在 `setup()` 第一行触发四类注册。 |

一个贯穿全讲的**关键事实**先记在心里：四大注册表的「注册表对象」和「填充它的 `register_*` 函数」常常**不在同一个文件**。例如 `MODEL_REGISTRY` 在 `common/models/__init__.py`，而填充它的 `register_llm_models()` 却在 `common/models/llm/__init__.py`；`ALGO_REGISTRY` 在 `algorithms/registry_factory.py`，而 `register_algorithms()` 在 `algorithms/quant/__init__.py`。这种「对象归对象、触发归触发」的分离是有意为之，4.2 节会展开。

## 4. 核心概念与源码讲解

### 4.1 Registry 基类与 RegistryItem

#### 4.1.1 概念说明

`Registry` 是 AMCT 所有注册表的公共基类。你可以把它理解成一个**带校验和友好报错的全局字典**：

- 存的内容不是裸对象，而是一个 `RegistryItem`（含名字、对象本身、以及任意元数据）。
- 写入用 `register`（支持装饰器语法），读取用 `get`（取对象）/ `get_item`（取完整条目）/ `list_all`（列名字）。
- 重复注册会报错，除非显式 `force=True`，防止两个插件意外撞名后被静默覆盖。

之所以要专门造一个基类而不是直接用 `dict`，是因为注册表需要三件 `dict` 做不到的事：装饰器友好的登记方式、重复键的防呆报错、以及「找不到时列出所有可用键」的报错提示。

#### 4.1.2 核心流程

一个注册表的典型生命周期如下：

```text
1. 创建空注册表        REGISTRY = Registry("model")
                       内部 self._items = {}  （dict[str, RegistryItem]）

2. 登记一个插件        @REGISTRY.register(name="qwen3", family="qwen")
                       class Qwen3(...): ...
                       —— 定义类时装饰器执行 → _register() 存入 RegistryItem

3. 按名字查表          cls = REGISTRY.get("qwen3")        → 返回 Qwen3 类本身
                       item = REGISTRY.get_item("qwen3")  → 返回 RegistryItem（含 family 等元数据）
                       REGISTRY.has("qwen3")              → True
                       REGISTRY.list_all()                → ["deepseek_v3_2", "qwen3", ...]

4. 撞名防呆            再次 register(name="qwen3") → KeyError，提示用 force=True 覆盖
                       get("不存在的名字")         → KeyError，并列出所有可用键
```

注意 `get` 和 `get_item` 的区别：`get` 只返回**被注册的对象本身**（多数情况就够用）；`get_item` 返回**完整条目**，当你需要读取当初注册时附带的 `family` / `task` / `targets` 等元数据时才用它。

#### 4.1.3 源码精读

先看存什么——`RegistryItem` 是一个**不可变**（`frozen=True`）数据类，三个字段：名字、目标对象、元数据字典：

[amct_pytorch/common/utils/registry_factory.py:22-26](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L22-L26) —— `RegistryItem`：注册表里每一条都是一个 `(name, target, metadata)` 三元组。

```python
@dataclass(frozen=True)
class RegistryItem:
    name: str
    target: Any
    metadata: dict[str, Any] = field(default_factory=dict)
```

再看 `Registry` 的构造与几个语法糖。`__call__` 直接转发给 `register`，于是 `REGISTRY(obj)` 和 `REGISTRY.register(obj)` 等价；`__contains__` 让 `key in REGISTRY` 可用；`__repr__` 打印时按字母序列出所有键，调试很方便：

[amct_pytorch/common/utils/registry_factory.py:29-46](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L29-L46) —— `Registry` 的构造与 `__call__/__contains__/__repr__/name`：注册表就是一个带名字的 `_items` 字典。

核心是 `register` 方法。它用一个小技巧**同时兼容两种装饰器写法**——带括号和不带括号：

[amct_pytorch/common/utils/registry_factory.py:48-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L48-L63) —— `register`：判断 `obj` 是否为 `None` 来区分「带括号」与「不带括号」两种用法。

```python
def register(self, obj=None, *, name=None, force=False, **metadata):
    def decorator(target):
        key = name or target.__name__      # 没给 name 就用类名/函数名
        self._register(key, target, force=force, metadata=metadata)
        return target                      # 原样返回，不改变被装饰对象
    if obj is not None:
        return decorator(obj)              # 不带括号：obj 就是类本身，立即登记
    return decorator                       # 带括号：返回装饰器，等类定义出来再套上去
```

- 写 `@REGISTRY.register(name="lwc")`（带括号）：`obj` 是 `None`，返回 `decorator`，随后套在类上。
- 写 `@REGISTRY.register`（不带括号）：`obj` 直接是被装饰的类，`decorator(obj)` 立即执行，键名取 `target.__name__`。

> 注：AMCT 仓库里所有真实用法都走**带括号**形式（显式传 `name=...`），不带括号的形式是基类提供的备用能力。

读取接口有三个，区别在于返回粒度。`get` 取对象、`get_item` 取条目，两者找不到时都抛 `KeyError` 并**列出所有可用键**（或 `<empty>`），这是非常友好的报错：

[amct_pytorch/common/utils/registry_factory.py:65-92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L65-L92) —— `get/get_item/has/list_all/items`：读取族接口，找不到时把可用键全列出来。

最后是真正落盘的 `_register`。重复键默认抛错、`force=True` 才允许覆盖；存的是 `RegistryItem`，元数据会被 `dict(metadata)` 拷一份防止外部篡改：

[amct_pytorch/common/utils/registry_factory.py:94-100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L94-L100) —— `_register`：撞名防呆（`force` 开关）+ 包成 `RegistryItem` 入库。

```python
def _register(self, key, obj, force, metadata):
    if key in self._items and not force:
        raise KeyError(
            f"'{key}' already registered in '{self._name}'. "
            f"Use @REGISTRY.register(force=True) to override.")
    self._items[key] = RegistryItem(name=key, target=obj, metadata=dict(metadata))
```

#### 4.1.4 代码实践

**实践目标**：用真实的 `Registry` 类，亲手走一遍「登记 → 查询 → 撞名报错」的完整流程。

**操作步骤**：

1. 确认已按 [u1-l2](u1-l2-build-and-install.md) 安装 `amct_pytorch`（若未安装，可把第一行换成下方「示例代码」里的最小自实现版本，效果一致）。
2. 新建一个 `play_registry.py`，粘贴下面这段**示例代码**并运行 `python play_registry.py`：

```python
# 示例代码：演示 Registry 的 register/get/list_all 与重复注册行为
from amct_pytorch.common.utils.registry_factory import Registry   # 用真实基类

# （未安装 amct 时，可用下面这段等价的最小实现替换上一行）
# from dataclasses import dataclass, field
# from typing import Any
# @dataclass(frozen=True)
# class RegistryItem:
#     name: str; target: Any; metadata: dict = field(default_factory=dict)
# class Registry:
#     def __init__(self, name): self._name = name; self._items = {}
#     def register(self, obj=None, *, name=None, force=False, **metadata):
#         def deco(t):
#             k = name or t.__name__; self._items[k] = RegistryItem(k, t, dict(metadata)); return t
#         return deco(obj) if obj is not None else deco
#     def get(self, k): return self._items[k].target
#     def list_all(self): return sorted(self._items)

MY_REGISTRY = Registry("greeting")

@MY_REGISTRY.register(name="hello", description="say hi")
def hello():
    return "hi"

@MY_REGISTRY.register(name="bye", description="say bye")
def bye():
    return "bye"

print("所有已注册:", MY_REGISTRY.list_all())        # ['bye', 'hello']
print("取出 hello:", MY_REGISTRY.get("hello")())    # hi

try:
    @MY_REGISTRY.register(name="hello")              # 撞名！
    def hello2():
        return "dup"
except KeyError as e:
    print("撞名报错:", e)
```

**需要观察的现象**：

- `list_all()` 输出按字母序排列的键。
- `get("hello")` 拿到的是函数对象本身，加 `()` 才执行。
- 第二次用 `name="hello"` 注册时，抛出 `KeyError`，提示信息里出现 `Use @REGISTRY.register(force=True) to override.`。

**预期结果**：`list_all` → `['bye', 'hello']`；撞名时抛 `KeyError` 且报错文案与源码一致。若你把第二次注册改成 `@MY_REGISTRY.register(name="hello", force=True)`，则不再报错、旧条目被覆盖。**待本地验证**：不同 amct 版本若改动了报错文案，以你本地的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`get(key)` 和 `get_item(key)` 返回的东西有什么不同？什么时候必须用后者？

> **参考答案**：`get` 返回被注册的**对象本身**（如一个类、一个函数）；`get_item` 返回**完整的 `RegistryItem`**，含 `name`、`target` 和 `metadata`。当你需要读取当初注册时附带的元数据（如模型的 `family`、算法的 `targets`）时，必须用 `get_item`，因为 `get` 拿不到这些附加信息。

**练习 2**：为什么 `_register` 里对撞名要主动抛 `KeyError`，而不是像普通字典那样静默覆盖？

> **参考答案**：注册表里的名字是分发依据，两个插件撞名通常意味着**有 bug**（比如两个适配器都叫 `qwen3`，运行时分不清该用谁）。静默覆盖会把这种 bug 隐藏到运行时才暴露，且极难排查；主动抛错能把问题前移到「注册时刻」。若确实要覆盖，显式写 `force=True` 表达意图，等于「我知道有重复，我故意的」。

### 4.2 四大注册表与副作用注册模式

#### 4.2.1 概念说明

AMCT 的 LLM PTQ 主流程有四类可插拔组件，对应四个全局注册表：

| 注册表 | 装什么 | 典型键 | 典型值 |
| --- | --- | --- | --- |
| `MODEL_REGISTRY` | 模型适配器类 | `"qwen3"`、`"deepseek_v3_2"`、`"hy_v3"` | 继承 `BaseModel` 的适配器类 |
| `SOLVER_REGISTRY` | 求解器类（量化参数优化器） | `"block"`、`"global"` | `BlockwiseSolver` 等 |
| `DTYPE_REGISTRY` | 量化数据类型类 | `"int"`、`"mxfp"`、`"hifp"` | `QuantDequantInt` 等 |
| `ALGO_REGISTRY` | 量化算法类 | `"lwc"`、`"lac"`、`"flatquant"` | 继承 `QuantAlgorithmBase` 的算法类 |

它们都基于 `Registry` 基类，但有三处值得注意的差异，本节逐一讲清。

第一，**注册表对象和填充函数常不在同一文件**。这是「定义归定义、填充归填充」的分层：注册表对象是很轻的东西（一行 `Registry("xxx")`），放在「公共定义处」；而真正把十几个类塞进去的 `register_*()` 函数，则放在更靠近业务的地方。

第二，**三个是普通 `Registry`，ALGO 是特化子类**。`ALGO_REGISTRY` 实际是 `QuantAlgorithmRegistry`，它在登记时额外校验「必须是 `QuantAlgorithmBase` 子类」，是一个**带类型守卫的注册表**。

第三，**登记靠 import 副作用**。`register_*()` 函数体里没有 `REGISTRY.register(...)` 调用，只有一连串 `from .xxx import XxxClass  # noqa: F401`——真正的登记发生在每个类定义上方的 `@REGISTRY.register(...)` 装饰器里，import 只是「触发器」。

#### 4.2.2 核心流程

以「新增并登记一个算法」为例，副作用注册的完整链路是：

```text
① 用户在 auto_clip.py 顶部写：
     @ALGO_REGISTRY.register(name="lwc", targets=("weight",), ...)
     class LWC(QuantAlgorithmBase): ...

② 运行时某处调用 register_algorithms()（见 4.3）

③ register_algorithms() 内部执行：
     from .auto_clip import LAC, LWC     # 这一行触发 auto_clip.py 被执行

④ Python 执行 auto_clip.py 顶层 → 遇到 @ALGO_REGISTRY.register(...) 装饰器
   → ALGO_REGISTRY._register("lwc", LWC, ...) 被调用
   → LWC 进入 ALGO_REGISTRY._items

⑤ 之后任何地方 ALGO_REGISTRY.get("lwc") 都能拿到 LWC 类
```

关键点：**装饰器执行 = 登记发生**，而装饰器只在模块被 import 时才执行。所以「谁 import 了那个模块」决定了「什么时候登记」。AMCT 把这个时机统一收口到 `register_*()` 函数里，再由 Workflow 的 `setup()` 调用它们（见 4.3）。

#### 4.2.3 源码精读

**模型注册表**。`MODEL_REGISTRY` 在 `common/models/__init__.py` 里一行创建，文件里**没有** `_REGISTERED`、**没有** `register_llm_models`——它只负责「给出注册表对象」：

[amct_pytorch/common/models/__init__.py:18-22](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/__init__.py#L18-L22) —— `MODEL_REGISTRY` 的创建：仅一行，纯净的注册表对象。

```python
__all__ = ["MODEL_REGISTRY"]
from amct_pytorch.common.utils.registry_factory import Registry
MODEL_REGISTRY = Registry("model")
```

真正的填充函数在它的子包 `common/models/llm/__init__.py` 里，逐个 import 十余个模型类（注意全部带 `# noqa: F401`，说明 import 的目的不是「使用这个名字」，而是「触发装饰器」）：

[amct_pytorch/common/models/llm/__init__.py:26-38](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L26-L38) —— `register_llm_models` 内部：靠 import 副作用把 DeepSeek/Qwen/GLM/HyV3/LongCat 等十余个适配器逐一登记。

每个模型类上方都有装饰器，附带 `family`/`task` 等元数据。以 Qwen3 为例：

[amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py:37-40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L37-L40) —— 模型适配器自登记：`name` 是分发用的键，`family`/`task` 是供 `get_item` 读取的元数据。

```python
@MODEL_REGISTRY.register(
    name="qwen3",
    task="llm",
    family="qwen",
    ...
)
class Qwen3(...):
```

**求解器注册表**。`SOLVER_REGISTRY` 与 `register_solvers` 在同一文件 `common/optimization/__init__.py`，模式相同：

[amct_pytorch/common/optimization/__init__.py:22-34](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/__init__.py#L22-L34) —— `SOLVER_REGISTRY` 创建 + `register_solvers`：import `BlockwiseSolver` 触发其 `@SOLVER_REGISTRY.register(name="block", ...)`。

而 `BlockwiseSolver` 类定义上方的装饰器，正是「登记」真正发生处：

[amct_pytorch/common/optimization/blockwise_solver.py:32-34](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/blockwise_solver.py#L32-L34) —— `BlockwiseSolver` 自登记为 `"block"`，这也是 PTQ workflow 用 `granularity="block"` 时去 `SOLVER_REGISTRY` 查表拿到的求解器。

**数据类型注册表**。`DTYPE_REGISTRY` 与 `register_dtype` 同在 `quantization/dtypes/__init__.py`，import 三个类对应 `int`/`mxfp`/`hifp` 三种量化数据类型：

[amct_pytorch/quantization/dtypes/__init__.py:22-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/__init__.py#L22-L37) —— `DTYPE_REGISTRY` 创建 + `register_dtype`：登记 int/mxfp/hifp 三个 `QuantDequant*` 类。

每个类自登记处以 `int` 为例：

[amct_pytorch/quantization/dtypes/int.py:29-30](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L29-L30) —— `QuantDequantInt` 自登记为 `"int"`，对应 CLI 参数 `--quant_dtype int`。

**算法注册表（特化子类）**。这是四个里最特殊的一个。`ALGO_REGISTRY` **不是** `Registry("algo")`，而是 `QuantAlgorithmRegistry("algo")`——后者继承 `Registry`，并**重写 `_register`** 加入类型守卫：只允许 `QuantAlgorithmBase` 的子类被登记，否则抛 `TypeError`。这就是 4.2.1 说的「带类型守卫的注册表」：

[amct_pytorch/algorithms/registry_factory.py:22-31](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/registry_factory.py#L22-L31) —— `QuantAlgorithmRegistry`：重写 `_register`，校验 `issubclass(obj, QuantAlgorithmBase)` 后再交给父类 `super()._register(...)`，并实例化 `ALGO_REGISTRY`。

```python
class QuantAlgorithmRegistry(Registry):
    def _register(self, key, obj, force, metadata):
        if not isinstance(obj, type) or not issubclass(obj, QuantAlgorithmBase):
            raise TypeError(
                f"Algorithm '{key}' must inherit QuantAlgorithmBase, got {obj!r}.")
        super()._register(key, obj, force, metadata)

ALGO_REGISTRY = QuantAlgorithmRegistry("algo")
```

算法类的自登记长这样，注意它比别的注册表多一个 `targets=(...)` 元数据（这个元数据如何被用来做 weight/activation/structure 路由，是 [u6-l2](u6-l2-algo-target-routing.md) 的主题，本讲先知道「有这么个元数据」即可）：

[amct_pytorch/algorithms/quant/auto_clip.py:24-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L24-L28) —— LWC 自登记：`name="lwc"`、`targets=("weight",)`，由 `register_algorithms()` 里 `from .auto_clip import LAC, LWC` 触发。

填充 `ALGO_REGISTRY` 的 `register_algorithms()` 却在**另一个目录** `algorithms/quant/__init__.py` 里——这正是「对象归对象、触发归触发」的体现：

[amct_pytorch/algorithms/quant/__init__.py:33-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/__init__.py#L33-L44) —— `register_algorithms`：import 五个可学习算法（LAC/LWC/AutoRound/OmniQuant/FlatQuant）触发各自装饰器。

#### 4.2.4 代码实践

**实践目标**：用一张「四表对照表」把四个注册表的「对象在哪、触发函数在哪、装什么」固化下来，并通过真实代码核对。

**操作步骤**：

1. 打开本讲 4.2.3 引用的五个 `__init__.py` / `registry_factory.py`。
2. 填写下面这张表（答案已给出，请逐行回原文核对）：

| 注册表对象 | 定义位置（文件:行） | 触发函数 | 触发函数位置 | 登记的键示例 |
| --- | --- | --- | --- | --- |
| `MODEL_REGISTRY = Registry("model")` | `common/models/__init__.py:22` | `register_llm_models()` | `common/models/llm/__init__.py:21` | qwen3, deepseek_v3_2, hy_v3 ... |
| `SOLVER_REGISTRY = Registry("solver")` | `common/optimization/__init__.py:22` | `register_solvers()` | `common/optimization/__init__.py:27` | block, global |
| `DTYPE_REGISTRY = Registry("dtype")` | `quantization/dtypes/__init__.py:22` | `register_dtype()` | `quantization/dtypes/__init__.py:27` | int, mxfp, hifp |
| `ALGO_REGISTRY = QuantAlgorithmRegistry("algo")` | `algorithms/registry_factory.py:31` | `register_algorithms()` | `algorithms/quant/__init__.py:33` | lwc, lac, flatquant, omniquant, auto_round |

3. 在四个 `register_*()` 函数体里数一下分别 import 了几个类，与上表「键示例」一列对齐。

**需要观察的现象**：

- `MODEL_REGISTRY` 的定义文件里**没有**触发函数；其余三张表「对象」与「触发函数」也未必同文件。
- 只有 `ALGO_REGISTRY` 用了子类 `QuantAlgorithmRegistry`，其余三张表都是普通 `Registry(...)`。

**预期结果**：四行全部能在源码中逐一定位；`register_llm_models` 里 import 了 13 个模型类（含 dense 与 MoE 变体），`register_algorithms` 里 import 了 5 个算法类。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MODEL_REGISTRY` 放在 `common/models/__init__.py`，而 `register_llm_models()` 放在 `common/models/llm/__init__.py`，不写在一起？

> **参考答案**：分层解耦。`common/models/__init__.py` 是「公共定义层」，只提供空的注册表对象，不依赖任何具体模型；具体模型适配器（及其重依赖，如 transformers 各模型结构）放在 `common/models/llm/` 子包里。这样「只想拿到空注册表」的代码 import 父包即可，不会被迫加载全部模型；真正需要模型时才由 `register_llm_models()` 显式触发子包 import。这与 [u1-l3](u1-l3-directory-structure.md) 讲过的「重依赖懒加载」是同一种思路。

**练习 2**：`QuantAlgorithmRegistry` 重写 `_register` 加了类型校验，这相对于普通 `Registry` 多带来了什么好处？

> **参考答案**：把「算法必须继承 `QuantAlgorithmBase`」这个**契约**从「运行时偶然发现」前移到「注册时强制检查」。如果有人误把一个普通函数或非算法类用 `@ALGO_REGISTRY.register` 登记进来，会立刻在注册阶段抛 `TypeError`，而不是等到后续调用算法接口（`calib_forward`/`trainable_params` 等）时才出现莫名其妙的 `AttributeError`。这是「注册表作为扩展点的类型守卫」的典型用法。

**练习 3**：`register_algorithms()` 里每个 import 后面都跟了 `# noqa: F401`，这个注释说明什么？

> **参考答案**：`F401` 是 lint 规则「import 了但未使用」的告警。加 `# noqa: F401` 是在告诉检查器「我知道这个 import 的名字没在本文件用到，但请不要报警」——因为这里的 import **不是**为了拿到那个名字，而是为了**触发模块顶层类定义上的 `@...register` 装饰器**（即 import 副作用）。这是 AMCT 注册模式的一个标志性写法。

### 4.3 _REGISTERED 幂等保护与注册触发时机

#### 4.3.1 概念说明

副作用注册有一个隐患：**装饰器在每次 import 时都会执行**，但 Python 的模块缓存（`sys.modules`）保证「同一个模块在同一个进程里通常只 import 一次」，所以单看模块级好像不会重复。可一旦你把触发逻辑包成 `register_algorithms()` 这样的**函数**，它在一次进程里可能被调用多次（例如连跑两条命令、或不同 Workflow 都调一次），函数体里的 import 虽然因为模块缓存而「不重复执行」，但「函数被多次调用」这件事本身需要被防御性地挡住——否则万一某些代码路径绕过模块缓存直接触发了登记，就会出现 4.1 讲过的「撞名 `KeyError`」。

AMCT 的解法是给每个 `register_*()` 配一个**模块级布尔标志 `_REGISTERED`**：第一次调用时为 `False`，执行完 import 后置 `True`；之后再调用直接 `return`。这就是「幂等保护」。

> 一个细节：上面说「模块缓存通常保证不重复」，那 `_REGISTERED` 是不是多此一举？不是。它是**纵深防御**：既挡住「同一进程多次调用 register_*」的重复 import 开销，也为未来可能的「非 import 式登记」（如直接 `register_*` 内显式调用 `REGISTRY.register`）预留安全网。即使模块缓存兜住了 99% 的情况，剩下 1% 一旦发生，撞名报错会很难查。一个布尔标志的成本极低，收益是确定性。

#### 4.3.2 核心流程

四个 `register_*()` 函数结构完全同构，套路如下：

```text
模块级： _REGISTERED = False

def register_xxx():
    global _REGISTERED
    if _REGISTERED:           # 第二次及以后：直接返回，什么都不做
        return
    from .a import A          # 第一次：import 各模块 → 触发各自 @...register
    from .b import B
    _REGISTERED = True        # 标记「已注册」，之后幂等
```

而**谁第一次调用它们**？是 Workflow 的 `setup()`。在 [u3-l2](u3-l2-workflow-skeleton.md) 我们提过 `setup()` 第一行是 `_register_components()`，现在看清它的内容——它就是把这四个 `register_*()` 按固定顺序各调一遍：

```text
LlmPtqWorkflow.setup()
  └─ _register_components()           # setup 第一行
       ├─ register_algorithms()        → 填 ALGO_REGISTRY
       ├─ register_llm_models()        → 填 MODEL_REGISTRY
       ├─ register_dtype()             → 填 DTYPE_REGISTRY
       └─ register_solvers()           → 填 SOLVER_REGISTRY
  └─ _build_pipeline()                 # 此时所有注册表已就绪，可按名字取对象
```

因为注册发生在 `setup()` 而非 `import` 时，所以叫「**惰性注册**」——只有真正要跑某条命令时才加载并登记相应组件，启动时 `import amct_pytorch` 不会被这些重依赖拖慢（这点与 [u1-l3](u1-l3-directory-structure.md) 的懒加载、[u3-l1](u3-l1-cli-args.md) 的薄壳入口一脉相承）。

#### 4.3.3 源码精读

先看四个 `register_*()` 的同构结构。以 `register_solvers` 为例：

[amct_pytorch/common/optimization/__init__.py:24-34](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/optimization/__init__.py#L24-L34) —— `_REGISTERED` 幂等保护：首调 import 并置位，再调直接 return。

```python
_REGISTERED = False

def register_solvers():
    global _REGISTERED
    if _REGISTERED:
        return                       # 幂等：已注册过就什么都不做
    from .blockwise_solver import BlockwiseSolver  # noqa: F401
    _REGISTERED = True
```

另外三个结构完全一致，只是 import 的清单不同：

[amct_pytorch/algorithms/quant/__init__.py:30-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/__init__.py#L30-L44) —— `register_algorithms` 同构：`_REGISTERED` 守卫 + 五个算法 import。

[amct_pytorch/quantization/dtypes/__init__.py:24-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/__init__.py#L24-L37) —— `register_dtype` 同构：守卫 + 三个 dtype import。

[amct_pytorch/common/models/llm/__init__.py:18-40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L18-L40) —— `register_llm_models` 同构：守卫 + 十三个模型 import。

再看触发点。`_register_components` 是个静态方法，把这四个函数按固定顺序调用——注意顺序不是随便排的：算法/模型/数据类型是「被 pipeline 取用的资源」，求解器最后注册也无妨，因为它们都只是「填表」，真正的取用发生在后面的 `_build_pipeline()`：

[amct_pytorch/workflows/llm_ptq.py:52-57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L52-L57) —— `_register_components`：把四个 `register_*()` 集中调用，是所有注册的统一入口。

```python
@staticmethod
def _register_components():
    register_algorithms()
    register_llm_models()
    register_dtype()
    register_solvers()
```

它在 `setup()` 里是**第一行**，必须早于 `_build_pipeline()`——因为建 pipeline 时要用 `MODEL_REGISTRY.get(model_name)` 取模型适配器，注册表此时必须非空：

[amct_pytorch/workflows/llm_ptq.py:69-74](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L69-L74) —— `setup`：注册在最前，建 pipeline 紧随其后，保证取表时表已填满。

最后给一个真实取用注册表的例子闭环：PTQ workflow 用 `granularity`（默认 `"block"`）去 `SOLVER_REGISTRY` 查表得到求解器类。这就是「注册 → 查表 → 实例化」的完整闭环，注册表的存在意义全在这一查。

#### 4.3.4 代码实践

**实践目标**：验证 `_REGISTERED` 的幂等行为，并追踪一次真实的「注册 → 取用」调用。

**操作步骤（源码阅读型实践，无需 NPU）**：

1. 打开 `amct_pytorch/common/optimization/__init__.py`，在 `register_solvers` 函数体第一行（`if _REGISTERED:` 之后、`from .blockwise_solver import` 之前）**脑内插入**两行打印：`print("first call, importing...")`。再在 `return` 之前插入 `print("skip: already registered")`。
2. 想象在同一进程里连续调用两次 `register_solvers()`，按源码逻辑推演打印顺序。
3. 用 `Grep` 在 `amct_pytorch/workflows/` 下搜索 `MODEL_REGISTRY.get(`，找到 PTQ workflow 真正「用名字取模型适配器」的那一行，确认它一定出现在 `_register_components()` 之后。

**需要观察的现象 / 推演结果**：

- 第一次调用 `register_solvers()` → 打印 `first call, importing...` → 执行 import → `_REGISTERED = True`。
- 第二次调用 → 进入 `if _REGISTERED: return` → 打印 `skip: already registered`，**不**再 import。
- 所有 `*_REGISTRY.get(...)` 的取用点都在 `setup()` 里 `_register_components()` 之后。

**预期结果**：幂等保护确保「无论 `register_*()` 被调用多少次，import 只发生一次、注册表内容稳定不变」。**待本地验证**：第 3 步搜索结果以你本地仓库的实际行号为准。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `_REGISTERED` 守卫，直接在 `register_algorithms()` 里每次都 `from .auto_clip import LAC, LWC`，会发生什么？

> **参考答案**：由于 Python 的 `sys.modules` 缓存，`from .auto_clip import ...` 第二次不会重新执行 `auto_clip.py` 顶层代码，所以「在普通单进程下」装饰器不会重复执行、通常不会撞名。但这只是「碰巧安全」——一旦未来有人改成显式 `ALGO_REGISTRY.register(...)` 调用、或在测试里用 `importlib.reload` 重载模块，就会立刻撞名抛 `KeyError`。`_REGISTERED` 把「绝不重复注册」从「依赖模块缓存的偶然」变成「代码显式保证的必然」，是低成本的确定性保险。

**练习 2**：为什么注册要放在 `setup()` 里做，而不是在 `amct_pytorch/__init__.py` 顶层（import 包时）就做？

> **参考答案**：为了**惰性加载**与**启动加速**。在 `__init__.py` 顶层注册意味着 `import amct_pytorch` 就要加载全部模型适配器、算法、数据类型——这些依赖很重（transformers、torch 等），会让哪怕只是想 `--help` 的启动都变慢。放在 `setup()` 里，只有真正要跑量化时才加载，符合 [u3-l1](u3-l1-cli-args.md) 薄壳入口和 [u1-l3](u1-l3-directory-structure.md) 懒加载的一贯设计。同时 `_REGISTERED` 保证了「setup 被调多次也安全」。

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**为 AMCT「假想新增一个量化数据类型」走通注册全链路**（仅纸面设计，不改真实源码）。

1. **设计类**：假想一个 `QuantDequantFp8` 类（继承 `torch.nn.Module`），它会做 FP8 的量化-反量化。在脑中/纸上写出它类定义上方的装饰器：应登记到哪个注册表？键名叫什么？参考 `int.py:29` 的写法给出 `@DTYPE_REGISTRY.register(name="fp8", description="...")`。
2. **接入触发**：要让 `register_fp8` 能被发现，应在 `quantization/dtypes/__init__.py` 的 `register_dtype()` 函数体里、`_REGISTERED = True` 之前补一行什么 import？为什么这行就够了？
3. **取用闭环**：写出 CLI 参数 `--quant_dtype fp8` 最终是如何变成一个对象的——从 `args.quant_dtype == "fp8"` 到 `DTYPE_REGISTRY.get("fp8")` 拿到 `QuantDequantFp8` 类、再实例化。标注这条链上每个环节发生在哪个文件。
4. **幂等复核**：解释为什么你的「新增」不会破坏「同一进程连跑两次 ptq」的场景（提示：`_REGISTERED`）。

**参考要点**：

- 第 1 步：`@DTYPE_REGISTRY.register(name="fp8", description="quant dequant for fp8")` 放在 `class QuantDequantFp8(...)` 上方，与 `int.py:29` 完全同构。
- 第 2 步：在 `register_dtype()` 内补 `from .fp8 import QuantDequantFp8  # noqa: F401`。这一行 import 会触发 `fp8.py` 顶层执行 → 装饰器登记 → `fp8` 进入 `DTYPE_REGISTRY`，无需别的改动。
- 第 3 步链路：`cli/llm/args.py` 解析 `--quant_dtype`（[u3-l1](u3-l1-cli-args.md)）→ Workflow `setup()` 先调 `register_dtype()` 填表 → `_build_pipeline()` 里 `DTYPE_REGISTRY.get(args.quant_dtype)` 取到 `QuantDequantFp8` 类 → 实例化挂到量化模块上（[u5-l3](u5-l3-quant-apply.md) 会详讲）。
- 第 4 步：第二次 `register_dtype()` 命中 `_REGISTERED == True` 直接 return，不会重复 import、不会撞名。

完成这个练习，你就把「定义插件 → 触发登记 → 按名取用 → 幂等保护」这条 AMCT 扩展主轴完整走了一遍。

## 6. 本讲小结

- `Registry` 是一个带校验的全局字典，存的是 `RegistryItem(name, target, metadata)`；`register` 兼容带/不带括号两种装饰器写法，重复键默认抛 `KeyError`（`force=True` 可覆盖），找不到键时会把所有可用键列出来。
- AMCT 有四大注册表：`MODEL_REGISTRY`（模型适配器）、`SOLVER_REGISTRY`（求解器）、`DTYPE_REGISTRY`（量化数据类型）、`ALGO_REGISTRY`（量化算法）。前三个是普通 `Registry`，`ALGO_REGISTRY` 是特化子类 `QuantAlgorithmRegistry`，登记时强制校验「必须继承 `QuantAlgorithmBase`」。
- 四大注册表的「对象」与「填充它的 `register_*()` 函数」常常不在同一个文件（如 `MODEL_REGISTRY` 在 `common/models/__init__.py`，`register_llm_models` 在 `common/models/llm/__init__.py`），体现「定义归定义、触发归触发」的分层。
- 登记靠 **import 副作用**：`register_*()` 体里只有一连串 `from .x import X  # noqa: F401`，真正的登记发生在每个类上方的 `@...register(...)` 装饰器里。
- 每个 `register_*()` 配一个模块级 `_REGISTERED` 布尔标志做**幂等保护**，首调执行 import 并置位、再调直接 return。
- 四个 `register_*()` 由 Workflow 的 `_register_components()` 在 `setup()` 第一行统一触发，早于 `_build_pipeline()`，保证后续按名取表时注册表已填满；这种「setup 时才注册」即是**惰性注册**，让 `import amct_pytorch` 不被重依赖拖慢。

## 7. 下一步学习建议

本讲把「注册表怎么存、怎么填、什么时候填」讲透了，但还没有展开「填进去之后怎么被用」。建议接下来：

- 读 [u3-l4](u3-l4-bit-policy-config.md)：看 `BitPolicy` 这类配置对象如何与 `DTYPE_REGISTRY`/`ALGO_REGISTRY` 配合，决定每个算子的位宽与算法——这是注册表「被取用」的又一个真实场景。
- 读 [u5-l2](u5-l2-model-adapters.md)：看 `MODEL_REGISTRY.get(model_name)` 取出的适配器类具体长什么样、要覆写哪些方法，从而理解「如何新增一个模型插件」。
- 读 [u6-l2](u6-l2-algo-target-routing.md)：专门展开 `ALGO_REGISTRY` 独有的 `targets` 元数据如何驱动 weight/activation/structure 三类路由——本讲刻意略过的部分在那里收口。
- 若想动手：仿照第 5 节综合实践，在本地 fork 一个分支，真的新增一个最简单的 `QuantDequantFp8` 占位类并跑通 `DTYPE_REGISTRY.get("fp8")`，把本讲的全链路在真实代码里验证一遍。
