# uarray 多方法与 _dispatch：函数即协议

> 本讲属于进阶层（intermediate），承接 u1-l2「目录结构与四层架构」。
> 你已经知道 `scipy.fft` 分为「公共 API 层 → uarray 分派层 → 后端层 → ducc 计算核心层」四层。
> 本讲要钻进**第二层**，回答一个反直觉的问题：

**为什么 `scipy.fft.fft` 的函数体里只有一行 `return (Dispatchable(x, np.ndarray),)`，却能算出真正的 FFT？**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 uarray 的**三要素**：domain（域）、multimethod（多方法）、Dispatchable（可分派标记）分别是什么。
2. 看懂 `_basic.py` 中 `_dispatch` 与 `_x_replacer` 两个小函数，理解 `generate_multimethod` 是如何把一个「普通函数」变成「可分派多方法」的。
3. 解释为什么公共函数体只 `return (Dispatchable(x, np.ndarray),)`——它不是计算代码，而是一份**分派协议声明**。
4. 在 REPL 中亲手验证 `scipy.fft.fft` 是一个 uarray multimethod，并讲清楚一次 `fft([1,2,3,4])` 是如何最终落到默认 scipy 后端的。

---

## 2. 前置知识

本讲会用到几个概念，先用最朴素的话解释：

- **多方法（multimethod / multiple dispatch）**：普通函数是「看参数值执行固定代码」；多方法是「先看参数的类型/出身，再决定把活儿派给哪一段实现」。你可以把它想成一个**调度员**：同一个函数名，背后可能站着好几套实现（后端），调度员根据「谁在调用、用什么数组」决定派单。

- **后端（backend）**：一套函数实现。`scipy.fft` 默认的后端叫 `_ScipyBackend`（真正干活的 ducc 内核）；但 uarray 的设计允许你**插拔**别的后端（比如基于 CuPy 上 GPU、或基于 NumPy 的纯 Python 后端）。这一讲只关注「分派机制」，后端管理 API（`set_backend` 等）留到 u4-l2。

- **uarray**：SciPy 内嵌的一个独立小库（vendored 在 `scipy/_lib/_uarray/`），专门提供「多方法 + 可插拔后端」机制。`scipy.fft` 只是它的一个使用者。

- **「函数即协议」**：uarray 最巧妙的设计是——一个多方法的「函数体」并不执行业务逻辑，而是**描述自己有哪些参数可以被后端替换**。下面会反复回到这一点。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `scipy/fft/_basic.py` | 定义 `_dispatch`、`_x_replacer`，以及 fft/ifft/rfft 等 18 个复变换多方法 |
| `scipy/fft/_realtransforms.py` | dct/dst 等 8 个实变换多方法，**复用** `_basic._dispatch` |
| `scipy/fft/_fftlog.py` | fht/ifht 两个 Hankel 变换多方法，同样**复用** `_basic._dispatch` |
| `scipy/fft/_backend.py` | 默认后端 `_ScipyBackend`，以及 import 时把 scipy 注册为全局后端的语句 |
| `scipy/_lib/_uarray/_backend.py` | uarray 的纯 Python 层：`generate_multimethod`、`Dispatchable` 的定义 |
| `scipy/_lib/_uarray/_uarray_dispatch.cxx` | uarray 的 C++ 核心：多方法的真正 `__call__`、分派循环、repr |

> 注意：前三行都在 `scipy/fft/` 内（公共 API 层），后三行属于 uarray 分派层。本讲会来回穿梭这几层。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应大纲指定的 `_dispatch`、`_x_replacer`、`generate_multimethod`、`Dispatchable`。

### 4.1 domain、multimethod、Dispatchable：uarray 的三要素

#### 4.1.1 概念说明

要理解 `scipy.fft` 的分派层，先记住 uarray 的**三要素**：

1. **domain（域）**：一个点分字符串，是「路由键」。`scipy.fft` 所有多方法都用同一个域 `"numpy.scipy.fft"`。一个后端只要声明 `__ua_domain__ = "numpy.scipy.fft"`，就表示「这个域里的所有多方法，我都能接管」。域是点分层级结构（`numpy.scipy.fft` 属于 `numpy` 家族），细节留到后端管理讲义。

2. **multimethod（多方法）**：调用 `scipy.fft.fft(...)` 时，你调用的对象就是一个多方法。它**不是普通函数**，而是一个 uarray 内部类型 `_Function` 的实例，它的 `repr` 长这样：`<uarray multimethod 'fft'>`。

3. **Dispatchable（可分派标记）**：一个用来「给参数贴标签」的小类，告诉 uarray「这个参数是可被后端替换的数组，它的目标类型是 `np.ndarray`」。

这三者怎么协作？一句话：

> **一个多方法 = 一段「参数提取器」 + 一段「参数回填器」 + 一个 domain。** 调用时，uarray 按 domain 找后端，把提取器标出的参数交给后端处理。

#### 4.1.2 核心流程

把一个普通函数变成多方法，由 `generate_multimethod` 完成，它接收三个关键入参：

```
generate_multimethod(
    argument_extractor,   # 参数提取器：声明哪些参数可分派（即「函数体」）
    argument_replacer,    # 参数回填器：把转换后的数组塞回调用签名
    domain,               # 路由域，如 "numpy.scipy.fft"
)
```

它的内部做了两件事：构造一个 `_Function` 实例（真正的多方法对象），再用 `functools.update_wrapper` 把原函数的 `__name__`、`__doc__` 等元信息「嫁接」到这个实例上——这样多方法对外看起来仍叫 `fft`、仍有完整 docstring，但本质已经换成可分派对象。

调用 `fft([1,2,3,4])` 时的执行路径（精简版，完整版见 4.3）：

```
fft([1,2,3,4])                 # 你调用的「多方法」
   │
   ▼  _Function.__call__  (C++ 分派循环)
   │   for_each_backend("numpy.scipy.fft")：按域找后端
   ▼
   找到默认 _ScipyBackend
   │   _ScipyBackend.__ua_function__(fft, args, kwargs)
   │   → getattr(_basic_backend, "fft")  按方法名查实现
   ▼
   _basic_backend.fft → _execute_1D → _duccfft → C 扩展 pyduccfft
```

注意第 3 步「按方法名查实现」：默认后端是靠**多方法的名字** `fft` 去三个 `*_backend` 模块里 `getattr` 找同名实现的。所以多方法的 `__name__` 必须正确——这正是 `update_wrapper` 的意义。

#### 4.1.3 源码精读

先看 `generate_multimethod` 的签名与函数体：

[_uarray/_backend.py:L174-L179](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_backend.py#L174-L179) —— 函数签名，三个关键入参一目了然。

[_uarray/_backend.py:L239-L249](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_backend.py#L239-L249) —— 构造 `_Function` 并嫁接元信息：

```python
kw_defaults, arg_defaults, opts = get_defaults(argument_extractor)
ua_func = _Function(
    argument_extractor,
    argument_replacer,
    domain,
    arg_defaults,
    kw_defaults,
    default,
)
return functools.update_wrapper(ua_func, argument_extractor)
```

`_Function` 是 C++ 类型，它的 `repr` 把它身份「自报家门」：

[_uarray_dispatch.cxx:L1408-L1414](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1408-L1414) —— `Function::repr`：当 `__name__` 已被嫁接时，打印 `<uarray multimethod 'fft'>`。这就是判断「它是不是 multimethod」最直接的证据。

再看默认后端如何「认领」这个域：

[_backend.py:L17-L17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L17-L17) —— `_ScipyBackend.__ua_domain__ = "numpy.scipy.fft"`，与多方法的 domain 完全一致，所以它能接管 `scipy.fft` 的所有多方法。

[_backend.py:L211-L211](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L211-L211) —— `set_global_backend('scipy', try_last=True)`：`_backend.py` 一被 import 就执行这一行，把 scipy 注册为全局默认后端。这就是「为什么裸调 `fft([1,2,3,4])` 能找到实现」的根源——`__init__.py` 里 `from ._backend import ...` 触发了它。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `scipy.fft.fft` 确实是一个 uarray multimethod，而不是普通函数。

**操作步骤**（在 Python REPL 中）：

```python
import scipy.fft as spfft

print(repr(spfft.fft))            # 应打印 <uarray multimethod 'fft'>
print(type(spfft.fft))            # 应是 uarray 的 _Function 类型
print(spfft.fft.__name__)         # 'fft'（被 update_wrapper 嫁接而来）
print(spfft.fft.__doc__[:40])     # 仍是 fft 的 docstring
```

**需要观察的现象**：

- `repr(spfft.fft)` 输出形如 `<uarray multimethod 'fft'>`，而不是 `<function fft at 0x...>`。这一行直接证明它是 multimethod。
- `type(spfft.fft)` 的类名里含 `_Function`（位于 `scipy._lib._uarray._uarray` 模块）。

**预期结果**：

```
<uarray multimethod 'fft'>
```

> 若你的环境 `repr` 没有显示 `__name__`，说明 `update_wrapper` 未成功嫁接属性，属异常情况——可标注「待本地验证」并回看 4.1.3 的 cxx 源码确认 `Function` 类型带有 `tp_dictoffset`（支持 `__dict__`）。

---

### 4.2 `_dispatch` 装饰器：把普通函数变成多方法的本地薄封装

#### 4.2.1 概念说明

`scipy.fft` 有 28 个变换函数（fft 族 18 个 + dct/dst 族 8 个 + fht/ifht 2 个），每一个都要变成多方法。如果每次都手写一遍 `generate_multimethod(func, _x_replacer, domain="numpy.scipy.fft")`，太啰嗦。于是 `_basic.py` 顶部定义了一个**一行**的装饰器 `_dispatch`，把「固定的 domain」和「共享的回填器 `_x_replacer`」打包好，公共函数只需在头顶加一个 `@_dispatch`。

这是典型的「**把重复配置提炼成装饰器**」手法。

#### 4.2.2 核心流程

```
@_dispatch                # 2. fft = generate_multimethod(fft, _x_replacer, "numpy.scipy.fft")
def fft(x, n=None, ...):  # 1. 先定义普通函数 fft（它的函数体即「参数提取器」）
    return (Dispatchable(x, np.ndarray),)
```

装饰器执行后，名字 `fft` 不再指向那个普通函数，而是指向 `_Function` 多方法对象。原函数被当作「参数提取器」封装进了多方法内部。

关键：`_realtransforms.py` 和 `_fftlog.py` **不重新定义** `_dispatch`，而是直接 `from ._basic import _dispatch` 复用。所以三个文件、28 个函数共用同一份 domain 与回填器，分派层只此一家。

#### 4.2.3 源码精读

[_basic.py:L18-L22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L18-L22) —— `_dispatch` 的全部内容：

```python
def _dispatch(func):
    """
    Function annotation that creates a uarray multimethod from the function
    """
    return generate_multimethod(func, _x_replacer, domain="numpy.scipy.fft")
```

注意它只是「转发」：把被装饰的 `func` 当作 `argument_extractor` 传进去，domain 写死为 `"numpy.scipy.fft"`，回填器固定为同文件的 `_x_replacer`。

装饰器的使用现场（fft）：

[_basic.py:L25-L28](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L25-L28) —— `@xp_capabilities(...)` 与 `@_dispatch` 双层装饰，定义 `fft`。

> 小知识：`@xp_capabilities(allow_dask_compute=True)` 是数组标准能力标注（u6-l2 专题），与分派机制正交。装饰器从下往上作用，所以 `_dispatch` 先把 `fft` 变成多方法，`xp_capabilities` 再包一层。对分派行为本身无影响。

复用 `_dispatch` 的另外两个文件：

[_realtransforms.py:L1-L2](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms.py#L1-L2) —— `from ._basic import _dispatch`，dct/dst 族复用同一份分派逻辑。

[_fftlog.py:L8-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog.py#L8-L9) —— `from ._basic import _dispatch`，fht/ifht 同样复用。

#### 4.2.4 代码实践

**实践目标**：确认三个文件的 28 个变换函数共用同一个 `_dispatch`。

**操作步骤**：

```python
import scipy.fft as spfft
import scipy.fft._basic, scipy.fft._realtransforms, scipy.fft._fftlog

# 这三个函数对象是否「同源」？
print(scipy.fft._basic._dispatch is scipy.fft._realtransforms._dispatch)  # True
print(scipy.fft._basic._dispatch is scipy.fft._fftlog._dispatch)         # True
```

**需要观察的现象**：两个比较都应输出 `True`，证明实变换与 Hankel 变换复用了 `_basic` 的分派器，而非各自另起炉灶。

**预期结果**：`True` / `True`。

---

### 4.3 Dispatchable 与「函数体只 return」之谜：函数体即参数提取器

#### 4.3.1 概念说明

这是本讲最反直觉、也最关键的一点。再看一眼 `fft` 的函数体（剥去 docstring 后）：

```python
def fft(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None):
    """（长篇 docstring）"""
    return (Dispatchable(x, np.ndarray),)   # ← 就这一行业务「逻辑」
```

一个号称「计算 FFT」的函数，函数体却只是把输入 `x` 包进 `Dispatchable` 然后返回——它**没有算任何傅里叶变换**。为什么？

答案：**在 uarray 的世界里，这个函数体扮演的是「参数提取器（argument extractor）」，不是计算代码。** 它的职责只有一个：当 uarray 问「你这个函数里，哪些参数是可分派的、目标类型是什么」时，它返回一个由 `Dispatchable` 组成的元组作为回答。这里回答的是「第一个参数 `x` 是可分派的，目标类型 `np.ndarray`」。

换句话说，函数体的 `return` 是一份**声明**，描述的是「分派协议」，而真正的计算发生在后端层。

`Dispatchable(value, dispatch_type, coercible=True)` 三个字段：
- `value`：被标记的参数原值（这里是 `x`）。
- `type`：目标类型（这里是 `np.ndarray`）。
- `coercible`：是否允许「昂贵转换」（如把 NumPy 数组拷到 GPU），默认 `True`。

#### 4.3.2 核心流程

那么函数体（提取器）到底什么时候被调用？关键在 C++ 分派循环里的一段判断：**只有当某个后端定义了 `__ua_convert__`（即它需要把数组转换成自己的类型）时，提取器才会被调用，用来取出可分派参数。** 默认的 `_ScipyBackend` 没有 `__ua_convert__`，所以一次普通的 `fft(numpy数组)` 调用，**提取器根本不会被执行**——参数原封不动直接喂给后端的 `__ua_function__`。

完整调用流程（带转换分支）：

```
fft(args, kwargs)
  │  _Function::call
  ▼
for_each_backend("numpy.scipy.fft"):      # 按域遍历后端
  │
  ├─ replace_dispatchables(backend, args, kwargs):
  │     if backend 没有 __ua_convert__:        # 默认 scipy 后端走这条
  │         return args, kwargs  (原样)         # ← 提取器、回填器都不调用
  │     else:                                   # 需要转换的后端走这条
  │         dispatchables = 提取器(args, kwargs)   # ← 此时函数体才执行
  │         converted   = backend.__ua_convert__(dispatchables)
  │         args, kwargs = 回填器(args, kwargs, converted)  # 把转换后的数组塞回去
  │
  ▼
  backend.__ua_function__(multimethod, args, kwargs)   # 真正干活
```

记住这条分界线：**提取器是「按需执行」的元数据，不是每次调用都跑的热路径。**

#### 4.3.3 源码精读

`Dispatchable` 的定义极其简单：

[_uarray/_backend.py:L412-L452](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_backend.py#L412-L452) —— `class Dispatchable`，核心是 `__init__(self, value, dispatch_type, coercible=True)`，把值与类型打包。

公共函数体（提取器）的现场：

[_basic.py:L168-L168](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L168-L168) —— `return (Dispatchable(x, np.ndarray),)`，fft 的全部「业务逻辑」。注意末尾的逗号——它返回的是**单元素元组**，因为提取器约定「返回一组可分派参数」。

[_realtransforms.py:L72-L72](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms.py#L72-L72) —— `dctn` 同样 `return (Dispatchable(x, np.ndarray),)`。

[_fftlog.py:L214-L214](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog.py#L214-L214) —— `fht` 返回 `return (Dispatchable(a, np.ndarray),)`。注意这里参数名是 `a` 而非 `x`（见 4.4 的讨论）。

C++ 分派循环对「是否需要提取器」的判断（核心证据）：

[_uarray_dispatch.cxx:L1197-L1202](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1197-L1202) —— `replace_dispatchables` 开头：若后端没有 `__ua_convert__`，直接原样返回参数，跳过提取器：

```cpp
auto has_ua_convert = PyObject_HasAttr(backend, identifiers.ua_convert->get());
if (!has_ua_convert) {
  return {py_ref::ref(args), py_ref::ref(kwargs)};   // 默认 scipy 后端走这条
}
```

[_uarray_dispatch.cxx:L1204-L1205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1204-L1205) —— 只有需要转换时，才调用提取器 `PyObject_Call(extractor_.get(), args, kwargs)` 取出 dispatchables。这正是「函数体（提取器）何时执行」的源头。

[_uarray_dispatch.cxx:L1312-L1317](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1312-L1317) —— 随后调用后端的 `__ua_function__(backend, multimethod, args, kwargs)`，这里才是 `_ScipyBackend` 按 `method.__name__` 查实现的地方。

#### 4.3.4 代码实践

**实践目标**：用最小示例亲手复刻「函数体即提取器」的机制，直观看到提取器的返回值。

**操作步骤**：

```python
from scipy._lib.uarray import generate_multimethod, Dispatchable

# 1) 定义「参数提取器」——它的函数体就是声明：第一个参数 a 可分派，目标类型 int
def my_extractor(a, b):
    return (Dispatchable(a, int),)        # 注意：与 fft 的函数体同构

# 2) 定义一个最小「参数回填器」（4.4 会讲，这里先占位）
def my_replacer(args, kwargs, dispatchables):
    return (dispatchables[0],) + args[1:], kwargs

# 3) 生成多方法
my_mm = generate_multimethod(my_extractor, my_replacer, "ua_demo")

print(repr(my_mm))           # <uarray multimethod 'my_extractor'>
print(my_mm.__name__)        # 'my_extractor'（update_wrapper 嫁接而来）
# my_mm(1, "x")              # 会抛 BackendNotImplementedError：没有任何后端实现它
```

**需要观察的现象**：

- `repr(my_mm)` 显示它是 uarray multimethod，证明一行 `generate_multimethod` 就把普通函数变成了多方法。
- 直接调用 `my_mm(1, "x")` 会抛 `BackendNotImplementedError`，因为域 `"ua_demo"` 下没有任何后端——这反向印证了「函数体不是计算，真正计算要靠后端」。

**预期结果**：

```
<uarray multimethod 'my_extractor'>
my_extractor
```

> 这个例子脱胎于 uarray 官方 docstring（`scipy/_lib/_uarray/__init__.py`），是理解三要素最短的路径。

#### 4.3.5 小练习与答案

**练习 1**：`fft` 的函数体为什么末尾要有个逗号 `return (Dispatchable(x, np.ndarray),)`，去掉会怎样？

> **参考答案**：逗号让它成为单元素元组 `(Dispatchable(...),)`。uarray 约定提取器返回「一组可分派参数」（元组）。若去掉逗号，返回的是单个 `Dispatchable` 对象而非元组，分派循环里 `PySequence_Tuple` 仍可能兼容，但会与「多分派参数」的语义不符，并可能在多分派参数场景下出错。保留逗号是稳妥写法。

**练习 2**：一次普通的 `scipy.fft.fft(np.arange(8))` 调用，`fft` 的函数体（提取器）会被执行吗？

> **参考答案**：不会。默认后端 `_ScipyBackend` 没有定义 `__ua_convert__`，C++ 分派循环在 `replace_dispatchables` 开头即原样返回参数，跳过提取器调用；参数直接进入 `__ua_function__`。提取器只在「需要转换数组的后端」介入时才执行。

---

### 4.4 `_x_replacer`：分派时如何把（转换后的）数组塞回调用

#### 4.4.1 概念说明

`_x_replacer` 是 `generate_multimethod` 的第二个入参，角色是**参数回填器（argument replacer）**。

设想一个会把数组搬到 GPU 的后端：它先用 `__ua_convert__` 把输入 `x`（NumPy 数组）转成 CuPy 数组；可问题是，uarray 接下来要用 `(args, kwargs)` 去调用后端的 `__ua_function__`，而这个 `x` 还是旧的 NumPy 数组。**回填器的职责就是：给定转换后的新数组，把它重新塞回 `args`/`kwargs` 的正确位置**，让后端拿到的是转换后的版本。

`_x_replacer` 针对 `scipy.fft` 的一个统一约定：**可分派的数组永远是第一个位置参数**（fft/rfft/dct 都叫 `x`）。所以它的逻辑只有两条路：位置参数或关键字参数 `x`。

#### 4.4.2 核心流程

```
_x_replacer(args, kwargs, dispatchables):
    if 有位置参数 args:                       # 数组是位置参数（args[0]）
        return (dispatchables[0],) + args[1:], kwargs
    else:                                     # 数组用关键字传入
        kw = kwargs.copy()
        kw['x'] = dispatchables[0]            # 把转换后的数组放进关键字 'x'
        return args, kw
```

它返回的是一个二元组 `(args, kwargs)`，这正是 C++ 侧 `replace_dispatchables` 期待的格式。C++ 在调用 `__ua_convert__` 得到转换后的 dispatchables 后，立刻调用 `_x_replacer(args, kwargs, converted)` 把它们回填，再把回填后的 `(args, kwargs)` 传给 `__ua_function__`。

> 一个隐藏约定：`_x_replacer` 假设可分派数组要么是第一个位置参数，要么关键字名为 `'x'`。`fht`/`ifht` 的数组参数叫 `a`/`A`（不是 `x`），因此它们**必须以位置参数方式传入**——若用 `fht(a=arr, ...)` 这种关键字调用，回填器会往 `kw['x']` 里塞，与 `fht` 实际参数名 `a` 对不上，分派会出错。这就是为什么 fft/rfft/dct 族统一把数组命名成 `x`。

#### 4.4.3 源码精读

[_basic.py:L7-L15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L7-L15) —— `_x_replacer` 的全部内容：

```python
def _x_replacer(args, kwargs, dispatchables):
    """
    uarray argument replacer to replace the transform input array (``x``)
    """
    if len(args) > 0:
        return (dispatchables[0],) + args[1:], kwargs
    kw = kwargs.copy()
    kw['x'] = dispatchables[0]
    return args, kw
```

C++ 侧调用回填器的现场（紧接 `__ua_convert__` 之后）：

[_uarray_dispatch.cxx:L1221-L1229](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1221-L1229) —— 把 `__ua_convert__` 的结果转成元组 `replaced_args`，再用 `(args, kwargs, replaced_args)` 调用 `_x_replacer`（即 `replacer_`），得到回填后的 `(new_args, new_kwargs)`：

```cpp
auto replaced_args = py_ref::steal(PySequence_Tuple(res.get()));
...
PyObject * replacer_args[] = {nullptr, args, kwargs, replaced_args.get()};
res = py_ref::steal(Q_PyObject_Vectorcall(
    replacer_.get(), &replacer_args[1], ...));
```

[_uarray_dispatch.cxx:L1233-L1248](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1233-L1248) —— 校验回填器返回的必须是二元组 `(args, kwargs)`，否则报错。这正是对 `_x_replacer` 返回格式的硬约束。

#### 4.4.4 代码实践

**实践目标**：用一个「会偷换数组」的后端，亲眼看到 `_x_replacer` 把转换后的数组塞回了调用。

**操作步骤**：

```python
import numpy as np
import scipy.fft as spfft

# 一个最小后端：它声明域、并在 __ua_convert__ 里把传入数组「放大 100 倍」
class ScalingBackend:
    __ua_domain__ = "numpy.scipy.fft"

    @staticmethod
    def __ua_convert__(dispatchables, coerce):
        # 把每个可分派参数替换成「原值 * 100」
        return [d.value * 100 for d in dispatchables]

    @staticmethod
    def __ua_function__(method, args, kwargs):
        # 直接把（已被 _x_replacer 回填过的）数组平方求和，证明拿到的是放大后的版本
        x = args[0]
        return np.sum(x ** 2)

# 用 set_backend 临时挂上我们的后端（API 细节见 u4-l2，这里先用起来）
with spfft.set_backend(ScalingBackend()):
    out = spfft.fft(np.array([1.0, 2.0, 3.0]))  # 放大后是 [100,200,300]
print(out)   # 预期 100^2 + 200^2 + 300^2 = 140000
```

**需要观察的现象**：

- `out` 等于 `140000`，说明后端的 `__ua_function__` 收到的 `args[0]` 是 `[100,200,300]`（放大版），而非原始的 `[1,2,3]`。
- 这正证明了链路：`__ua_convert__` 转换 → `_x_replacer` 把转换结果回填到 `args` → `__ua_function__` 拿到回填后的数组。

**预期结果**：`140000.0`。

> 若 `__ua_convert__` 返回的元素个数或顺序与提取器声明的 dispatchables 不一致，回填会错位——这也是为什么 `_x_replacer` 只取 `dispatchables[0]`：提取器只声明了一个可分派参数 `x`。

#### 4.4.5 小练习与答案

**练习 1**：如果有一个变换函数，它的可分派数组**不是**第一个位置参数（比如是第二个），现有的 `_x_replacer` 还能用吗？为什么？

> **参考答案**：不能直接用。`_x_replacer` 写死了「可分派参数在 `args[0]` 或关键字 `x`」的假设。若数组在第二位，需要另写一个回填器，如 `return args[:1] + (dispatchables[0],) + args[2:], kwargs`。`scipy.fft` 能复用同一个 `_x_replacer`，前提是所有变换都遵守「数组是首个位置参数」的约定。

**练习 2**：为什么 `fht(a, dln, mu, ...)` 能和 `_x_replacer` 配合工作，尽管它的数组参数叫 `a` 而不是 `x`？

> **参考答案**：因为正常调用 `fht` 时数组 `a` 总是作为**第一个位置参数**传入，命中 `_x_replacer` 的 `if len(args) > 0` 分支，直接替换 `args[0]`，根本不会走到依赖关键字名 `'x'` 的 else 分支。这隐含了一条使用规则：调用 `fht`/`ifht` 时数组应按位置传，不要用关键字 `a=`。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，亲手搭建一个完整的「多方法 + 后端」微型系统，跑通一次分派，并解释每一步对应本讲的哪个概念。

**操作步骤**：

```python
import numpy as np
from scipy._lib.uarray import generate_multimethod, Dispatchable

# ① 提取器（对应 4.3：函数体即协议声明）
def my_fft(x, n=None):
    """my mini fft multimethod"""
    return (Dispatchable(x, np.ndarray),)

# ② 回填器（对应 4.4：把转换后的数组塞回）
def my_replacer(args, kwargs, dispatchables):
    if len(args) > 0:
        return (dispatchables[0],) + args[1:], kwargs
    kw = kwargs.copy(); kw['x'] = dispatchables[0]
    return args, kw

# ③ 多方法（对应 4.1/4.2：generate_multimethod + domain）
MyFFT = generate_multimethod(my_fft, my_replacer, domain="demo.fft")
print("① 多方法 repr:", repr(MyFFT))
print("② __name__:", MyFFT.__name__, " __doc__:", MyFFT.__doc__.strip())

# ④ 后端（对应 4.1：__ua_domain__ + __ua_function__ 按方法名分派）
class NumpyBackend:
    __ua_domain__ = "demo.fft"
    @staticmethod
    def __ua_function__(method, args, kwargs):
        # 简单「实现」：返回数组长度，模拟一次真实计算
        return len(args[0])

# ⑤ 挂载后端并调用（对应 4.1 核心流程）
from scipy._lib.uarray import set_backend
with set_backend(NumpyBackend()):
    print("③ 分派结果:", MyFFT(np.arange(10)))   # 预期 10
```

**需要观察的现象与对应关系**：

| 现象 | 对应本讲概念 |
| --- | --- |
| `repr(MyFFT)` 为 `<uarray multimethod 'my_fft'>` | 4.1：它是 multimethod（`_Function`） |
| `MyFFT.__name__`/`__doc__` 与原函数一致 | 4.1/4.2：`update_wrapper` 嫁接元信息 |
| 函数体 `return (Dispatchable(x, np.ndarray),)` 不算 FFT | 4.3：函数体是提取器，是协议声明 |
| 调用结果是 `10` 而非报错 | 4.1：按域 `"demo.fft"` 找到 `NumpyBackend` 并执行 `__ua_function__` |

**预期结果**：

```
① 多方法 repr: <uarray multimethod 'my_fft'>
② __name__: my_fft  __doc__: my mini fft multimethod
③ 分派结果: 10
```

**思考延伸**：把 `NumpyBackend` 的 `__ua_function__` 改成 `return NotImplemented`，再调用 `MyFFT(np.arange(10))`，会得到 `BackendNotImplementedError`——这正是后端「拒绝实现、留给下一个后端」的回退机制（u4-l2、u8-l2 会展开）。

---

## 6. 本讲小结

- **uarray 三要素**：`domain`（路由键 `"numpy.scipy.fft"`）、multimethod（`_Function` 实例，`repr` 为 `<uarray multimethod 'fft'>`）、`Dispatchable`（给可分派参数贴类型标签）。
- **`_dispatch`** 是 `_basic.py` 里的一行装饰器，把「写死的 domain + 共享的 `_x_replacer`」打包，被 `_realtransforms.py`/`_fftlog.py` 复用，28 个变换函数共用同一份分派逻辑。
- **函数体即参数提取器**：`return (Dispatchable(x, np.ndarray),)` 不是计算，而是声明「`x` 可分派、目标 `np.ndarray`」；默认 `_ScipyBackend` 无 `__ua_convert__`，故普通 numpy 调用并不执行提取器。
- **`_x_replacer` 是参数回填器**：在需要转换数组的后端介入时，把 `__ua_convert__` 的结果塞回 `args[0]`（或关键字 `x`），返回 `(args, kwargs)` 供 `__ua_function__` 使用。
- **默认后端的来源**：`_backend.py` 被 import 时执行 `set_global_backend('scipy', try_last=True)`，`_ScipyBackend.__ua_domain__` 与多方法 domain 一致，并靠 `method.__name__` 在三个 `*_backend` 模块里查找实现——这就是裸调 `fft` 能找到实现的完整链路。

---

## 7. 下一步学习建议

本讲只回答了「多方法是怎么构造的、分派是怎么穿透的」，但故意把**后端管理的优先级语义**留在了门外。建议接下来：

1. **学 u4-l2「后端管理」**：搞清楚 `set_backend` / `set_global_backend` / `register_backend` / `skip_backend` 的作用域与优先级（局部 > 全局 > 注册），以及 `only` / `coerce` / `try_last` 三个标志的差别——尤其是为什么 scipy 默认要用 `try_last=True`。
2. **学 u4-l3「默认与调试后端」**：剖析 `_ScipyBackend.__ua_function__` 如何在 `_basic_backend` / `_realtransforms_backend` / `_fftlog_backend` 三个模块间按方法名查找，以及用 `_debug_backends.py` 的 `EchoBackend` / `NumPyBackend` 调试分派。
3. **延伸阅读**：`scipy/_lib/_uarray/__init__.py` 顶部的 docstring 是 uarray 协议最权威的入门教程；`tests/mock_backend.py`（`_implements` 字典 + `__ua_function__`）是「按方法对象分派」的另一范本，可与本讲的「按方法名分派」对照。
