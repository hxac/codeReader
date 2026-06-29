# uarray 多方法与后端分发机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「多方法（multimethod）」与「后端（backend）」这两个核心概念，理解为什么同一个函数名可以对应多种不同实现。
- 读懂 SciPy 内嵌的 `uarray` 协议：一个后端需要提供哪些属性/方法（`__ua_domain__`、`__ua_function__`、可选的 `__ua_convert__`），多方法又是如何用 `generate_multimethod` 生成的。
- 掌握三种激活后端的方式（`set_backend` / `set_global_backend` / `register_backend`）以及 `skip_backend`，并理解后端之间的优先级与 `only`/`coerce` 短路逻辑。
- 看懂 `scipy.fft` 是如何把这些机制落地到真实代码的：`_basic.py` 生成多方法、`_backend.py` 的 `_ScipyBackend` 兜底、`set_global_backend('scipy', try_last=True)` 设定默认。
- 分辨清楚：`scipy.fft` 里哪些函数是「会被分发」的多方法，哪些只是用了上一讲（u2-l1）的 `array_namespace` 的普通函数。

## 2. 前置知识

本讲默认你已经读过 **u2-l1**，知道 `_lib/_util.py` 是 SciPy 的私有共享工具箱，并接触过 `array_namespace`、`xp`（数组命名空间）、Array API 这些术语。这里先用最通俗的话补两个本讲专属的概念。

### 2.1 什么是「分发（dispatch）」

设想你拨打一个客服号码「10086」，接线员会根据当前值班情况，把你的电话转到不同部门。`scipy.fft.fft(x)` 就像这个号码：**函数名只有一个，但真正干活的代码可以有很多份**——可能是 SciPy 自带的快速傅里叶实现（pocketfft/ducc），也可能是 NumPy 的实现、CuPy 的 GPU 实现、PyTorch 的实现……到底用哪一份，取决于「当前值班的后端」是谁。把「根据当前活跃的后端，决定调用哪一份实现」这件事，就叫做**分发**。

### 2.2 两种分发风格（重要对比）

SciPy 里其实有两套不同的「按数组类型选实现」的机制，本讲讲的是第一种，第二种是下一讲 **u2-l3（Array API）**的主题，先把它们区分开，避免混淆：

| 机制 | 谁来决定用哪份实现 | 典型入口 | 本讲/后续 |
| --- | --- | --- | --- |
| **uarray 多方法**（本讲） | 用户用 `set_backend` 等**显式声明**当前后端 | `scipy.fft.fft` | 本讲 |
| **Array API**（u2-l1 已铺垫） | 代码用 `array_namespace(x)` **看输入数组本身**的类型来选 | `scipy.fft.fftshift` | u2-l3 详讲 |

一句话区别：uarray 是「**用户选后端**」，Array API 是「**代码看数组**」。两者目的相近（都为了支持 NumPy 以外的数组，如 CuPy/Torch/JAX），但实现哲学不同。本讲聚焦 uarray。

### 2.3 一个术语：domain（域）

uarray 用一个**点分字符串**来标识一组多方法所属的「域」，例如 `"numpy.scipy.fft"`。域是**层级结构**：一个声明了实现 `"numpy"` 域的后端，原则上也能覆盖 `"numpy.scipy.fft"`。这一点会直接影响后面 `_ScipyBackend` 为什么把域写成 `"numpy.scipy.fft"` 而不是 `"scipy.fft"`。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为真实文件，按「由抽象到具体」排列）：

| 文件 | 角色 |
| --- | --- |
| [scipy/_lib/uarray.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/uarray.py) | 极薄的「垫片」：优先用系统已装的 `uarray`（≥0.8），否则回退到 SciPy 内嵌副本。 |
| [scipy/_lib/_uarray/__init__.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py) | 内嵌 uarray 包的入口，含一份讲解后端协议的完整文档字符串。 |
| [scipy/_lib/_uarray/_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py) | uarray 的 **Python 层 API**：`generate_multimethod`、`Dispatchable`、`set_backend` 等。 |
| [scipy/_lib/_uarray/_uarray_dispatch.cxx](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx) | uarray 的 **C++ 分发核心**：多方法被调用时遍历后端、处理 `NotImplemented` 回退的循环就在这里。 |
| [scipy/fft/_basic.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic.py) | 用 `generate_multimethod` 把 `fft`/`ifft`/… 注册成多方法的地方。 |
| [scipy/fft/_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py) | `_ScipyBackend`（默认后端）+ `set_backend`/`register_backend` 等对 uarray 的封装。 |
| [scipy/fft/_basic_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic_backend.py) | `_ScipyBackend` 真正调用的「SciPy 自有 FFT 实现」（duccfft / `xp.fft`）。 |
| [scipy/fft/_helper.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py) | `fftfreq`/`fftshift` 等**普通函数**（不走 uarray），作为「边界对照」。 |
| [scipy/fft/tests/mock_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/mock_backend.py) | 一个完整的「自定义后端」范例，本讲实践以它为蓝本。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：① 多方法与后端协议；② 后端的注册/切换/跳过与优先级；③ `scipy.fft` 的具体分发实现；④ 多方法的边界（哪些函数被分发、哪些不是）。

### 4.1 多方法（multimethod）与后端（backend）协议

#### 4.1.1 概念说明

uarray 的世界里有两种角色，它们**相互独立**，你可以只懂一种：

- **API 设计者**：写「多方法」。多方法只是一个**函数名占位**，它声明「我接受这些参数，其中某某参数是需要分发的数组」，但**不真正实现计算**。
- **实现者**：写「后端」。后端是一个对象或模块，声明「我属于哪个域（`__ua_domain__`），并且当某个多方法被调用时，我知道该怎么算（`__ua_function__`）」。

这就好比：API 设计者定义了「插座标准」，实现者造出「各种插头」。同一个插座可以插不同插头，具体通电方式由插头决定。

一个**后端**需要满足的协议（属性/方法）是：

| 成员 | 必需 | 作用 |
| --- | --- | --- |
| `__ua_domain__` | 是 | 一个点分字符串，声明本后端服务的域，如 `"numpy.scipy.fft"`。 |
| `__ua_function__(method, args, kwargs)` | 是 | 真正的计算。返回结果，或返回 `NotImplemented` 表示「我处理不了，请找下一个后端」。 |
| `__ua_convert__(dispatchables, coerce)` | 否 | 类型转换钩子。若省略，参数原样传给 `__ua_function__`，由后端自行处理类型。 |

`NotImplemented` 是整个分发的关键：后端可以「礼貌地拒绝」，于是分发器会继续尝试下一个后端。这正是回退（fallback）机制的基础。

#### 4.1.2 核心流程

生成一个多方法靠 `generate_multimethod`，它需要四个输入：

1. **argument_extractor（参数提取器）**：一个与你想要的多方法**签名相同**的函数，返回一个由 `Dispatchable(...)` 标记组成的元组，标记出「哪些参数是要参与分发的数组」。
2. **argument_replacer（参数替换器）**：签名 `(args, kwargs, dispatchables) -> (args, kwargs)`，把已经「替换好的数组」塞回参数里。它的存在是为了支持**类型强制转换**：比如某后端把 CPU 数组拷到 GPU，那么真正调用时传进去的就得是转换后的数组。
3. **domain（域）**：点分字符串。
4. **default（默认实现，可选）**：当没有任何后端处理时使用的兜底实现；`None` 表示没有兜底（此时会抛 `BackendNotImplementedError`）。

当用户调用这个多方法时，分发器（C++ 实现）大致按这样的伪代码工作：

```
function 调用多方法(method, args, kwargs):
    for backend in 按优先级排序的活跃后端:
        result = backend.__ua_function__(method, args, kwargs)
        if result 不是 NotImplemented:
            return result
        # 否则（拒绝或抛 BackendNotImplementedError）继续下一个
    if default 存在:
        return default(*args, **kwargs)
    raise BackendNotImplementedError   # 全都拒绝
```

> 注意：上面是「概念伪代码」，真实循环在 C++ 里（见 4.2.3），但它完整反映了「遍历 → 拒绝就跳过 → 全拒则报错」的语义。

#### 4.1.3 源码精读

**① 垫片层：优先用系统已装的 uarray。** [scipy/_lib/uarray.py:11-31](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/uarray.py#L11-L31) 先 `try: import uarray`，若版本 ≥ 0.8 就用系统的；否则回退到 SciPy 自己内嵌的副本 `from ._uarray import *`（第 28 行）。这样做的好处是：用户若装了独立 uarray，可以直接 `uarray.set_backend(...)` 而不必经过 SciPy。

**② 内嵌副本与协议文档。** [scipy/_lib/_uarray/__init__.py:115-116](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py#L115-L116) 把 `_backend` 全部导出，并写死一个带 `.scipy` 后缀的版本号。这个文件的**文档字符串**（约 26–112 行）是用 `doctest` 写的完整教学示例，明确给出了协议签名：

- `__ua_function__` 的签名是 `(method, args, kwargs)`（[L52-L60](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py#L52-L60)）。
- `__ua_convert__` 的签名是 `(dispatchables, coerce)`，并说明 `coerce=False` 时转换最好是 O(1) 的视图操作（[L62-L74](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py#L62-L74)）。

**③ 生成多方法的真身。** [scipy/_lib/_uarray/_backend.py:174-249](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L174-L249) 定义 `generate_multimethod`。核心只有两步（第 239–249 行）：

```python
kw_defaults, arg_defaults, opts = get_defaults(argument_extractor)   # L239 解析默认值
ua_func = _Function(                                                  # L240 构造 C++ 多方法对象
    argument_extractor, argument_replacer, domain,
    arg_defaults, kw_defaults, default,
)
return functools.update_wrapper(ua_func, argument_extractor)          # L249 拷贝名字/文档/签名
```

`_Function` 是来自 C++ 模块的真实类型（被调用时执行分发循环）。`update_wrapper` 只是把原函数的 `__name__`/`__doc__`/`__wrapped__` 拷过去，**不改变对象类型**——所以多方法对象的类型仍是 `_Function`。

**④ Dispatchable 标记。** [scipy/_lib/_uarray/_backend.py:412-451](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L412-L451) 定义 `Dispatchable(value, dispatch_type, coercible=True)`，它只是把「值 + 期望分发的类型 + 是否可强制转换」打包在一起。配套的 `mark_as`（[L454-L464](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L454-L464)）和 `all_of_type`（[L467-L494](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L467-L494)）是两个便捷工厂，用来批量给参数打标记。

#### 4.1.4 代码实践

下面这个最小例子**直接取自 `__init__.py` 的文档字符串**（是最权威的入门示例），我们在解释器里复现它，亲眼看到「拒绝 → 回退 → 报错」的全过程。

```python
# 示例代码：取自 scipy/_lib/_uarray/__init__.py 文档字符串的 doctest
import scipy._lib.uarray as ua
from scipy._lib.uarray import generate_multimethod, Dispatchable

def override_me(a, b):
    return Dispatchable(a, int),          # 标记 a 是要分发的 int，注意末尾逗号（元组）

def override_replacer(args, kwargs, dispatchables):
    return (dispatchables[0], args[1]), {}

# 生成一个域为 "ua_examples" 的多方法（无 default）
overridden_me = generate_multimethod(override_me, override_replacer, "ua_examples")

# 情况一：没有任何后端 -> 报错
try:
    overridden_me(1, "a")
except ua.BackendNotImplementedError as e:
    print("无后端时：", type(e).__name__)

# 情况二：注册一个“只会拒绝”的后端 -> 仍然报错
class RefuseBackend:
    __ua_domain__ = "ua_examples"
    @staticmethod
    def __ua_function__(method, args, kwargs):
        return NotImplemented

with ua.set_backend(RefuseBackend()):
    try:
        overridden_me(1, "a")
    except ua.BackendNotImplementedError as e:
        print("后端拒绝时：", type(e).__name__)
```

操作步骤：把上面代码存为 `demo_uarray.py`，`python demo_uarray.py` 运行。

需要观察的现象：两次调用都会进入 `except` 分支。

预期结果：

```
无后端时： BackendNotImplementedError
后端拒绝时： BackendNotImplementedError
```

> 说明：情况二里 `set_backend` 设置了后端，但后端的 `__ua_function__` 返回 `NotImplemented`，分发器遍历完所有后端都没人能处理，又没有 `default`，于是抛 `BackendNotImplementedError`——这正是 4.1.2 伪代码里「全拒则报错」的体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 `generate_multimethod` 加上 `default=lambda x, y: (x, y)`，`overridden_me(1, "a")`（后端拒绝时）会返回什么？

> **答案**：返回 `(1, 'a')`。因为所有后端都拒绝后，分发器会回退到 `default` 实现（见 [\_backend.py:227-L231](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L227-L231) 的 doctest）。

**练习 2**：参数提取器 `override_me` 返回的为什么是 `Dispatchable(a, int),`（带末尾逗号）而不是 `Dispatchable(a, int)`？

> **答案**：因为提取器必须返回一个**可迭代对象**（多个 dispatchable 的序列）。`Dispatchable(a, int),` 是单元素元组；少了逗号就只是一个 `Dispatchable` 实例，遍历时会出错。源码注释也专门强调了「The trailing comma is needed」（[L202-L203](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L202-L203)）。

---

### 4.2 后端的注册、切换、跳过与优先级

#### 4.2.1 概念说明

一个后端写好后，还要「激活」它才能被分发器看到。uarray 提供了**四种激活/排除方式**，按「作用范围」划分：

| API | 形态 | 作用范围 | 优先级 |
| --- | --- | --- | --- |
| `set_backend(be)` | `with` 上下文管理器 | **临时**，仅 `with` 块内，退出即恢复 | **最高**，且后入的更优先（栈式） |
| `set_global_backend(be)` | 普通调用 | **全局永久**，整个进程 | 中（可用 `try_last` 调到最低） |
| `register_backend(be)` | 普通调用 | **全局永久**，但优先级最低 | 低（在 global 之后才被尝试） |
| `skip_backend(be)` | `with` 上下文管理器 | **临时**黑名单，块内跳过该后端 | —— |

此外有两个**标志位**会改变遍历行为：

- `only=True`：如果当前后端返回 `NotImplemented`，**立刻停止**，不再尝试更低优先级的后端。
- `coerce=True`：允许「昂贵的类型转换」（例如把 NumPy 数组拷到 GPU），它**隐含** `only=True`。

#### 4.2.2 核心流程

当多方法被调用时，后端的遍历顺序（从高到低）大致是：

```
1. 栈式局部后端：由内到外的 set_backend（with 嵌套，最内层最先试）
2. 全局后端（若未设 try_last）
3. register_backend 注册的后端
4. 全局后端（若设了 try_last，则放到最后兜底）
其间：被 skip_backend 命中的后端一律跳过。
短路：遇到 only/coerce 的后端拒绝时，立即停止并报错。
```

关于 `try_last` 的妙用：它让「全局后端」从「优先」变成「兜底」。这正是 `scipy.fft` 默认配置的关键——下一节会看到 `set_global_backend('scipy', try_last=True)`，意思是：SciPy 自带实现永远是最后的退路，而用户注册的 CuPy/Torch 后端可以优先接管。

#### 4.2.3 源码精读

**① set_backend：上下文管理器 + 每线程缓存。** [scipy/_lib/_uarray/_backend.py:252-280](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L252-L280) 用 `threading.get_native_id()` 做键缓存上下文对象（第 270、279 行），这样同一线程内重复 `set_backend(同一个 be)` 不会反复构造，对多线程友好。它返回 `_SetBackendContext`（C++ 类型），所以能用在 `with` 里。

**② set_global_backend：带 try_last。** [scipy/_lib/_uarray/_backend.py:330-362](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L330-L362) 的签名是 `set_global_backend(backend, coerce=False, only=False, *, try_last=False)`，注意 `try_last` 是**仅关键字参数**。文档明确警告「not thread-safe」「不建议库作者调用，只应由终端用户/参考实现调用」（[L337-L343](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L337-L343)）。

**③ register_backend / skip_backend。** 分别见 [\_backend.py:365-378](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L365-L378) 与 [\_backend.py:283-309](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L283-L309)。`register_backend` 没有 `only/coerce/try_last`，它就是「低优先级常驻」。

**④ C++ 分发循环（真实遍历逻辑）。** [scipy/_lib/_uarray/_uarray_dispatch.cxx](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx) 里，多方法被调用时的核心是 [L1304-L1306](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1304-L1306) 的 `for_each_backend(domain_key_, ...)`，它逐个后端调用 `backend` 的 `__ua_function__`（`__ua_function__` 这个字符串在第 182 行被 intern 缓存以加速属性查找）。当某次调用抛出 `BackendNotImplementedError`（[L1321](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1321)、[L1349](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1349) 用 `PyErr_ExceptionMatches` 判定），就继续下一个；若遍历完仍无结果，则在 [L1403](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1403) 抛 `BackendNotImplementedError`。`only`/`coerce` 的「立即停止」语义体现在 [L998-L1002](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L998-L1002)（局部）和 [L1026](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1026)（全局）。

**⑤ 可序列化：为多进程/并行准备。** [scipy/_lib/_uarray/_backend.py:99-102](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L99-L102) 用 `copyreg.pickle` 为 `_Function`、`_BackendState`、两个 Context 注册了 pickle 支持。这解释了为什么多线程测试（`scipy/fft/tests/test_multithreading.py`）能跑通——后端状态可被保存/恢复。

#### 4.2.4 代码实践

用 `scipy.fft` 自带的 `set_backend` 来体会 `only` 的「立即停止」效果，以及 `skip_backend` 的黑名单效果。

```python
# 示例代码：体会 only 与 skip
import scipy.fft as sfft
import numpy as np

x = np.arange(8)

# (A) 正常调用：走默认 scipy 后端
print("默认:", sfft.fft(x)[:3])

# (B) skip 掉 scipy 后端，且没有别的后端 -> 报错
try:
    with sfft.skip_backend('scipy'):
        sfft.fft(x)
except sfft._backend.BackendNotImplementedError if False else Exception as e:
    print("skip scipy 后:", type(e).__name__)
```

操作步骤：运行上述脚本。

需要观察的现象：(B) 中跳过了唯一的后端，于是没有实现可用。

预期结果：

```
默认: [28.+0.j -4.+8.j -4.+3.32...j]
skip scipy 后: BackendNotImplementedError
```

> 说明：`skip_backend('scipy')` 把字符串 `'scipy'` 映射到 `_ScipyBackend`（见 4.3.3），在 `with` 块内它被排除，又没有其他后端，于是分发器遍历一空，抛 `BackendNotImplementedError`。这与 `_backend.py` 中 `skip_backend` 文档字符串（[L198-L205](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L198-L205)）的 doctest 完全一致。报错信息里出现的异常类名以本地实际为准。

#### 4.2.5 小练习与答案

**练习 1**：`set_backend(A)` 嵌套 `set_backend(B)`（B 在内层），同时调用一个两者都能处理的多方法，会调用谁？

> **答案**：调用 **B**。局部后端是栈式结构，最内层（最后 `with` 的）优先级最高，所以 B 先被尝试且成功。

**练习 2**：为什么 `set_global_backend` 的文档警告「库作者不要在代码里调用，只应由用户调用」？

> **答案**：因为全局后端是**进程级**状态且**非线程安全**（[L337](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_backend.py#L337)）。如果某个库偷偷改了全局后端，会污染调用它的所有用户代码。正确做法是库用 `set_backend`（局部、`with` 退出即恢复），把「全局」的选择权留给最终用户。

---

### 4.3 scipy.fft 的具体分发实现

前两节讲的是 uarray 的通用机制，本节看 `scipy.fft` 如何把它用起来——这也是本讲的**主实践**所在。

#### 4.3.1 概念说明

`scipy.fft` 把「多方法」和「后端」两部分分别落在两个文件：

- **多方法侧（`_basic.py`）**：`fft`/`ifft`/`fft2`/… 每个函数都被 `@_dispatch` 装饰，从而变成域为 `"numpy.scipy.fft"`、对输入数组 `x` 进行分发的多方法。
- **后端侧（`_backend.py`）**：定义默认后端 `_ScipyBackend`，它的 `__ua_function__` 会到 `_basic_backend` / `_realtransforms_backend` / `_fftlog_backend` 这几个「实现模块」里按函数名查找真正干活的函数。

并且，在 `_backend.py` **模块加载的最后一行**调用了 `set_global_backend('scipy', try_last=True)`，把 SciPy 自带实现注册为「全局兜底后端」。这一行非常关键——它保证了：即使用户什么后端都不设，`scipy.fft.fft` 也能算出正确结果；而一旦用户 `register_backend` 了一个 CuPy 后端，CuPy 就会**优先于** SciPy 接管（因为 SciPy 是 `try_last`）。

#### 4.3.2 核心流程

调用 `scipy.fft.fft(x)` 的完整旅程：

```
scipy.fft.fft(x)              # fft 是 _Function（多方法），域 = "numpy.scipy.fft"
   │
   ▼  C++ 分发循环按优先级遍历后端
   ├─ 1) 局部 set_backend 的后端（若有，内层优先）→ backend.__ua_function__(fft, (x,...), {})
   ├─ 2) register_backend 注册的后端（如 cupy_backend）
   └─ 3) 全局 scipy 后端（try_last，兜底）
            │ _ScipyBackend.__ua_function__
            ▼  getattr(_basic_backend, "fft")
               _execute_1D(...)  →  duccfft 或 xp.fft   # 真正的 FFT 计算
   返回结果；任一后端返回 NotImplemented 则继续下一个
```

#### 4.3.3 源码精读

**① 生成多方法：`_dispatch` 装饰器。** [scipy/fft/_basic.py:7-22](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic.py#L7-L22) 是核心：

```python
def _x_replacer(args, kwargs, dispatchables):   # L7 参数替换器：替换输入数组 x
    if len(args) > 0:
        return (dispatchables[0],) + args[1:], kwargs
    kw = kwargs.copy()
    kw['x'] = dispatchables[0]
    return args, kw

def _dispatch(func):                             # L18 把普通函数变成多方法
    return generate_multimethod(func, _x_replacer, domain="numpy.scipy.fft")
```

注意 `_x_replacer` 只替换第一个位置参数或关键字 `x`——也就是说 `scipy.fft` **只对输入数组 `x` 这一个参数做分发**。随后 [L25-L27](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic.py#L25-L27) 用 `@xp_capabilities(...)` 叠 `@_dispatch` 装饰真实的 `fft` 函数（`fft` 函数体本身只是文档字符串，真正的计算在后端里）。

**② 默认后端 `_ScipyBackend`。** [scipy/fft/_backend.py:8-29](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L8-L29)：

```python
class _ScipyBackend:
    __ua_domain__ = "numpy.scipy.fft"            # L17 域

    @staticmethod
    def __ua_function__(method, args, kwargs):   # L19 真正的分发
        fn = getattr(_basic_backend, method.__name__, None)        # 先查基础变换
        if fn is None:
            fn = getattr(_realtransforms_backend, method.__name__, None)  # 再查 DCT/DST
        if fn is None:
            fn = getattr(_fftlog_backend, method.__name__, None)         # 再查 Hankel
        if fn is None:
            return NotImplemented                 # L28 都没有 → 礼貌拒绝
        return fn(*args, **kwargs)
```

类注释（[L9-L16](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L9-L16)）专门解释了为什么域写成 `"numpy.scipy.fft"`：因为 uarray 把域当层级看，这样「装一个 `numpy` 域的后端就能顺带实现 `numpy.scipy.fft`」。

**③ 字符串到后端的映射与校验。** [scipy/fft/_backend.py:32-49](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L32-L49)：

```python
_named_backends = {'scipy': _ScipyBackend}       # L32 字符串别名表

def _backend_from_arg(backend):                   # L37 校验并归一化
    if isinstance(backend, str):
        backend = _named_backends[backend]        # 'scipy' → _ScipyBackend
    if backend.__ua_domain__ != 'numpy.scipy.fft':
        raise ValueError('Backend does not implement "numpy.scipy.fft"')
    return backend
```

这就是为什么你能写 `set_backend('scipy')`，也是为什么你自己写的后端**必须**把 `__ua_domain__` 设成 `"numpy.scipy.fft"`，否则 `_backend_from_arg` 会直接拒绝。

**④ 四个封装函数 + 默认注册。** [scipy/fft/_backend.py:52-208](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L52-L208) 的 `set_global_backend`/`register_backend`/`set_backend`/`skip_backend` 都是「先用 `_backend_from_arg` 校验，再转发给 `ua.*`」的薄封装。最关键的是文件**最后一行**：

```python
set_global_backend('scipy', try_last=True)        # L211 把 SciPy 设为全局兜底后端
```

这一行在 `import scipy.fft` 时执行，确立了「SciPy 永远是最后的退路」。

**⑤ 真正的 FFT 计算。** [scipy/fft/_basic_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic_backend.py) 的 `_execute_1D` 等函数（文件头部注释 [L16-L25](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic_backend.py#L16-L25) 解释了路由策略）：默认情况或输入是 NumPy 数组时用编译扩展 `_duccfft`；当开了 `SCIPY_ARRAY_API` 且输入是 CuPy/Torch 等数组时，尝试 `xp.fft.*`。

**⑥ 一个现成的自定义后端范例。** [scipy/fft/tests/mock_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/mock_backend.py) 示范了「**整个模块就是一个后端**」的写法：它在模块级定义 `__ua_domain__ = "numpy.scipy.fft"`（[L58](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/mock_backend.py#L58)）和模块级函数 `__ua_function__`（[L93-L96](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/mock_backend.py#L93-L96)），把模块本身传给 `set_backend` 即可。测试 [test_backend.py:62-66](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/test_backend.py#L62-L66) 正是用 `with set_backend(mock_backend, only=True)` 验证分发被接管。

#### 4.3.4 代码实践（本讲主实践：注册一个打印日志的自定义后端）

**实践目标**：仿照 `_ScipyBackend` 与 `mock_backend`，写一个「日志后端」，在 `with scipy.fft.set_backend(...)` 内调用 `scipy.fft.fft`，验证：① 我们的 `__ua_function__` 确实被调用（看到日志）；② 当我们返回 `NotImplemented` 且**未设** `only` 时，分发会**回退**到默认 SciPy 后端并给出正确结果；③ 当设了 `only=True` 时，回退被禁止，于是报错。

**操作步骤**：把下面代码存为 `fft_log_backend.py` 并运行。

```python
# 示例代码：自定义“日志后端”，演示分发与回退
import numpy as np
import scipy.fft as sfft


class LoggingBackend:
    """一个只打印日志、本身不计算的后端；返回 NotImplemented 表示“交还控制权”。"""
    __ua_domain__ = "numpy.scipy.fft"          # 必须与 _backend_from_arg 的校验一致

    @staticmethod
    def __ua_function__(method, args, kwargs):
        print(f"[LOG] 拦截到多方法: {method.__name__}, 参数个数={len(args)}")
        return NotImplemented                   # 礼貌拒绝 → 分发器继续找下一个后端


x = np.arange(8)

print("=== (A) 不设 only：日志后端拒绝后，回退到默认 scipy 后端 ===")
with sfft.set_backend(LoggingBackend()):       # 注意：没有 only=True
    y = sfft.fft(x)
print("结果前 3 项:", y[:3])
print("结果是否等于默认 scipy 实现:", np.allclose(y, sfft.fft(x)))

print("\n=== (B) 设 only=True：日志后端拒绝后立即停止，不回退 ===")
try:
    with sfft.set_backend(LoggingBackend(), only=True):
        sfft.fft(x)
except Exception as e:
    print("设 only=True 时抛出:", type(e).__name__)
```

**需要观察的现象**：
- (A) 中应先看到一行 `[LOG] 拦截到多方法: fft ...`，然后仍能拿到正确的 FFT 结果，且与默认实现一致——说明 `NotImplemented` 触发了回退。
- (B) 中应抛出异常（`only=True` 禁止回退）。

**预期结果**（具体异常类名以本地为准）：

```
=== (A) 不设 only：日志后端拒绝后，回退到默认 scipy 后端 ===
[LOG] 拦截到多方法: fft, 参数个数=1
结果前 3 项: [28.+0.j -4.+8.j -4.+3.41421356j]
结果是否等于默认 scipy 实现: True

=== (B) 设 only=True：日志后端拒绝后立即停止，不回退 ===
设 only=True 时抛出: BackendNotImplementedError
```

> 说明：本实践综合验证了三件事——后端协议（`__ua_domain__`+`__ua_function__`）、`NotImplemented` 回退、以及 `only` 短路。它的「拒绝即回退」语义与 4.1.3 引用的 `__init__.py` 文档字符串 doctest（[L99-L108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py#L99-L108)）完全一致。若想让日志后端「既打印又自己算」，可在 `__ua_function__` 里像 `_ScipyBackend` 那样 `getattr(_basic_backend, method.__name__)` 来委托计算（见小练习 2）。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认注册用的是 `set_global_backend('scipy', try_last=True)`，而不是普通 `set_global_backend('scipy')`？

> **答案**：`try_last=True` 让 SciPy 后端排到**最后**才尝试。这样用户通过 `register_backend` 注册的 CuPy/Torch 等后端会**优先**于 SciPy 被调用，而 SciPy 始终作为兜底。若不设 `try_last`，SciPy 会先于用户后端被尝试并成功，用户后端就永远没机会接管了——这与「让用户自由切换 GPU/异构后端」的设计目标相悖。

**练习 2**：改造 `LoggingBackend`，使其在打印日志后**真正算出结果**（而不依赖回退）。提示：参考 [\_backend.py:19-29](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_backend.py#L19-L29)。

> **答案**：在 `__ua_function__` 里把 `return NotImplemented` 换成委托：
> ```python
> from scipy.fft import _basic_backend
> fn = getattr(_basic_backend, method.__name__, None)
> if fn is None:
>     return NotImplemented
> return fn(*args, **kwargs)
> ```
> 这样它就等价于 `_ScipyBackend` 再多打一行日志。注意：**不能**在 `__ua_function__` 里直接调用 `scipy.fft.fft`，否则会递归触发分发、造成无限循环。

---

### 4.4 多方法的边界：哪些函数被分发，哪些不是

#### 4.4.1 概念说明

一个常见的误解是「`scipy.fft` 里所有函数都能被后端接管」。**并非如此**。`scipy.fft` 的函数分两类：

- **多方法类**（`_basic.py`/`_realtransforms.py`/`_fftlog.py` 里的 `fft`/`dct`/`fht` 等）：被 `@_dispatch` 装饰，**会被 uarray 分发**，自定义后端能拦截它们。
- **普通函数类**（`_helper.py` 里的 `fftfreq`/`rfftfreq`/`fftshift`/`ifftshift`/`next_fast_len`/`prev_fast_len`）：**不是多方法**，它们走的是上一讲（u2-l1）讲过的 `array_namespace` 路线——直接看输入数组的类型来选实现，**自定义 uarray 后端拦截不到它们**。

这条边界很重要：如果你写了一个 CuPy 后端并 `set_backend`，它能接管 `fft`，但 `fftshift` 仍会按「数组本身是不是 CuPy 数组」来决定走哪条路。

#### 4.4.2 核心流程

以 `fftshift` 为例（[_helper.py:262-312](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L262-L312)）：

```
fftshift(x):
    xp = array_namespace(x)          # 看 x 是哪种数组（numpy/cupy/...）
    if hasattr(xp, 'fft'):
        return xp.fft.fftshift(x, axes=axes)   # 直接用该数组库自带的 fftshift
    # 否则回退到 numpy 计算，再转回 xp 类型
    x = np.asarray(x)
    y = np.fft.fftshift(x, axes=axes)
    return xp.asarray(y)
```

这与多方法的「用户选后端」截然不同——这里**没有** `__ua_function__`、**没有**后端遍历，纯粹是「数组类型驱动」的分支。

#### 4.4.3 源码精读

**① 装饰器一眼区分两类函数。** 对比两个装饰器：
- 多方法侧用 `@_dispatch`（内部是 `generate_multimethod`），见 [\_basic.py:25-27](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_basic.py#L25-L27)。
- 普通函数侧用 `@xp_capabilities()`（**不是**多方法装饰器），见 [\_helper.py:149](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L149) 的 `fftfreq`、[L262](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L262) 的 `fftshift`。`xp_capabilities` 只是一个标注 Array API 能力的装饰器（来自 u2-l1 提到的 `_array_api`），不引入任何分发。

**② fftfreq：典型的「看 xp 选实现」。** [scipy/fft/_helper.py:192-199](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L192-L199)：

```python
xp = np if xp is None else xp
if hasattr(xp, 'fft') and xp.__name__ != 'numpy':
    return xp.fft.fftfreq(n, d=d, device=device)
if device is not None:
    raise ValueError('device parameter is not supported for input array type')
return np.fft.fftfreq(n, d=d)
```

它依据传入的 `xp`（数组命名空间）分支：非 NumPy 且有 `fft` 模块就用对应库的实现，否则用 NumPy。整个过程与 uarray 后端无关。

**③ 包导出也分两路。** [scipy/fft/\_\_init\_\_.py:86-97](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/__init__.py#L86-L97) 分别从 `_basic`/`_realtransforms`/`_fftlog`（多方法）、`_helper`（普通函数）、`_backend`（后端控制函数）三处导入，结构上就暗示了这三类函数的不同性质。

#### 4.4.4 代码实践

**实践目标**：用类型检查和实际拦截两种方式，亲手确认 `fft` 是多方法、`fftshift` 不是。

```python
# 示例代码：区分多方法与普通函数
import numpy as np
import scipy.fft as sfft

# (1) 看对象类型
print("type(sfft.fft)      =", type(sfft.fft).__name__)
print("type(sfft.fftshift) =", type(sfft.fftshift).__name__)

# (2) 实测拦截：自定义后端能否拦到它们？
class Probe:
    __ua_domain__ = "numpy.scipy.fft"
    @staticmethod
    def __ua_function__(method, args, kwargs):
        print(f"  [拦截] {method.__name__}")
        return NotImplemented

with sfft.set_backend(Probe()):
    print("调用 fft ——")
    sfft.fft(np.arange(4))
    print("调用 fftshift ——")
    sfft.fftshift(np.arange(4))
```

**操作步骤**：运行脚本。

**需要观察的现象**：
- `type(sfft.fft)` 应表现为 uarray 生成的多方法对象类型；`type(sfft.fftshift)` 应是普通 `function`。（两类对象的确切类型名以本地为准。）
- 拦截实验中：调用 `fft` 时应打印 `[拦截] fft`（被后端看到）；调用 `fftshift` 时**不会**打印任何拦截信息（因为它根本不走分发）。

**预期结果**：

```
type(sfft.fft)      = _Function
type(sfft.fftshift) = function
调用 fft ——
  [拦截] fft
调用 fftshift ——
```

> 说明：`fft` 的类型名是否恰好显示为 `_Function` 依赖本地版本，但关键是它**与 `fftshift` 的类型不同**，且只有 `fft` 会被 `Probe` 拦截。`_Function` 这个类型名来自 `generate_multimethod` 内 `functools.update_wrapper(ua_func, ...)` 不会改变对象类型的事实（见 4.1.3 ②）。若本地结果与预期不符，记为「待本地验证」并重点对比两个 `type(...)` 的差异即可。

#### 4.4.5 小练习与答案

**练习 1**：你 `set_backend` 了一个自定义后端，希望它也能接管 `fftshift`，能做到吗？

> **答案**：**不能**。`fftshift` 不是 uarray 多方法，没有 `__ua_function__` 分发环节，自定义后端无从拦截。它的实现由 `array_namespace(x)` 决定（[_helper.py:307](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L307)）。要改变它的行为，只能改变输入数组的类型（即下一讲 u2-l3 的 Array API 路线）。

**练习 2**：如果要新增一种「全新的、连 `xp.fft` 都没有的数组后端」，uarray 多方法和 `array_namespace` 两种风格，哪种更合适？

> **答案**：**uarray 多方法**更合适。因为 `array_namespace` 风格依赖目标数组库**自身已实现** `xp.fft.fftshift` 等（见 [_helper.py:308](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/_helper.py#L308) 的 `hasattr(xp, 'fft')` 判断）；若新后端没有这些，就走不通。而 uarray 多方法允许你**自己写一份完整实现**放进 `__ua_function__`，对外仍暴露同一个 `scipy.fft.fft` 接口，这正是它的设计初衷。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「**带优先级的可观测 FFT 后端**」小任务：

1. **写两个后端**：
   - `FastBackend`：在 `__ua_function__` 里打印 `[FAST] <方法名>`，并通过 `getattr(_basic_backend, method.__name__)` 委托 SciPy 真实实现（参考 4.3.5 练习 2），返回正确结果。
   - `SlowBackend`：同样打印 `[SLOW] <方法名>`，但**故意** `return NotImplemented`（模拟「我处理不了」）。
   两者都设 `__ua_domain__ = "numpy.scipy.fft"`。
2. **验证优先级**：用 `with sfft.set_backend(FastBackend()):` 套 `with sfft.set_backend(SlowBackend()):`（Slow 在内层），调用 `sfft.fft(np.arange(8))`。预期：先看到 `[SLOW] fft`（内层优先），它拒绝后回退，再看到 `[FAST] fft`，最终拿到正确结果。
3. **验证 only 短路**：把内层改成 `set_backend(SlowBackend(), only=True)`，再次调用，预期抛 `BackendNotImplementedError`（拒绝后立即停止，不再回退到 Fast）。
4. **验证边界**：在上述任意 `with` 块内调用 `sfft.fftshift(np.arange(8))`，确认**不会**出现 `[FAST]/[SLOW]` 日志——因为 `fftshift` 不走分发（4.4）。
5. **画一张调用链**：把第 2 步中「多方法被调用 → C++ 遍历后端（Slow→Fast→全局 scipy）→ 命中」的过程画成流程图，标注每一步对应 4.3.3 里的哪个源码位置。

> 这个任务同时覆盖了：后端协议（4.1）、优先级与 only（4.2）、`scipy.fft` 落地与 `_ScipyBackend` 委托（4.3）、多方法边界（4.4）。若某一步结果与预期不符，先用 4.1.4 的最小例子确认你的 uarray 环境正常，再排查 `__ua_domain__` 是否写对、是否在 `__ua_function__` 里误调了 `scipy.fft.fft` 导致递归。

## 6. 本讲小结

- **多方法 + 后端**是 uarray 的两个正交角色：API 设计者用 `generate_multimethod(extractor, replacer, domain, default)` 生成「只有名字、不实现计算」的多方法；实现者写带 `__ua_domain__` 和 `__ua_function__` 的后端。
- **`NotImplemented` 是回退的关键**：后端可以礼貌拒绝，分发器（C++ 循环 `_uarray_dispatch.cxx`）会按优先级继续尝试下一个，全拒且有 `default` 则用 default，否则抛 `BackendNotImplementedError`。
- **四种激活方式**各有适用场景：`set_backend`（临时、最高优先、栈式）、`set_global_backend`（全局、可 `try_last`）、`register_backend`（全局、最低优先）、`skip_backend`（临时黑名单）；`only`/`coerce` 控制短路。
- **`scipy.fft` 的落地**：`_basic.py` 用 `@_dispatch` 把变换函数做成域为 `"numpy.scipy.fft"` 的多方法（只对输入 `x` 分发）；`_backend.py` 的 `_ScipyBackend` 按 `getattr(_basic_backend, name)` 委托真实计算；`set_global_backend('scipy', try_last=True)` 让 SciPy 永远兜底、让用户后端优先。
- **并非所有 `scipy.fft` 函数都被分发**：`fft`/`dct`/`fht` 是多方法（可被后端拦截），而 `fftfreq`/`fftshift` 等是走 `array_namespace` 的普通函数（拦截不到）——这是 uarray 与下一讲 Array API 的分界线。

## 7. 下一步学习建议

- **承接本讲的下一篇是 u2-l3《Array API 数组后端覆盖》**：那里讲 4.4 提到的「另一条路线」——`SCIPY_ARRAY_API` 环境变量、`array_namespace`、`_asarray` 抽象如何让 CuPy/Torch/JAX/Dask 数组贯穿 SciPy。学完后你将能完整对比「用户选后端（uarray）」与「代码看数组（Array API）」两套机制。
- **想看更多 uarray 实战**：阅读 [scipy/fft/tests/mock_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/mock_backend.py)（模块即后端的写法）和 [scipy/fft/tests/test_backend.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/test_backend.py)，以及 [scipy/fft/tests/test_multithreading.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/fft/tests/test_multithreading.py)（后端状态为何能跨线程/进程——见 4.2.3 ⑤ 的 pickle 注册）。
- **想深入分发核心**：通读 [scipy/_lib/_uarray/\_\_init\_\_.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/__init__.py) 的文档字符串（一份可运行的 doctest 教程），再看 [_uarray_dispatch.cxx](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_uarray/_uarray_dispatch.cxx) 中 `for_each_backend` 的真实遍历实现。
- **后续 u12-l1《fft 傅里叶变换与后端分发》** 会从「使用者」视角再讲一遍 fft 的接口与归一化，届时你会更清楚自己写的后端到底替换了哪一层。
