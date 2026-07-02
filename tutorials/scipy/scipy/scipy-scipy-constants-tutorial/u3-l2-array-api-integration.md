# Array API 集成与 xp_capabilities

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清「Python Array API 标准」要解决什么问题，以及 SciPy 为什么要把 `convert_temperature`、`lambda2nu`、`nu2lambda` 改造成能吃 NumPy 以外数组库（CuPy / PyTorch / JAX / Dask）的函数。
2. 区分两件容易被混淆的事：**运行时的后端分发**（靠 `array_namespace` 与 `_asarray`）和 **声明/文档/测试用的元数据**（靠 `@xp_capabilities` 装饰器）。
3. 读懂 `@xp_capabilities()` 与 `@xp_capabilities(out_of_scope=True)` 在行为上的差异，并能解释为什么 `value` / `unit` / `precision` / `find` 这四个查询函数被标记为「不在 Array API 支持范围内」。

本讲是专家层的第二篇，承接 u3-l1（`convert_temperature` 的非线性温度换算），把视角从「单个函数的算法」抬升到「这一类函数如何被统一接入多后端体系」。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **NumPy 的 `ndarray` 与 `np.asarray`**：SciPy 历史上所有数值函数都默认输入是 NumPy 数组。
- **模块与命名空间（namespace）**：一个数组库（如 `numpy`、`torch`）就是一个「命名空间」，里面提供 `asarray`、`astype`、算术运算符等一套统一接口。
- **装饰器（decorator）**：`@something` 语法，本质是「接收函数、返回函数」的高阶函数。
- 本手册 u1-l4（单位换算因子与 `_cd`/`value` 的关系）、u3-l1（`convert_temperature` 的实现）。

一个关键直觉：所谓「Array API 标准」就是约定一份**所有数组库都遵守的同一份函数签名**（`xp.asarray`、`xp.astype`、`__add__` 等）。只要 SciPy 内部只调用这套标准接口、不直接写 `np.xxx`，那么把 NumPy 数组换成 CuPy 数组，同一段代码也能跑——这就是「一次编写，多后端运行」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_constants.py](_constants.py) | 三个**参与** Array API 的函数 `convert_temperature` / `lambda2nu` / `nu2lambda` 的实现，在函数体里调用 `array_namespace` 与 `_asarray`。 |
| [_codata.py](_codata.py) | 四个**不参与** Array API 的查询函数 `value` / `unit` / `precision` / `find`，仅用 `@xp_capabilities(out_of_scope=True)` 标注。 |
| `scipy/_lib/_array_api.py`（外部共享模块） | 提供 `array_namespace`、`_asarray`、`xp_capabilities`、`make_xp_test_case` 等基础设施，被整个 SciPy 复用。 |
| `scipy/_lib/_array_api_override.py`（外部共享模块） | `array_namespace` 的真正实现，以及控制全局开关的环境变量 `SCIPY_ARRAY_API`。 |
| `scipy/constants/tests/test_constants.py` | 用 `make_xp_test_case` 把温度/光学函数参数化到多个数组库上做回归测试。 |

> 提示：后两个文件在 `scipy/_lib/` 下，不在 `scipy/constants/` 子包内，因此本讲为它们构造的是指向同一 commit 的完整 GitHub 链接，而不是 constants 目录的相对链接。

## 4. 核心概念与源码讲解

### 4.1 Python Array API 标准与 SciPy 的适配目标

#### 4.1.1 概念说明

长期以来，SciPy 的函数都「写死」在 NumPy 上：函数体里直接 `np.asarray(x)`，返回值也一定是 `numpy.ndarray`。这意味着你想在 GPU 上算（用 CuPy）、想用自动微分（用 JAX / PyTorch）、想延迟计算（用 Dask），都得自己手工搬运数据，SciPy 帮不上忙。

[Python Array API 标准](https://data-apis.org/array-api/latest/purpose_and_scope.html)的出现就是为了打破这种绑定：它规定了一组**所有符合标准的数组库都必须实现的统一接口**。于是 SciPy 的改造思路变成——

> 只要我在函数里**不直接用 `np.xxx`**，而是先问一句「这个输入数组来自哪个库」，拿到那个库（即「命名空间」`xp`），再统一用 `xp.asarray(...)` 和标准运算符，同一段代码就能在 NumPy、CuPy、PyTorch、JAX、Dask 上跑。

这就是 `scipy.constants` 里 `convert_temperature`、`lambda2nu`、`nu2lambda` 三个函数改造的背景。注意：constants 子包里**绝大多数内容是纯标量常量**（`pi`、`c`、`kilo`……），它们与数组库无关，所以真正需要做 Array API 适配的只有这三个**接收数组、返回数组**的函数。

#### 4.1.2 核心流程

SciPy 的多后端分发有一个**全局开关**，这点非常容易踩坑：

1. 读取环境变量 `SCIPY_ARRAY_API`。如果不设置，SciPy 处于「NumPy-only」模式，即使你传入 CuPy 数组，分发函数也会假装没看见、直接返回 NumPy 命名空间。
2. 只有当 `SCIPY_ARRAY_API=1` 时，分发函数才会真正去检查输入数组的类型，并返回对应的命名空间（`numpy` / `cupy` / `torch` / `jax.numpy` / `dask.array`）。
3. 拿到 `xp` 后，函数体用 `xp.asarray` 和标准运算符做计算，输出数组的类型自然与输入一致。

这个开关是为了**默认行为零变化**：绝大多数用户不需要多后端，SciPy 不应因为多了这套机制而变慢或变严格。

#### 4.1.3 源码精读

全局开关在 `_array_api_override.py` 里，本质是一个从环境变量读出来的模块级变量：

[scipy/_lib/_array_api_override.py:27] `SCIPY_ARRAY_API` 从环境变量读取，默认为 `False`（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_override.py#L27）

```python
SCIPY_ARRAY_API: str | bool = os.environ.get("SCIPY_ARRAY_API", False)
```

而 `array_namespace` 函数的开头就是这道闸门——开关关闭时直接返回 NumPy 命名空间，跳过一切合规检查：

[scipy/_lib/_array_api_override.py:111-L113] 当 `SCIPY_ARRAY_API` 未启用时，`array_namespace` 无条件返回 NumPy 命名空间（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_override.py#L111-L113）

```python
if not SCIPY_ARRAY_API:
    # here we could wrap the namespace if needed
    return np_compat
```

`_array_api.py` 顶部的模块文档也点明了这套机制的用途与背景：

[scipy/_lib/_array_api.py:1-L8] 该模块是「使用 Array API 兼容库的工具集」，并给出了标准与 SciPy 用例的两个官方链接（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L1-L8）

#### 4.1.4 代码实践

**实践目标**：亲手验证「不开 `SCIPY_ARRAY_API` 时，`array_namespace` 总是返回 NumPy」。

**操作步骤**：

```python
# 示例代码
import os
from scipy._lib._array_api import array_namespace
import numpy as np

print("SCIPY_ARRAY_API =", os.environ.get("SCIPY_ARRAY_API"))
print(array_namespace(np.array([1, 2, 3])))      # -> <module 'numpy'>
print(array_namespace([1, 2, 3]))                 # Python list -> 仍是 numpy
```

**需要观察的现象**：默认环境下，无论传入 NumPy 数组还是 Python 列表，`array_namespace` 都返回 `numpy` 命名空间。

**预期结果**：第一行打印 `SCIPY_ARRAY_API = None`；后两行都返回 `numpy` 模块对象。

> 若你本地装了 PyTorch 并想看真正的跨后端分发，可在**启动 Python 前**设环境变量 `SCIPY_ARRAY_API=1`，再 `array_namespace(torch.tensor([1,2.]))` 观察它返回 `torch`。该结果依赖外部库是否安装，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SciPy 要用一个环境变量来「默认关闭」多后端分发，而不是默认开启？

**参考答案**：默认开启会改变绝大多数只用 NumPy 的用户的运行时行为——分发函数要对每个输入做类型检查、合规校验（拒绝 `np.matrix`、`MaskedArray` 等），既增加开销也可能让原本「能跑」的旧代码报错。用环境变量把关，可以做到「老用户零感知、需要多后端的用户主动开启」。

**练习 2**：constants 子包里有几十个公开符号，为什么真正需要 Array API 适配的只有三个函数？

**参考答案**：其余符号要么是纯标量常量（`pi`、`c`、`kilo`、`mile`……），要么是它们的算术组合，本身参与的是 Python 标量运算，根本不接收「数组」输入。只有 `convert_temperature` / `lambda2nu` / `nu2lambda` 接收 `array_like` 并返回数组，才有「输出该跟谁的后端」的问题。

---

### 4.2 `array_namespace` 与 `_asarray`：运行时的后端分发

#### 4.2.1 概念说明

上一节讲的是「开关」，本节讲「开关打开后，函数体内部到底怎么做分发」。两个核心工具是：

- **`array_namespace(val)`**：传入一个（或多个）数组，返回它所属的命名空间模块 `xp`。这是「问一句：你来自哪个库？」。
- **`_asarray(val, xp=xp, subok=True)`**：SciPy 自家的 `np.asarray` 替代品，能把列表、标量等「类数组」输入规整成真正的数组，并且**用指定的 `xp` 来构造**，从而保证结果与输入同后端。

`_asarray` 之所以不直接用标准里的 `xp.asarray`，是因为它额外支持 `order`、`check_finite`、`subok` 这几个 Array API 标准里没有、但 SciPy 老代码需要的参数。

#### 4.2.2 核心流程

以 `lambda2nu` 为例（计算 \(\nu = c / \lambda\)），改造后的函数体只有两行核心逻辑：

1. `xp = array_namespace(lambda_)` —— 问输入来自哪个库。
2. `return c / _asarray(lambda_, xp=xp, subok=True)` —— 用该库把输入规整成数组，再做标准除法。

`c` 是一个 Python 浮点常量（光速）。标准规定「数组 ÷ 标量」由数组的命名空间决定结果类型，所以 NumPy 数组进去就出 NumPy 数组，CuPy 数组进去就出 CuPy 数组。整个函数里**没有出现一个 `np.`**，这就是 Array API 适配的本质。

`_asarray` 的内部分支很清晰：

- 若 `xp` 是 NumPy：走 `np.asanyarray`（当 `subok=True`），保留子类行为，并支持 `order`。
- 若 `xp` 是其他后端：走 `xp.asarray(array, dtype=dtype, copy=copy)`，标准接口。

#### 4.2.3 源码精读

`_constants.py` 顶部一次性把三个工具从共享模块导入：

[_constants.py:18](_constants.py#L18) 从共享模块 `scipy._lib._array_api` 导入 `array_namespace`、`_asarray`、`xp_capabilities` 三个名字（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L18）

```python
from scipy._lib._array_api import array_namespace, _asarray, xp_capabilities
```

`convert_temperature` 函数体的分发两件套（注意它复用了 u3-l1 讲过的 `zero_Celsius`）：

[_constants.py:273-L274](_constants.py#L273-L274) 先取命名空间，再用 `_asarray(..., subok=True)` 把输入规整为数组（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L273-L274）

```python
xp = array_namespace(val)
_val = _asarray(val, xp=xp, subok=True)
```

`lambda2nu` 把分发与计算压缩成两行：

[_constants.py:336-L337](_constants.py#L336-L337) `nu = c / lambda`，`c` 为光速标量，结果类型由 `_asarray` 出来的数组决定（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L336-L337）

```python
xp = array_namespace(lambda_)
return c / _asarray(lambda_, xp=xp, subok=True)
```

`nu2lambda` 与之对称（`nu2lambda` 的实现见 [_constants.py:368-L369](_constants.py#L368-L369)，公式 \(\lambda = c / \nu\)）。

再看 `_asarray` 的实现，体会「NumPy 走老路、其他后端走标准」的双分支：

[scipy/_lib/_array_api.py:100-L116] `subok=True` 时 NumPy 走 `np.asanyarray` 保留子类；其他后端走 `xp.asarray` 标准接口（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L100-L116）

```python
if xp is None:
    xp = array_namespace(array)
if is_numpy(xp):
    # Use NumPy API to support order
    ...
    elif subok:
        array = np.asanyarray(array, order=order, dtype=dtype)
    ...
else:
    try:
        array = xp.asarray(array, dtype=dtype, copy=copy)
    except TypeError:
        coerced_xp = array_namespace(xp.asarray(3))
        array = coerced_xp.asarray(array, dtype=dtype, copy=copy)
```

> 这里有个常被忽略的细节：`_asarray` 的 `subok=True` 在 constants 三个函数里都用了，目的是对 NumPy 输入保留 `np.asanyarray` 的「穿透子类」行为；对非 NumPy 后端则无意义（标准里没有子类概念），直接走 `xp.asarray`。

#### 4.2.4 代码实践

**实践目标**：用 NumPy 数组调用 `lambda2nu` 和 `convert_temperature`，确认输出类型跟随输入。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.constants import lambda2nu, convert_temperature, speed_of_light

# 1) lambda2nu：数组进 -> 数组出
out1 = lambda2nu(np.array([1.0, speed_of_light]))
print(type(out1), out1)        # 期望 <class 'numpy.ndarray'> [2.99792458e+08 1.]

# 2) convert_temperature：数组进 -> 数组出
out2 = convert_temperature(np.array([-40, 40]), 'Celsius', 'Kelvin')
print(type(out2), out2)        # 期望 numpy.ndarray [233.15 313.15]

# 3) 列表进：内部被 _asarray 规整成 NumPy 数组
out3 = lambda2nu([speed_of_light, 1.0])
print(type(out3))              # 期望 numpy.ndarray
```

**需要观察的现象**：当输入是 `numpy.ndarray` 时，输出也是 `numpy.ndarray`；当输入是 Python 列表时，`_asarray` 会先把它转成 NumPy 数组，输出仍是 `numpy.ndarray`。

**预期结果**：三条 `type(...)` 都应打印 `numpy.ndarray` 相关类型。

> 关于跨后端（CuPy/PyTorch/JAX）：必须先设 `SCIPY_ARRAY_API=1` 才会真正分发，且依赖对应库已安装，具体返回类型（如 `torch.Tensor`）**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`lambda2nu` 函数体里没有任何 `if 判断后端` 的分支，它是怎么做到「输入什么库就输出什么库」的？

**参考答案**：靠两点：① `array_namespace(lambda_)` 把后端信息抽出来变成 `xp`；② `_asarray(lambda_, xp=xp, ...)` 用这个 `xp` 构造数组。之后的 `c / array` 走的是 Python 运算符协议，结果类型由左侧数组（也就是 `_asarray` 的产物）的命名空间决定，无需手写分支。

**练习 2**：为什么 `_asarray` 对 NumPy 要单独走 `np.asanyarray`，而不是统一用 `xp.asarray`？

**参考答案**：因为 SciPy 历史上依赖 NumPy 的 `subok`（保留子类）和 `order`（内存布局）语义，这两个都不在 Array API 标准里。对 NumPy 输入保留老行为可以不破坏既有用法；对其他后端则退回标准接口。

---

### 4.3 `@xp_capabilities` 装饰器：元数据而非运行时包装

#### 4.3.1 概念说明

这是本讲最容易产生误解的地方：**`@xp_capabilities()` 装饰器本身并不让函数支持 Array API**。让函数支持多后端的是上一节讲的 `array_namespace` + `_asarray`；装饰器只负责两件「外围」工作：

1. **登记元数据**：把「这个函数在哪些后端、哪些设备（CPU/GPU）上被测试过」记录到一张全局表 `capabilities_table` 里，供测试框架 `make_xp_test_case` 自动生成 `skip` / `xfail` 标记。
2. **改写文档字符串**：自动在函数 docstring 末尾追加一段「Array API Standard Support」说明，列出支持的后端表格。

也就是说，装饰器是一个**纯标注层**：它不改变函数的运行逻辑，只是声明「我支持哪些后端」并据此生成文档和测试。理解了这一点，就不会误以为「加上 `@xp_capabilities()` 就自动支持 CuPy」。

#### 4.3.2 核心流程

装饰器接收一批「能力参数」（`skip_backends`、`cpu_only`、`np_only`、`out_of_scope` 等），然后：

1. 计算出一个 `capabilities` 字典，存入 `capabilities_table[f]`（`f` 是被装饰函数）。
2. 用 `_make_sphinx_capabilities(...)` 生成一份给文档用的能力描述。
3. 用 `_make_capabilities_note(...)` 生成一段说明文字，追加进 `f.__doc__`。
4. **原样返回 `f`**（不包 wrapper），所以运行时函数对象没变。

关键一点：装饰器内部**没有 `return wrapper`，只有 `return f`**。这一点在源码里非常显眼。

#### 4.3.3 源码精读

三个参与 Array API 的函数都在函数定义上方加了无参的 `@xp_capabilities()`：

[_constants.py:228](_constants.py#L228) `convert_temperature` 上方的装饰器（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L228）；同样用法见 `lambda2nu`（[_constants.py:308](_constants.py#L308)）与 `nu2lambda`（[_constants.py:340](_constants.py#L340)）。

```python
@xp_capabilities()
def convert_temperature(val, old_scale, new_scale):
    ...
```

装饰器的「不包装」特性——这是理解它纯标注属性的关键：

[scipy/_lib/_array_api.py:922-L939] 装饰器把能力登记进表、追加 docstring，然后**原样返回 `f`**，不套 wrapper（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L922-L939）

```python
def decorator(f):
    # Don't use a wrapper, as in some cases @xp_capabilities is
    # applied to a ufunc
    capabilities_table[f] = capabilities
    doc = FunctionDoc(f)
    if not np_only or out_of_scope:
        note = _make_capabilities_note(f.__name__, sphinx_capabilities, extra_note)
        doc['Notes'].append(note)
    ...
    return f
return decorator
```

默认能力表（无参调用时，所有后端在 CPU 上都「已测试」）：

[scipy/_lib/_array_api.py:750-L760] 默认认为 NumPy/array_api_strict/CuPy/PyTorch/JAX/Dask 都至少在 CPU 上被测试过（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L750-L760）

#### 4.3.4 代码实践

**实践目标**：验证 `@xp_capabilities()` 不改变函数的运行时身份，只改了 docstring。

**操作步骤**：

```python
# 示例代码
from scipy.constants import lambda2nu
import scipy._lib._array_api as _aapi

# 1) 看函数是否还能像普通函数一样被直接调用
print(lambda2nu([3e8, 1.0]))

# 2) 它的 docstring 末尾应被追加了一段 Array API 说明
assert "Array API Standard Support" in (lambda2nu.__doc__ or "")

# 3) 它确实被登记进了 capabilities_table（装饰器的副作用）
print(lambda2nu in _aapi.xp_capabilities_table)
```

**需要观察的现象**：函数照常返回数值；docstring 里多出一段「Array API Standard Support」并带一张后端表；`lambda2nu` 出现在全局能力表里。

**预期结果**：第 3 步打印 `True`，证明装饰器登记了元数据。

#### 4.3.5 小练习与答案

**练习 1**：如果我把 `@xp_capabilities()` 这一行从 `lambda2nu` 上删掉，函数还能不能用 CuPy 数组？

**参考答案**：运行时**仍然能**（前提是 `SCIPY_ARRAY_API=1`），因为真正做分发的是函数体里的 `array_namespace` / `_asarray`，与装饰器无关。删掉装饰器损失的只是：① 文档里不再有支持后端表；② 测试框架不会再为它自动生成 skip/xfail 标记（可能导致本该跳过的后端被误跑而报错）。这正说明装饰器是「声明 + 测试 + 文档」层，不是「能力」本身。

**练习 2**：装饰器注释里写「Don't use a wrapper, as in some cases `@xp_capabilities` is applied to a ufunc」。为什么对 ufunc 不能套 wrapper？

**参考答案**：NumPy 的 ufunc 是 C 实现的特殊对象，套一层 Python wrapper 会破坏它的 `__call__`、`accumulate`、`outer`、类型解析等机制。所以装饰器选择「只改属性、不换对象」，原样返回 `f`，对普通函数和 ufunc 都安全。

---

### 4.4 `out_of_scope=True`：标量查询函数为何不参与

#### 4.4.1 概念说明

`_codata.py` 里的 `value` / `unit` / `precision` / `find` 是四个**查询函数**：输入一个字符串 key，输出一个标量（`float` 或 `str`）或一个列表。它们**不接收数组、不返回数组**，本质是对 `physical_constants` 字典的查找。

Array API 标准是为「数组计算」设计的。一个根本不碰数组的函数，自然不在它的支持范围内。SciPy 用 `@xp_capabilities(out_of_scope=True)` 来**显式声明**「这个函数不属于 Array API 的支持范围（除了 NumPy）」，从而：

- 文档里生成一段「not in-scope」说明，而不是后端支持表；
- 测试框架据此把这些函数排除在多后端测试之外。

注意 `out_of_scope` 与 `np_only` 的微妙关系：源码里 `out_of_scope=True` 会顺带把 `np_only` 也置为 `True`，但两者生成的文档说明不同。

#### 4.4.2 核心流程

`out_of_scope=True` 在装饰器内部的连锁反应：

1. 进入 `xp_capabilities`，`if out_of_scope: np_only = True`。
2. `_make_sphinx_capabilities` 一看到 `out_of_scope`，**直接短路返回** `{"out_of_scope": True}`，不再生成那张六后端能力表。
3. `_make_capabilities_note` 检测到能力字典里有 `"out_of_scope"` 键，生成「`func` is not in-scope ...」这段不同于常规的说明。
4. 函数体本身完全不变——`value(key)` 仍然只是查字典返回 `physical_constants[key][0]`，返回一个纯 Python `float`。

#### 4.4.3 源码精读

`_codata.py` 顶部**只**导入了 `xp_capabilities`（不像 `_constants.py` 还导入了 `array_namespace`/`_asarray`），因为这四个函数不做任何数组操作：

[_codata.py:60](_codata.py#L60) 这里只导入 `xp_capabilities`，因为查询函数不需要 `array_namespace` / `_asarray`（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L60）

```python
from scipy._lib._array_api import xp_capabilities
```

四个函数统一用 `out_of_scope=True` 标注，以 `value` 为例：

[_codata.py:2129-L2152](_codata.py#L2129-L2152) `value` 标记为「不在 Array API 支持范围」，函数体只是查字典取三元组的第一项（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2129-L2152）

```python
@xp_capabilities(out_of_scope=True)
def value(key: str) -> float:
    ...
    _check_obsolete(key)
    return physical_constants[key][0]
```

同样用法的还有 `unit`（[_codata.py:2155](_codata.py#L2155)）、`precision`（[_codata.py:2181](_codata.py#L2181)）、`find`（[_codata.py:2207](_codata.py#L2207)）。

再看装饰器内部 `out_of_scope` 如何短路掉能力表：

[scipy/_lib/_array_api.py:744-L745] 一旦 `out_of_scope=True`，能力表生成函数立刻返回，不构造六后端表格（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L744-L745）

```python
if out_of_scope:
    return {"out_of_scope": True}
```

并据此生成「not in-scope」专用说明（与参与函数的后端表说明截然不同）：

[scipy/_lib/_array_api.py:792-L803] 检测到 `out_of_scope` 后生成「不在 Array API 支持范围」的说明（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L792-L803）

```python
if "out_of_scope" in capabilities:
    note = f"""
    **Array API Standard Support**

    `{fun_name}` is not in-scope for support of Python Array API Standard compatible
    backends other than NumPy.
    ...
    """
```

而 `out_of_scope=True` 时 `np_only` 也被置真，但文档说明仍会写入——这靠的是这一行条件判断：

[scipy/_lib/_array_api.py:884-L885] `out_of_scope` 隐含 `np_only`（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L884-L885）

```python
if out_of_scope:
    np_only = True
```

[scipy/_lib/_array_api.py:927] 即使 `np_only` 为真，只要 `out_of_scope` 为真，说明文字仍会被追加（注意是「或」关系）（https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L927）

```python
if not np_only or out_of_scope:
    note = _make_capabilities_note(...)
```

#### 4.4.4 代码实践

**实践目标**：解释 `value('speed of light in vacuum')` 为什么不参与 Array API，并用代码确认它返回纯标量。

**操作步骤**：

```python
# 示例代码
from scipy.constants import value, unit, precision

v = value('speed of light in vacuum')
print(type(v), v)                       # 期望 <class 'float'> 299792458.0
print(unit('speed of light in vacuum')) # 'm s^-1'
print(precision('speed of light in vacuum'))  # 0.0（精确常数）

# 确认它的 docstring 是「not in-scope」版本
assert "not in-scope" in (value.__doc__ or "")
```

**需要观察的现象**：`value(...)` 返回的是 Python 内置 `float`，而不是任何数组类型；它的 docstring 里写的是「不在 Array API 支持范围」，而不是一张后端表。

**预期结果**：`type(v)` 为 `float`；`precision` 对精确常数返回 `0.0`；断言通过。

**为什么它不参与 Array API**：`value` 的输入是字符串 key、输出是字典里存好的标量，全程没有数组参与；Array API 解决的是「数组在不同后端间互通」的问题，对一个查字典返回 `float` 的函数没有意义。因此用 `out_of_scope=True` 显式声明它不属于这套体系，既诚实（文档不误导），又干净（测试不会拿它去多后端跑）。

#### 4.4.5 小练习与答案

**练习 1**：`precision(key)` 的返回值是 `physical_constants[key][2] / physical_constants[key][0]`（即不确定度除以值）。对精确常数它会返回 `0.0`。请解释为什么这个函数即使想支持 Array API 也做不到。

**参考答案**：它的输入是字符串 key，输出是两个标量相除得到的 Python `float`。既没有数组输入可以用来「推断后端」，也没有数组输出需要「跟随后端」。Array API 的整套机制（`array_namespace`、`_asarray`）对它无从施加，所以只能标 `out_of_scope=True`。

**练习 2**：对比 `convert_temperature`（参与）与 `value`（不参与）的 docstring，二者末尾的「Array API Standard Support」段落有什么区别？

**参考答案**：`convert_temperature` 的段落带一张六后端（NumPy/CuPy/PyTorch/JAX/Dask）的 CPU/GPU 支持表，并提示可用 `SCIPY_ARRAY_API=1` 测试；`value` 的段落则是一句话「is not in-scope for support of ... backends other than NumPy」，没有表格。这种区别完全由 `@xp_capabilities()`（无参）与 `@xp_capabilities(out_of_scope=True)` 两种调用决定。

---

## 5. 综合实践

把本讲三件事——「运行时分发」「装饰器是元数据」「out_of_scope 标量函数」——串起来，完成下面这个对照实验。

**任务**：写一段脚本，对 `lambda2nu`（参与 Array API）和 `value`（不参与）做并排观察，回答三个问题。

```python
# 示例代码
import numpy as np
import scipy._lib._array_api as _aapi
from scipy.constants import lambda2nu, value

# (A) 运行时分发：lambda2nu 的输出类型跟随输入
a = np.array([1.0, 2.0])
print("lambda2nu 输出类型:", type(lambda2nu(a)).__name__)

# (B) 装饰器是元数据：两个函数都被登记进能力表，但能力内容不同
cap_l = _aapi.xp_capabilities_table[lambda2nu]
cap_v = _aapi.xp_capabilities_table[value]
print("lambda2nu 的 out_of_scope =", cap_l["out_of_scope"])  # 期望 False
print("value     的 out_of_scope =", cap_v["out_of_scope"])  # 期望 True

# (C) 标量查询：value 返回纯 float，与数组后端无关
print("value 返回类型:", type(value('speed of light in vacuum')).__name__)
```

**需要回答的三个问题**：

1. `lambda2nu` 接收 `np.ndarray` 时返回什么类型？为什么？（对应 4.2 节）
2. `lambda2nu` 与 `value` 在 `capabilities_table` 里的 `out_of_scope` 字段为何不同？（对应 4.3、4.4 节）
3. 如果设置 `SCIPY_ARRAY_API=1` 并传入一个 PyTorch 张量，`value('speed of light in vacuum')` 的返回类型会变成 PyTorch 张量吗？为什么？

**预期结论**：

1. 返回 `ndarray`。因为函数体用 `array_namespace` + `_asarray` 规整输入，且 `c / array` 的结果类型由输入数组决定。
2. `lambda2nu` 无参装饰 → `out_of_scope=False`（声明参与）；`value` 用 `out_of_scope=True` → 声明不参与。这是开发者对「该函数是否处理数组」的显式标注。
3. **不会**。`value` 的函数体根本不看输入数组，它只查字典返回 `physical_constants[key][0]`，永远是 Python `float`。这也正是它标 `out_of_scope=True` 的原因——即便全局开关打开，它也没有可分发的数组。

> 跨后端部分（设 `SCIPY_ARRAY_API=1` 后用 PyTorch/CuPy 调 `lambda2nu`）依赖外部库是否安装，具体张量类型**待本地验证**。

## 6. 本讲小结

- **Array API 标准**让 SciPy 函数能脱离 NumPy 单一绑定，理论上同一份代码可在 CuPy/PyTorch/JAX/Dask 上运行；constants 子包里只有 `convert_temperature`、`lambda2nu`、`nu2lambda` 三个「数组进、数组出」的函数需要这套适配。
- 真正的**运行时分发**靠 `array_namespace(val)` 取命名空间 `xp`、再用 `_asarray(val, xp=xp, subok=True)` 规整输入；函数体里全程不出现 `np.`，输出类型由输入决定。
- 整套机制受**全局开关** `SCIPY_ARRAY_API` 控制：不设置时 `array_namespace` 一律返回 NumPy，默认行为零变化。
- `@xp_capabilities()` 是**纯标注装饰器**——它不包装函数、不改运行逻辑，只登记能力元数据（供测试用 `make_xp_test_case` 生成 skip/xfail）并追加 docstring 说明。
- `value` / `unit` / `precision` / `find` 用 `@xp_capabilities(out_of_scope=True)` 显式声明**不在 Array API 支持范围**，因为它们是查字典返回标量的函数，根本不接触数组；`out_of_scope` 会短路能力表生成并产出不同的「not in-scope」文档段落。
- 区分两件事是本讲的核心：**「能跑多后端」靠函数体写法，「声明/文档/测试支持哪些后端」靠装饰器**——两者分离，不能混为一谈。

## 7. 下一步学习建议

- 下一讲 **u3-l3（弃用模块垫片与版本演进策略）** 会转向 `codata.py` / `constants.py` 两个垫片模块，看 SciPy 如何用 PEP 562 的模块级 `__getattr__` 做惰性弃用重定向，与本讲的「元数据/声明」思路一脉相承。
- 想深入 Array API 测试体系，可直接阅读 **u3-l4（测试体系与回归保护）**，重点看 `tests/test_constants.py` 里 `@make_xp_test_case(sc.convert_temperature)` 如何读取本讲登记的能力表，自动把测试参数化到多个数组库。
- 想看更复杂的 Array API 用法（不只是 `c / array` 这种一行计算），可挑 SciPy 其他子包（如 `scipy.fft`、`scipy.special`）里同样戴 `@xp_capabilities` 但函数体更长的函数对照阅读。
- 若要追溯 `array_namespace`、`SCIPY_ARRAY_API` 的最底层实现，建议通读 `scipy/_lib/_array_api_override.py` 与 `scipy/_lib/_array_api.py` 两个共享模块——它们是整个 SciPy 多后端体系的地基。
