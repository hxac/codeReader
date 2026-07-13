# array_function_dispatch 与 set_module 装饰器

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `numpy.char` 六个比较函数头上的 `@array_function_dispatch(_binary_op_dispatcher)` 装饰器到底做了什么——它是 NumPy 实现 **NEP-18** 协议（`__array_function__`）的入口，让第三方数组类型（Dask、CuPy 等）能「接管」这些函数；
- 分清两个容易混淆的角色：**dispatcher（分发器）** 与 **implementation（实现）**——前者负责「把用户传的参数翻译成『哪些数组参数需要被检查 `__array_function__`』」，后者负责「在没有第三方数组时真正干活」；
- 解释为什么 `_binary_op_dispatcher(x1, x2)` **只返回 `(x1, x2)`**——因为比较码和 rstrip 开关是写死的内部常量，不是用户传入的数组参数；
- 理解 `@set_module('numpy.char')` 如何把一个**定义在 `numpy/_core/defchararray.py`** 里的函数/类的 `__module__` 改写成 `numpy.char`，以及这对「文档归属、公开 API 身份、内省」的意义；
- 写脚本验证 `np.char.equal.__module__ == 'numpy.char'`、`np.char.multiply.__module__ == 'numpy.char'`，并解释这两条路径（`array_function_dispatch` 的 `module=` 与 `set_module`）殊途同归。

## 2. 前置知识

本讲紧接 u2-l3（比较函数与 `compare_chararrays`）。u2-l3 已经告诉你六个比较函数的函数体都只有一行 `return compare_chararrays(x1, x2, 比较码, True)`，并在「NEP-18 / `array_function_dispatch`」处留了一句「详细机制留到 u2-l4」。本讲就来兑现这个承诺，并把镜头从「比较语义」拉到「装饰器工程」。

在此基础上，还需要几个背景概念：

- **装饰器（decorator）**：Python 里 `@something` 写在 `def` 之上，等价于「先定义函数，再用 `something(函数)` 把它包一层、把返回值重新绑定回原名」。本讲里 `@array_function_dispatch(_binary_op_dispatcher)` 和 `@set_module("numpy.char")` 都是装饰器，它们都会**用一个新对象替换原函数**。
- **`functools.partial`**：把一个函数的某些参数「提前钉死」，得到一个新函数。`defchararray` 用它把通用的 `overrides.array_function_dispatch` 钉死 `module='numpy.char'`，造出一个 char 专用的装饰器工厂。
- **`functools.update_wrapper`**：把「被包装函数」的元信息（`__name__`、`__doc__`、`__module__` 等）复制到「包装器」上，让包装器「看起来」就是原函数。它在 `array_function_dispatch` 里会被调用一次，后面会看到它和 `__module__` 改写的先后顺序很关键。
- **duck-typing（鸭子类型）**：「只要一个对象长得像数组、叫得像数组，就把它当数组用」。NEP-18 的 `__array_function__` 协议就是 NumPy 给鸭子类型数组留的「官方接管口子」：如果一个非 NumPy 数组实现了 `__array_function__`，NumPy 在调用相关函数时会先问问它「这件事你自己来吗？」。
- **NEP-18**：NumPy Enhancement Proposal 第 18 号，《A dispatch mechanism for NumPy's high-level array functions》。它定义了 `__array_function__` 协议，是本讲 `array_function_dispatch` 存在的全部理由。

如果你已经清楚「装饰器就是包一层」和「`functools.partial` 就是提前钉参数」，本讲的重点就是第 4 节对 `overrides.py` 与 `_utils/__init__.py` 里两个定义的逐行精读。

## 3. 本讲源码地图

本讲盯两条「装饰」链路——一条负责**分发**，一条负责**模块归属**——把它们对着读：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|------|
| `numpy/_core/defchararray.py` | `numpy.char` 的真正实现 | `import functools` / `from numpy._core import overrides` / `from numpy._utils import set_module`（18–29 行）、char 专用的 `array_function_dispatch` 偏函数与 `_binary_op_dispatcher`（53–58 行）、比较函数上的用法（61–63 行等）、`@set_module("numpy.char")` 的多处用法（266、318、360、404、1220、1367 行） |
| `numpy/_core/overrides.py` | NEP-18 分发机制的 Python 胶水层 | `ARRAY_FUNCTIONS` 注册表（14 行）、`_ArrayFunctionDispatcher` 的 docstring（35–61 行）、`_get_implementing_args` 的 docstring（64–80 行）、`verify_matching_signatures`（86–105 行）、`array_function_dispatch` 主体（108–177 行），尤其内层 `decorator` 创建包装器、`update_wrapper`、设 `__module__`、登记 `ARRAY_FUNCTIONS`（145–175 行） |
| `numpy/_utils/__init__.py` | 不依赖 NumPy 的私有工具集 | `set_module` 的完整定义（17–38 行），含对「函数」与「类」的分别处理 |
| `numpy/char/__init__.py` | 门面转发 | u2-l1 已讲透；本讲只在「`np.char.equal` 实际来自哪里」处点一句 |

一句话定位：用户调用 `np.char.equal(x1, x2)` →（u2-l1 门面 `__getattr__`）→ `defchararray.equal` → 但这个名字被 `array_function_dispatch` 装饰过，所以实际指向一个 **`_ArrayFunctionDispatcher` 对象**（C 实现）→ 它先用 `_binary_op_dispatcher(x1, x2)` 拿到相关参数、检查 `__array_function__`、决定谁来干活 → 没人接管时就调用真正的 implementation，也就是那行 `compare_chararrays(x1, x2, '==', True)`。与此同时，这个函数的 `__module__` 被改写成 `'numpy.char'`，对外伪装成「char 模块的原住民」。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**overrides 分发（NEP-18 与 `array_function_dispatch`）**、**模块归属（`set_module` 与 `__module__` 改写）**。前者回答「这些函数凭什么能让第三方数组接管」，后者回答「定义在 `_core` 里的函数凭什么归属 `numpy.char`」。

### 4.1 overrides 分发：array_function_dispatch 与 NEP-18

#### 4.1.1 概念说明

先想一个场景：你有一个 Dask 数组 `dx`（惰性、可分布的数组），想对它做 `np.char.equal(dx, 'b')`。NumPy 自己显然不懂「怎么比较两个 Dask 数组」，那这个调用会不会直接报错？

NEP-18 给出的答案是：**不会，只要你让 Dask 数组实现一个叫 `__array_function__` 的方法**。规则很简单——NumPy 在执行任何「受协议保护的」函数之前，会先**收集**这次调用里所有「相关的数组参数」，看看其中有没有谁的类型定义了 `__array_function__`：

- 如果**有**，NumPy 就把「函数本身 + 参数」打包交给那个对象的 `__array_function__`，由它决定结果（Dask 会返回一个新的惰性 Dask 数组）；
- 如果**没有**（全是普通 NumPy 数组），NumPy 就老老实实走自己的实现。

这里有两个关键设计问题：

1. **「受协议保护的函数」是哪些？** —— 不能让所有函数都自动卷进来。NumPy 的做法是：只有显式被 `array_function_dispatch` 装饰过、并被登记进全局集合 `ARRAY_FUNCTIONS` 的函数，才算「受保护」。
2. **「相关的数组参数」是哪些？** —— 一个函数可能有十几个参数，但只有少数是「数组」（其余是字符串、整数、开关）。把判断逻辑写死不现实，于是 NumPy 让**每个函数自己提供一个 dispatcher**，专门负责回答「我这次调用里，哪些参数需要被检查 `__array_function__`」。

`array_function_dispatch` 这个装饰器，就是把上面这两件事串起来的胶水：它接收一个 dispatcher，把原函数包成一个 `_ArrayFunctionDispatcher` 对象，并登记进 `ARRAY_FUNCTIONS`。

#### 4.1.2 核心流程

`array_function_dispatch(dispatcher, module=None, ...)` 装饰一个 `implementation` 函数后，发生的事大致是：

```text
@array_function_dispatch(_binary_op_dispatcher)
def equal(x1, x2):                       # ← implementation（真正干活的）
    return compare_chararrays(x1, x2, '==', True)
```

被装饰后，`equal` 这个名字不再指向上面的 `def`，而是指向一个新对象 `public_api`，其调用流程是：

1. 用户调用 `np.char.equal(x1, y)`。
2. `public_api`（其实是 C 类 `_ArrayFunctionDispatcher` 的实例）的 `__call__` 被触发。
3. 它先调用 `dispatcher(*args, **kwargs)`，即 `_binary_op_dispatcher(x1, y)`，拿到「相关参数」`(x1, y)`。
4. 用 `_get_implementing_args((x1, y))` 过滤出「真正带 `__array_function__` 的参数」（按类型优先级排序）。
5. 若有第三方类型想接管 → 走它的 `__array_function__`；否则 → 调用 `implementation(x1, y)`，也就是那行 `compare_chararrays(...)`。
6. 返回结果。

用伪代码概括 `_ArrayFunctionDispatcher.__call__`（C 实现的语义）：

```text
def __call__(self, *args, **kwargs):
    relevant = self.dispatcher(*args, **kwargs)        # 哪些参数是数组
    types   = _get_implementing_args(relevant)         # 谁实现了 __array_function__
    for overriding in types:                           # 按优先级问一遍
        result = type(overriding).__array_function__(self, types, args, kwargs)
        if result is not NotImplemented:
            return result
    return self.implementation(*args, **kwargs)        # 没人接管，NumPy 自己来
```

这就把「dispatcher」与「implementation」的分工讲清楚了：

| 角色 | 职责 | 在 char 里的实例 |
|------|------|------------------|
| **dispatcher** | 把用户参数翻译成「待检查 `__array_function__` 的相关数组参数」；签名必须与 implementation 一致（可选参数默认值只能用 `None`） | `_binary_op_dispatcher(x1, x2)` → `(x1, x2)` |
| **implementation** | 没有第三方接管时真正干活的函数 | 比较函数体 `return compare_chararrays(...)` |

#### 4.1.3 源码精读

**① char 专用的装饰器工厂与 dispatcher。** `defchararray` 顶部先把通用的 `overrides.array_function_dispatch` 钉死 `module='numpy.char'`，再造出本地用的 `array_function_dispatch`，并定义唯一的 dispatcher：

[numpy/_core/defchararray.py:53-58](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L53-L58) —— `array_function_dispatch = functools.partial(overrides.array_function_dispatch, module='numpy.char')` 是一条偏函数：以后只要写 `@array_function_dispatch(_binary_op_dispatcher)`，等价于 `@overrides.array_function_dispatch(_binary_op_dispatcher, module='numpy.char')`。紧随其后的 `_binary_op_dispatcher(x1, x2)` 原样返回 `(x1, x2)`。

为什么 dispatcher **只返回 `(x1, x2)`**？因为 NEP-18 只关心「哪些参数是数组、可能想接管」。比较函数里真正参与运算的只有两个数组 `x1`、`x2`；至于比较码 `'=='` 和 rstrip 开关 `True`，它们是**写死在 implementation 里的内部常量**，根本不暴露给用户、也永远不会是「想接管的第三方数组」。所以 dispatcher 把它们排除在外——它返回的元组，就是「这次调用里需要被检查 `__array_function__` 的全集」。

**② 装饰器的实际用法（以 `equal` 为例）。**

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L61-L92) —— `@array_function_dispatch(_binary_op_dispatcher)` 装饰 `equal(x1, x2)`。装饰后，模块里的 `equal` 名字指向的是 `_ArrayFunctionDispatcher` 对象，**而不是**下面那个 `def equal`。第 92 行 `return compare_chararrays(x1, x2, '==', True)` 才是真正的 implementation 体。另外五个比较函数（`not_equal`/`greater_equal`/`less_equal`/`greater`/`less`）用法完全相同，只是 implementation 里的比较码不同（u2-l3 已详述）。

**③ `_ArrayFunctionDispatcher` 的语义（C 类的 docstring）。** 这个 C 类在 `overrides.py` 顶部导入，并用 `add_docstring` 给它挂了一段说明：

[numpy/_core/overrides.py:35-61](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L35-L61) —— docstring 写明：构造器要两个参数 `dispatcher`（或 `None` 表示 `like=` 分发器）与 `implementation`；调用时「所有参数必须按位置传」；它还保留 `_implementation` 属性指向原始实现。这段说明就是 4.1.2 流程图里 `__call__` 的权威依据。

**④ `_get_implementing_args` 的作用。** 它负责从 dispatcher 返回的相关参数里，挑出「真正实现了 `__array_function__`」的那些，并按优先级排序：

[numpy/_core/overrides.py:64-80](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L64-L80) —— 输入「可能像数组的参数」，输出「带 `__array_function__` 的参数序列，按应当被调用的顺序」。普通 NumPy 数组不带这个方法，所以会被滤掉；只有第三方类型（或显式定义了该协议的子类）才会留下。

**⑤ 装饰器主体：签名校验、创建包装器、登记注册表。**

[numpy/_core/overrides.py:108-144](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L108-L144) —— `array_function_dispatch(dispatcher=None, module=None, verify=True, docs_from_dispatcher=False)` 的外层签名与 docstring。注意 `module` 参数：默认会「从被装饰函数复制」，但 char 版用偏函数把它钉成了 `'numpy.char'`（见 4.2.3）。

[numpy/_core/overrides.py:145-175](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L145-L175) —— 内层 `decorator(implementation)` 才是真正干活的：先（若 `verify`）调 `verify_matching_signatures` 校验 dispatcher 与 implementation 签名一致；接着 `public_api = _ArrayFunctionDispatcher(dispatcher, implementation)` 造出包装器；`functools.update_wrapper(public_api, implementation)` 把 implementation 的 `__name__`/`__doc__`/`__module__` 等元信息复制到包装器上；最后 `if module is not None: public_api.__module__ = module` 把模块归属改写，并 `ARRAY_FUNCTIONS.add(public_api)` 把它登记进全局注册表。注意 `update_wrapper` 在前、`__module__` 改写在后——这点顺序在 4.2 会再强调。

**⑥ 签名校验：dispatcher 的纪律。** 为什么 dispatcher 必须与 implementation「签名一致」、且默认值只能用 `None`？因为 dispatcher 要能用「用户实际传的同一组参数」来调用：

[numpy/_core/overrides.py:86-105](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L86-L105) —— `verify_matching_signatures` 比较 `getargspec(implementation)` 与 `getargspec(dispatcher)` 的 `args`/`varargs`/`keywords`/`defaults`；若 dispatcher 用了非 `None` 的默认值就报错。对 `equal` 而言，`_binary_op_dispatcher(x1, x2)` 与 `equal(x1, x2)` 完全对齐，校验通过。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `np.char.equal` 已被「协议化」——它是一个 `_ArrayFunctionDispatcher`、它登记在 `ARRAY_FUNCTIONS` 里、它的 `_implementation` 才是真正调用 `compare_chararrays` 的那一段。

**操作步骤**：

```python
import numpy as np
from numpy._core.overrides import ARRAY_FUNCTIONS
from numpy._core._multiarray_umath import _ArrayFunctionDispatcher

# (1) np.char.equal 的真实类型，是分发器对象，而不是普通函数
print(type(np.char.equal))
# 预期：<class 'numpy._core._multiarray_umath._ArrayFunctionDispatcher'>

# (2) 它确实登记在 NEP-18 的受保护函数注册表里
print(np.char.equal in ARRAY_FUNCTIONS)
# 预期：True

# (3) 它保留了原始 implementation —— 即那行 compare_chararrays(...)
impl = np.char.equal._implementation
print(impl.__name__)          # 预期：equal
print(np.char.equal is impl)  # 预期：False（public_api 是包装器，不是 implementation 本身）

# (4) dispatcher 只返回 (x1, x2)：用普通 ndarray（无 __array_function__）调用，
#     结果由 implementation 产生，仍是“先剥尾部空白”的语义
x = np.array(['aa', 'b'])
print(np.char.equal(x, 'aa '))   # 预期：array([ True, False])  ← 'aa' 与 'aa ' 相等
print(np.equal(x, 'aa '))        # 预期：array([False, False])  ← 原生 numpy.equal 不剥空白
```

**需要观察的现象**：

- 第 (1) 步打印出的类型是 `_ArrayFunctionDispatcher`，说明 `np.char.equal` 不是裸函数、而是被装饰后的分发对象；
- 第 (2) 步证明它属于 NEP-18 受保护集合——这正是第三方数组能接管它的前提；
- 第 (3) 步说明 `_implementation` 指向「真正干活的原始函数」，与对外暴露的 `np.char.equal`（包装器）不是同一对象；
- 第 (4) 步对照 u2-l3：在「没有第三方接管」的普通场景下，最终落到 implementation，于是表现出「剥尾部空白」。

**预期结果**：如上注释所示。第 (1)~(3) 步若想观察第三方接管，可自行构造一个定义了 `__array_function__` 的子类（属进阶练习，见 4.1.5 第 3 题）。

> 说明：上述断言基于本讲引用的源码（`_ArrayFunctionDispatcher` 的 docstring 写明它保留 `_implementation`）。若你所用 NumPy 版本与当前 HEAD（`4e7f3b33`）差异较大，第 (1)~(3) 步的具体类型名可能不同，请以本地实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_binary_op_dispatcher` 只返回 `(x1, x2)`，而不把比较码 `'=='` 也放进去？

**参考答案**：dispatcher 的职责是「挑出本次调用里**可能想接管 `__array_function__`** 的数组参数」。比较码 `'=='` 和 rstrip 开关 `True` 是写死在 implementation 里的**内部常量**，既不是用户传入的参数，也永远不可能是一个「实现了 `__array_function__` 的第三方数组」。把它们塞进 dispatcher 既无意义，也会让 NEP-18 去检查一个根本不是数组的字符串，纯属浪费。

**练习 2**：`verify_matching_signatures` 为什么要求「dispatcher 的默认值只能是 `None`」？

**参考答案**：dispatcher 要能被「与 implementation 相同的实参」调用。若 dispatcher 自己带了非 `None` 的默认值，会和 implementation 的默认值产生歧义；统一要求 dispatcher 的可选参数默认值必须是 `None`，是为了让校验逻辑简单且无歧义——它只关心「参数的名字和顺序对得上」，不关心「业务默认值」。`_binary_op_dispatcher(x1, x2)` 没有默认参数，自然满足。

**练习 3**：写一个最小子类 `MyArr(np.ndarray)`，给它加上 `__array_function__ = ...`，调用 `np.char.equal(MyArr实例, 'aa ')`，观察会发生什么。

**参考答案（思路）**：定义 `__array_function__(cls, func, types, args, kwargs)`，在其中打印 `func`（你会看到它就是 `np.char.equal`）和 `types`，然后 `return NotImplemented`（表示「我不接管」，让 NumPy 回退到 implementation）或返回自定义结果。你会观察到：因为有 `__array_function__`，`_get_implementing_args` 不会滤掉 `MyArr`，分发器先去问它；返回 `NotImplemented` 后 NumPy 才回退到 implementation。这一题验证了 4.1.2 流程图里的「优先问接管者」分支。完整实现待本地验证。

---

### 4.2 模块归属：set_module 与 __module__ 改写

#### 4.2.1 概念说明

现在回答第二个问题：`multiply`、`partition`、`chararray`、`array`、`asarray` 这些名字，明明**定义在 `numpy/_core/defchararray.py`** 里，为什么 `np.char.multiply.__module__` 却显示成 `'numpy.char'`？

这要从 Python 的 `__module__` 说起。每个函数/类都有一个 `__module__` 属性，记录「它是在哪个模块里被定义的」。默认情况下，它就是你 `def`/`class` 所在那个 `.py` 文件的模块名——所以「裸」的 `multiply`，`__module__` 本应是 `numpy._core.defchararray`。

但 NumPy 想让这些函数**对外表现为 `numpy.char` 的成员**，原因有三：

1. **文档归属**：NumPy 用 Sphinx 自动生成文档，按 `__module__` 把函数归类到对应模块页。如果 `__module__` 是 `numpy._core.defchararray`，文档里就会冒出一个「内部模块」`_core.defchararray`，既难看也暴露实现细节。改成 `numpy.char` 后，函数就堂堂正正出现在 `numpy.char` 的文档页。
2. **公开 API 身份**：用户从 `numpy.char` 拿到这些函数（经 u2-l1 的门面转发），`__module__` 反映成 `numpy.char` 才符合「我看到它属于哪」的直觉，也让 `pydoc`、IDE 的「跳转/提示」把它定位到正确的公开命名空间。
3. **与 `_core` 解耦的心智模型**：`_core` 是「实现层」、`numpy.char` 是「公开层」。把 `__module__` 统一指向公开层，等于在元信息层面落实了 u1-l1 讲的「门面 → 实现」分层。

NumPy 提供了一个专用小工具来做这件事，就是 `set_module`。它被 `defchararray` 直接从 `numpy._utils` 导入：

[numpy/_core/defchararray.py:29](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L29) —— `from numpy._utils import set_module`。

需要特别澄清的一点：**`__module__` 的改写与 NEP-18 分发是两件独立的事**。分发能不能生效，取决于「函数是否被 `array_function_dispatch` 包装并登记进 `ARRAY_FUNCTIONS`」；而 `__module__` 只影响文档/内省/归属展示，不参与 `__array_function__` 的判定。本讲把它们放在同一篇，是因为 char 里这两条路径**目的相同**（让 `_core` 里的东西看起来像 `numpy.char` 的公开 API），但机制互不依赖。

另外注意 `overrides.py` 也**顺手再导出**了 `set_module`：

[numpy/_core/overrides.py:11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L11) —— `from numpy._utils import set_module  # noqa: F401`。`# noqa: F401` 表示「虽然本文件没直接用它，但故意再导出，请 linter 不要报『未使用』」。这是 NumPy 把 `set_module` 当作 `overrides` 模块公共表面的一部分提供给其他内部模块的约定。

#### 4.2.2 核心流程

`set_module` 的逻辑极简，但它要兼容「函数」和「类」两种被装饰对象：

```text
set_module("numpy.char")  →  返回 decorator  →  decorator(func) 把 func.__module__ 改写后原样返回 func
```

要点：

- 它**不创建包装器**（不像 `array_function_dispatch` 那样返回 `_ArrayFunctionDispatcher`），而是**就地修改** `func.__module__` 后把原对象返回——所以被 `@set_module` 装饰的函数，身份不变，只是换了「户口」。
- 对**类**（`isinstance(func, type)`）会额外保存一份 `_module_source`（记录改写前的真实出处），便于追溯；对普通函数则只改 `__module__`。
- 若传入 `module is None`，则什么都不做（在类型存根 `.pyi` 里 `set_module(None)` 被声明成恒等函数）。

#### 4.2.3 源码精读

**① `set_module` 的完整定义。**

[numpy/_utils/__init__.py:17-38](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_utils/__init__.py#L17-L38) —— `set_module(module)` 内层 `decorator(func)`：若 `module is not None`，先判断 `isinstance(func, type)`，是类就把原 `func.__module__` 存进 `func._module_source`（用 `try/except AttributeError` 兜底），然后 `func.__module__ = module`，最后 `return func`。注意它住在 `numpy._utils`——一个「不依赖 NumPy 其余部分、可被任意地方导入而不致循环引用」的工具集（见该文件顶部 docstring），这正是它能被 `_core` 之类底层模块安全导入的原因。

**② `defchararray` 里 `@set_module("numpy.char")` 的多处用法。** 这些函数/类都定义在 `_core`，但对外归属 `numpy.char`：

[numpy/_core/defchararray.py:266-267](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L267) —— `@set_module("numpy.char")` 装饰 `multiply`（u1-l3 讲过它是 `strings_multiply` 的薄包装）。

[numpy/_core/defchararray.py:318](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L318) 和 [numpy/_core/defchararray.py:360](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L360) —— 同样装饰 `partition`、`rpartition`。

[numpy/_core/defchararray.py:404-405](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L404-L405) —— 装饰 **类** `chararray(ndarray)`。因为它是类，`set_module` 会顺带把 `chararray._module_source` 存为原 `'numpy._core.defchararray'`。

[numpy/_core/defchararray.py:1220-1221](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1220-L1221) 与 [numpy/_core/defchararray.py:1367-1368](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1367-L1368) —— 装饰工厂函数 `array` 与 `asarray`。

**③ 比较函数的 `__module__` 是谁来改的？** 注意：六个比较函数头上的装饰器是 `@array_function_dispatch(...)`，**不是** `@set_module`。它们的 `__module__` 是在 `array_function_dispatch` 内部、通过 `module=` 参数改写的：

[numpy/_core/defchararray.py:53-54](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L53-L54) —— char 版的 `array_function_dispatch` 偏函数把 `module='numpy.char'` 钉死。

[numpy/_core/overrides.py:164-171](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/overrides.py#L164-L171) —— 这里能看到关键的**先后顺序**：第 165 行 `functools.update_wrapper(public_api, implementation)` 先把 implementation 的 `__module__`（=`numpy._core.defchararray`）复制到包装器；第 170–171 行 `if module is not None: public_api.__module__ = module` 紧接着把它**覆盖**成 `'numpy.char'`。所以比较函数虽然没用 `@set_module`，最终 `__module__` 仍是 `numpy.char`——`update_wrapper` 在前、`module=` 覆盖在后，缺一不可。

**两条路径对比**：

| 路径 | 用于谁 | 谁改 `__module__` | 是否创建包装器 |
|------|--------|-------------------|----------------|
| `@set_module("numpy.char")` | `multiply`/`partition`/`rpartition`/`chararray`/`array`/`asarray` | `set_module` 的 `decorator`（就地改） | 否（返回原对象） |
| `@array_function_dispatch(...)` + 偏函数 `module='numpy.char'` | 六个比较函数 | `array_function_dispatch` 内层 `decorator`（覆盖） | 是（`_ArrayFunctionDispatcher`） |

殊途同归：两类函数的 `__module__` 最终都是 `'numpy.char'`。

#### 4.2.4 代码实践

**实践目标**：验证两类函数的 `__module__` 都被改写成 `'numpy.char'`，并对照 `set_module` 源码确认「类会额外保存 `_module_source`」。

**操作步骤**：

```python
import numpy as np

# (1) 比较函数：走 array_function_dispatch 的 module= 路径
print(np.char.equal.__module__)        # 预期：'numpy.char'
print(np.char.not_equal.__module__)    # 预期：'numpy.char'

# (2) 被 @set_module 直接装饰的函数
print(np.char.multiply.__module__)     # 预期：'numpy.char'
print(np.char.partition.__module__)    # 预期：'numpy.char'

# (3) 被 @set_module 装饰的类：额外带有 _module_source（记录改写前的真实出处）
print(np.char.chararray.__module__)    # 预期：'numpy.char'
print(getattr(np.char.chararray, '_module_source', '<无>'))
# 预期：'numpy._core.defchararray'（见 set_module 对类的处理）

# (4) 对照：这些对象「真正定义」在哪个文件
print(np.char.equal.__module__, '<- 公开归属；真实出处是 numpy._core.defchararray')
```

**需要观察的现象**：

- 第 (1)、(2) 步四行都打印 `'numpy.char'`，证明两条路径（`module=` 与 `set_module`）殊途同归；
- 第 (3) 步 `chararray.__module__` 是 `'numpy.char'`，但 `_module_source` 仍保留 `'numpy._core.defchararray'`——这正是 `set_module` 对「类」做的额外留痕（函数没有这个属性）；
- 第 (4) 步提醒：`__module__` 是「对外归属」，不等于「物理定义位置」。

**预期结果**：如注释所示。第 (3) 步的 `_module_source` 依赖 `set_module` 源码里 `func._module_source = func.__module__` 那段逻辑（对类生效）；若未来 `set_module` 改动，此属性可能变化（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：六个比较函数头上写的是 `@array_function_dispatch(...)` 而不是 `@set_module(...)`，为什么它们的 `__module__` 仍然是 `'numpy.char'`？

**参考答案**：因为 char 版的 `array_function_dispatch` 是一条偏函数，把 `module='numpy.char'` 钉死了。在 `array_function_dispatch` 的内层 `decorator` 里，`functools.update_wrapper` 先把 implementation 的 `__module__` 复制到包装器，紧接着 `if module is not None: public_api.__module__ = module` 把它覆盖成 `'numpy.char'`。所以比较函数虽然没直接用 `set_module`，`__module__` 照样被改成 `numpy.char`。

**练习 2**：`set_module` 对「函数」和「类」的处理有什么不同？为什么对类要多做一步？

**参考答案**：对两者都执行 `func.__module__ = module`；但对**类**（`isinstance(func, type)` 为真）会先把原始 `func.__module__` 存到 `func._module_source`（带 `try/except AttributeError` 兜底），函数则不存。原因是类一旦改了 `__module__`，文档/序列化/调试里就很难再追溯它「真正来自哪个模块」，多留一个 `_module_source` 便于反查；函数相对轻量，且 `functools.update_wrapper` 之类的机制一般不涉及函数的「出身追溯」，故省略。

**练习 3**：`__module__` 被改成 `numpy.char`，会影响 NEP-18 的 `__array_function__` 分发吗？

**参考答案**：**不会**。分发是否生效，取决于「函数是否被 `array_function_dispatch` 包装并登记进 `ARRAY_FUNCTIONS`」以及「参数里有没有带 `__array_function__` 的对象」。`__module__` 只影响文档归类、`pydoc`/IDE 内省、错误信息里的模块名展示，与分发判定无关。本讲把二者并列，是因为它们共同塑造了「这些函数属于 `numpy.char` 公开 API」的对外形象，而非因为它们在机制上互相依赖。

---

## 5. 综合实践

把本讲两条主线串成一个任务：**给 `numpy.char` 写一份「装饰器体检报告」**。

**任务**：对下列对象逐一检查并填表——真实类型、`__module__`、是否在 `ARRAY_FUNCTIONS` 中、改写 `__module__` 的是哪条路径。

对象清单：`np.char.equal`、`np.char.less`、`np.char.multiply`、`np.char.partition`、`np.char.chararray`、`np.char.array`。

**操作步骤**：

```python
import numpy as np
from numpy._core.overrides import ARRAY_FUNCTIONS
from numpy._core._multiarray_umath import _ArrayFunctionDispatcher

objs = {
    'equal':     np.char.equal,
    'less':      np.char.less,
    'multiply':  np.char.multiply,
    'partition': np.char.partition,
    'chararray': np.char.chararray,
    'array':     np.char.array,
}

for name, obj in objs.items():
    is_disp = isinstance(obj, _ArrayFunctionDispatcher)
    print(f"{name:10} type={type(obj).__name__:28} "
          f"__module__={obj.__module__:14} "
          f"in_ARRAY_FUNCTIONS={obj in ARRAY_FUNCTIONS} "
          f"is_dispatcher={is_disp}")
```

**预期结果（基于当前 HEAD 源码）**：

| 对象 | 真实类型 | `__module__` | 在 `ARRAY_FUNCTIONS` 中 | 改写路径 |
|------|----------|--------------|--------------------------|----------|
| `equal` | `_ArrayFunctionDispatcher` | `numpy.char` | True | `array_function_dispatch(module=)` |
| `less` | `_ArrayFunctionDispatcher` | `numpy.char` | True | `array_function_dispatch(module=)` |
| `multiply` | `function` | `numpy.char` | False | `set_module` |
| `partition` | `function` | `numpy.char` | False | `set_module` |
| `chararray` | `type` | `numpy.char` | False | `set_module`（额外留 `_module_source`） |
| `array` | `function` | `numpy.char` | False | `set_module` |

**需要观察的现象与思考**：

1. 只有 `equal` / `less`（及另外四个比较函数）是 `_ArrayFunctionDispatcher` 且登记在 `ARRAY_FUNCTIONS`——即只有它们享有 NEP-18 分发能力；`multiply` 等是普通函数，**不**参与 `__array_function__` 分发。
2. 尽管机制不同，**六个对象的 `__module__` 全是 `numpy.char`**——这正是「门面归一化」的效果：无论内部走哪条装饰路径，对外都伪装成 char 原住民。
3. 用一句话总结 dispatcher 与 implementation 的分工：**dispatcher 决定「谁有机会接管」（翻译相关数组参数），implementation 决定「没人接管时怎么算」（真正的算法体）**；`set_module` / `module=` 则独立地决定「这个函数挂靠在哪个公开模块名下」。

> 若你的本地 NumPy 与当前 HEAD（`4e7f3b33`）不同，`type(obj).__name__` 的具体字符串、`_module_source` 是否存在等可能略有差异；以本地实际输出为准（待本地验证）。

## 6. 本讲小结

- `@array_function_dispatch(_binary_op_dispatcher)` 是 NumPy 落实 **NEP-18** 的入口：它把比较函数包成一个 C 类 `_ArrayFunctionDispatcher`，并登记进全局集合 `ARRAY_FUNCTIONS`，使第三方数组（带 `__array_function__`）能接管这些函数。
- **dispatcher** 与 **implementation** 分工明确：dispatcher（`_binary_op_dispatcher`）只返回 `(x1, x2)`——即「需要被检查 `__array_function__` 的相关数组参数」；implementation（`return compare_chararrays(...)`）才是「没人接管时」的真正算法体。
- `_binary_op_dispatcher` 之所以只返回 `(x1, x2)`，是因为比较码和 rstrip 开关是写死的内部常量，不是用户传入的、可能想接管的数组参数。
- `array_function_dispatch` 用 `verify_matching_signatures` 强制 dispatcher 与 implementation 签名一致（dispatcher 默认值只能为 `None`）；包装时 `functools.update_wrapper` 在前、`__module__` 覆盖在后、`ARRAY_FUNCTIONS.add` 收尾。
- `@set_module("numpy.char")` 是另一条独立路径：**就地**把函数/类的 `__module__` 改写成 `'numpy.char'`（对类额外保存 `_module_source`），用于文档归属与公开 API 身份，不创建包装器、不参与分发判定。
- 两条路径殊途同归：六个比较函数走 `module=`、`multiply`/`partition`/`rpartition`/`chararray`/`array`/`asarray` 走 `set_module`，最终 `__module__` 都是 `numpy.char`——共同把「定义在 `_core`」的实现伪装成「`numpy.char` 的公开成员」。

## 7. 下一步学习建议

- 接下来建议阅读 **u2-l5（multiply / partition / rpartition 本地包装）**，看 `@set_module("numpy.char")` 装饰的这三个本地函数，如何分别在 `numpy.strings` 对应函数之上做「错误类型转换（TypeError→ValueError）」与「结果重组（`np.stack` 增维）」，把本讲建立的「本地定义 vs 再导出」落到具体函数体上。
- 若你想把 NEP-18 这条线吃透，可以跳到 NumPy 官方文档的 [NEP-18](https://numpy.org/neps/nep-0018-array-function-protocol.html) 原文，并对照 `numpy/_core/overrides.py` 通读 `array_function_dispatch`、`array_function_from_dispatcher`、`ARRAY_FUNCTIONS` 的全貌。
- 若你对「子类化与运算符」更感兴趣，可直接进入 **u3-l1（chararray 的 ndarray 子类化机制）** 与 **u3-l2（运算符重载与方法委托）**——那里会用到本讲的结论：`chararray` 的六个比较运算符直接委托给这几个被 `array_function_dispatch` 包装的自由函数。
- 进阶练习：构造一个带 `__array_function__` 的 `ndarray` 子类，验证它能接管 `np.char.equal`（在 `ARRAY_FUNCTIONS` 中）却无法接管 `np.char.multiply`（不在其中），亲手感受「登记进 `ARRAY_FUNCTIONS`」这一资格的实际效果。
