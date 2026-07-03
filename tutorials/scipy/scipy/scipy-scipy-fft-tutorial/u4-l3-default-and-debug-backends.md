# 默认后端 _ScipyBackend 与调试后端

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `_ScipyBackend` 作为 scipy.fft **默认后端**的两件套：`__ua_domain__`（声明自己属于哪个域）和 `__ua_function__`（声明自己如何执行某个多方法）。
- 复述 `_ScipyBackend.__ua_function__` 的「**方法名路由表**」：它用 `method.__name__` 在 `_basic_backend` / `_realtransforms_backend` / `_fftlog_backend` 三个模块里依次 `getattr` 查找实现，找不到就返回 `NotImplemented` 让位。
- 理解为什么 `_ScipyBackend` 是一个**无状态的路由器**，真正计算仍由三个 `*_backend` 模块各自下沉到 `_duccfft`。
- 区分两个**调试后端**：`NumPyBackend`（用 `numpy.fft` 当参照实现来交叉验证结果）与 `EchoBackend`（只打印分派时的 `method/args/kwargs`，相当于一个探针）。
- 学会亲手用 `set_backend(EchoBackend())` / `set_backend(NumPyBackend())` 观察分派过程，定位「后端到底收到了什么参数」。

本讲承接 [u4-l2](u4-l2-backend-management.md) 已建立的「后端优先级队列 + `NotImplemented` 让位 + `_backend_from_arg` 校验」认知，回答下一个自然问题：**当优先级轮到 scipy 默认后端时，它内部到底是怎样把一次 `fft(x)` 调用翻译成真实计算的？又该怎么观察这个过程？**

---

## 2. 前置知识

### 2.1 回顾：一个合格后端必须实现什么

在 [u4-l1](u4-l1-uarray-dispatch.md) 和 [u4-l2](u4-l2-backend-management.md) 中我们看到：

- scipy.fft 的公共函数（`fft`、`dct`、`fht`…）函数体只有一行 `return (Dispatchable(x, np.ndarray),)`，是「分派协议声明」而非计算代码。
- 真正计算由**后端（backend）**完成。一个后端只要满足 uarray 协议的两个属性即可被识别：

  - `__ua_domain__`：一个字符串，声明这个后端服务哪个域。scipy.fft 全家族共用 `"numpy.scipy.fft"`。
  - `__ua_function__(method, args, kwargs)`：当某个多方法（multimethod）轮到本后端执行时，uarray 会调用这个函数，把「是哪个方法、调用参数是什么」交给它。

- [u4-l2](u4-l2-backend-management.md) 讲了「谁来执行、按什么顺序」：scipy.fft 在 import 时执行 `set_global_backend('scipy', try_last=True)`，把字符串 `'scipy'` 翻译成 `_ScipyBackend` 并注册为**全局兜底后端**。

本讲就钻进 `_ScipyBackend.__ua_function__` 内部，看 `try_last` 轮到它时具体发生了什么。

### 2.2 本讲要用到的术语

- **方法名路由（dispatch by name）**：后端不关心 `method` 是哪个多方法对象本身，而是看它的**名字字符串**（`method.__name__`），再用这个名字去自己的实现表里查找。这是 `_ScipyBackend` 的核心设计。
- **让位（NotImplemented）**：后端对某个方法返回 `NotImplemented`，表示「我不接这个活」，uarray 自动尝试下一个后端（见 [u4-l2](u4-l2-backend-management.md)）。这与抛异常不同——异常会中断，`NotImplemented` 只是让位。
- **探针（probe）**：一个不真正计算、只为「观察」分派过程而存在的后端。本讲的 `EchoBackend` 就是探针。

> 关键直觉：后端的 `__ua_function__` 本质上就是一张「方法名 → 实现函数」的路由表。`_ScipyBackend` 把这张表拆进了三个 `*_backend` 模块，调试后端则用极简的路由表（直接转 `numpy.fft` 或干脆只打印）来服务于调试。

---

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
|------|------|------|
| [`_backend.py`](_backend.py) | 后端层 | 定义默认后端 `_ScipyBackend`、四个管理 API、`_backend_from_arg` 校验、`_named_backends` 命名映射；末尾 import 时注册 scipy 为全局后端。**本讲主角之一。** |
| [`_debug_backends.py`](_debug_backends.py) | 后端层 | 两个调试后端 `NumPyBackend` / `EchoBackend`，仅 23 行。**本讲另一主角。** |
| [`_basic_backend.py`](_basic_backend.py) | 后端层 | 18 个 FFT 族函数（`fft`/`ifft`/`rfft`/…/`ihfftn`），`_ScipyBackend` 查找的**第一站**。 |
| [`_realtransforms_backend.py`](_realtransforms_backend.py) | 后端层 | 8 个 DCT/DST 函数，`_ScipyBackend` 查找的**第二站**。 |
| [`_fftlog_backend.py`](_fftlog_backend.py) | 后端层 | `fht`/`ifht`（及直接函数 `fhtoffset`），`_ScipyBackend` 查找的**第三站**。 |
| [`tests/mock_backend.py`](tests/mock_backend.py) | 测试 | 测试用的 mock 后端，用**字典**做路由表——与 `_ScipyBackend` 的**属性查找**形成对照。 |

> 注意：`_debug_backends.py` **没有被** `__init__.py` 导入，所以 `NumPyBackend` / `EchoBackend` 不在 `scipy.fft` 的公共命名空间里。要用它们必须显式 `from scipy.fft._debug_backends import NumPyBackend, EchoBackend`（见 4.2/4.3 的实践）。这点常被初学者忽略。

---

## 4. 核心概念与源码讲解

### 4.1 _ScipyBackend：默认后端的「方法名路由表」

#### 4.1.1 概念说明

`_ScipyBackend` 是 scipy.fft 的**默认后端**——即那个真正调用 `_duccfft`（C 扩展 `pyduccfft`）干活的后端。它在 `_backend.py` 中定义，并在文件末尾被注册为全局兜底后端。

它的设计哲学可以一句话概括：**自己不计算，只负责「按方法名把调用路由到对应的实现模块」**。

为什么这样设计？回顾 [u1-l2](u1-l2-directory-layout.md) 的四层架构：公共 API 层（`_basic.py` 等）只放签名，后端层（`_basic_backend.py` 等）桥接 numpy 与核心。`_ScipyBackend` 把后端层进一步收口——它是一个**统一入口**，让 uarray 只需要认识「一个后端对象」，而真正的实现分散在三个按数学性质划分的模块里（FFT 族 / DCT-DST 族 / Hankel 族）。这种「一个门面 + 三张分表」的结构，使得新增一个变换时只需在对应模块加一个同名函数，`_ScipyBackend` 完全不用改。

#### 4.1.2 核心流程

当 uarray 轮到 `_ScipyBackend` 执行某个多方法 `method`（参数为 `args, kwargs`）时，`__ua_function__` 做的事：

```
输入：method（多方法对象，如 <uarray multimethod 'fft'>）, args, kwargs
  │
  ├─ 取出方法名 name = method.__name__        # 例如 'fft'、'dct'、'fht'
  │
  ├─ 第 1 站：getattr(_basic_backend, name)     # 查 FFT 族
  │     命中 → fn = 这个函数
  │
  ├─ 第 2 站（若上一站为 None）：getattr(_realtransforms_backend, name)   # 查 DCT/DST 族
  │     命中 → fn = 这个函数
  │
  ├─ 第 3 站（若仍为 None）：getattr(_fftlog_backend, name)   # 查 Hankel 族
  │     命中 → fn = 这个函数
  │
  ├─ 若三站都 miss：return NotImplemented        # 让位给下一个后端
  │
  └─ 命中：return fn(*args, **kwargs)            # 调用对应实现
```

要点：

1. **路由依据是名字字符串**，不是多方法对象本身。`method.__name__` 恰好等于各 `*_backend` 模块里函数的名字（如 `'fft'`），所以 `getattr` 能命中。
2. **三站顺序固定**：FFT 族 → DCT/DST 族 → Hankel 族。由于名字在三个模块间无冲突，顺序不影响正确性，只影响查找效率（FFT 族最常用，放第一站）。
3. **找不到就让位**：返回 `NotImplemented` 而非抛错（详见 [u4-l2](u4-l2-backend-management.md)）。
4. **`_ScipyBackend` 本身无状态**：它不保存任何实例属性，`__ua_function__` 是 `@staticmethod`，不依赖 `self`。

#### 4.1.3 源码精读

先看 `_ScipyBackend` 的完整定义（仅 22 行）：

[_backend.py:8-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L8-L29) — 定义默认后端 `_ScipyBackend`，含域声明与方法名路由的 `__ua_function__`。

```python
class _ScipyBackend:
    """The default backend for fft calculations

    Notes
    -----
    We use the domain ``numpy.scipy`` rather than ``scipy`` because ``uarray``
    treats the domain as a hierarchy. ...
    """
    __ua_domain__ = "numpy.scipy.fft"

    @staticmethod
    def __ua_function__(method, args, kwargs):

        fn = getattr(_basic_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_realtransforms_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_fftlog_backend, method.__name__, None)
        if fn is None:
            return NotImplemented
        return fn(*args, **kwargs)
```

逐行拆解：

- `__ua_domain__ = "numpy.scipy.fft"`（[_backend.py:17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L17)）：声明本后端服务的域。文档字符串里专门解释了为什么用 `numpy.scipy.fft` 而不是 `scipy.fft`——**uarray 把域当作层级**，这样用户若安装了一个服务于整个 `numpy` 域的后端，它也能顺便覆盖 `numpy.scipy.fft`。这正是 [u4-l2](u4-l2-backend-management.md) 里 `_backend_from_arg` 校验 `__ua_domain__ == 'numpy.scipy.fft'` 的对照面。
- `@staticmethod` + `def __ua_function__(method, args, kwargs)`（[_backend.py:19-20](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L19-L20)）：声明为静态方法，所以**不接收 `self`**。uarray 调用时传入的是 `(method, args, kwargs)` 三元——分别是「哪个多方法、位置参数元组、关键字参数字典」。因为实现完全无状态，用静态方法即可；无论是 `_ScipyBackend.__ua_function__(...)` 还是实例 `_ScipyBackend().__ua_function__(...)`，访问到的都是同一个纯函数。
- 三级 `getattr` 链（[_backend.py:22-26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L22-L26)）：`getattr(module, name, None)` 的第三个参数 `None` 是默认值——名字不存在时返回 `None` 而非抛 `AttributeError`，从而可以用 `if fn is None` 优雅地链式回退到下一站。
- `return NotImplemented`（[_backend.py:28](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L28)）：三站都 miss 时的让位信号。对 `_ScipyBackend` 而言这几乎不会发生（它覆盖了全部 28 个变换多方法），但保留让位语义让它与其他后端遵循同一协议。
- `return fn(*args, **kwargs)`（[_backend.py:29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L29)）：把原始调用参数**原样**转发给命中的实现函数。注意 `_ScipyBackend` 不做任何参数改写——它只路由。

再看三个被查找的模块各提供了什么名字：

- 第一站 [_basic_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py) 提供 18 个 FFT 族函数，例如 [fft（_basic_backend.py:77-80）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L77-L80) 一行转调 `_execute_1D('fft', _duccfft.fft, ...)`，最终落 `_duccfft`。
- 第二站 [_realtransforms_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms_backend.py) 提供 8 个 DCT/DST 函数（如 [dctn（_realtransforms_backend.py:18-21）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms_backend.py#L18-L21)），都走同一个 `_execute` 适配器（见 [u3-l3](u3-l3-realtransforms-backend.md)）。
- 第三站 [_fftlog_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog_backend.py) 提供 `fht` / `ifht`（[fht（_fftlog_backend.py:14-39）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog_backend.py#L14-L39)）。

> **一个容易踩的细节**：第三站模块 `_fftlog_backend` 的 `__all__` 里还列了 `fhtoffset`（[_fftlog_backend.py:8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog_backend.py#L8)），但 `fhtoffset` **不是多方法**——它是一个普通函数，在 [`_fftlog.py:10`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog.py#L10) 被 `from ._fftlog_backend import fhtoffset` 直接搬进公共命名空间。因此系统中**根本没有名为 `fhtoffset` 的多方法**，`__ua_function__` 永远不会收到 `method.__name__ == 'fhtoffset'`。这说明：不是 `*_backend.py` 里出现的每个名字都会经过分派，只有 `@_dispatch` 装饰过的才会。

最后看 `_ScipyBackend` 是怎么变成「默认后端」的。文件末尾这一行在 import 时执行：

[_backend.py:211](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L211) — 把字符串 `'scipy'` 经 `_named_backends` 翻译成 `_ScipyBackend`，注册为全局兜底后端（`try_last=True`）。

```python
set_global_backend('scipy', try_last=True)
```

而 `_named_backends` 把字符串映射到类（[_backend.py:32-34](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L32-L34)）：

```python
_named_backends = {
    'scipy': _ScipyBackend,
}
```

注意这里存的是**类** `_ScipyBackend` 而非实例。这能工作，是因为 `__ua_domain__` 是类属性、`__ua_function__` 是静态方法——类本身已满足 uarray 协议（与 [tests/mock_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/mock_backend.py) 直接用模块当后端是同一种鸭子类型思路）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `_ScipyBackend.__ua_function__` 的「方法名路由」行为，以及默认后端被注册的事实。

**操作步骤**（源码阅读型，无需运行即可理解；若运行则标注「待本地验证」）：

1. 打开 [_backend.py:8-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L8-L29)，对照三个 `getattr`，填出下表「`method.__name__` → 命中站 → 实现函数」：

   | `method.__name__` | 命中站 | 实现函数所在文件 |
   |-------------------|--------|------------------|
   | `'fft'` | 第 1 站 `_basic_backend` | `_basic_backend.py:77` |
   | `'fftn'` | 第 1 站 `_basic_backend` | `_basic_backend.py:113` |
   | `'dctn'` | 第 2 站 `_realtransforms_backend` | `_realtransforms_backend.py:18` |
   | `'dst'` | 第 2 站 `_realtransforms_backend` | `_realtransforms_backend.py:54` |
   | `'fht'` | 第 3 站 `_fftlog_backend` | `_fftlog_backend.py:14` |

2. 思考：若有人写了一个全新的变换多方法 `@_dispatch def my_transform(x): ...`，但忘了在三个 `*_backend` 模块里加同名实现，`_ScipyBackend.__ua_function__` 会返回什么？（答案见 4.1.5）

3. （可选，待本地验证）在 REPL 里执行，观察默认后端的存在：

   ```python
   import scipy.fft as spf
   from scipy.fft._backend import _ScipyBackend, _named_backends
   print(_named_backends)         # {'scipy': <class '_ScipyBackend'>}
   print(_ScipyBackend.__ua_domain__)   # numpy.scipy.fft
   ```

**需要观察的现象**：路由表是纯按名字的 `getattr` 链；`_ScipyBackend` 自身没有一行计算代码。

**预期结果**：你能用一句话向别人解释——**`_ScipyBackend` 是一张「名字 → 实现模块」的三级路由表，命中就转发、全 miss 就让位。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_ScipyBackend.__ua_function__` 是 `@staticmethod` 而不是普通实例方法？如果改成 `def __ua_function__(self, method, args, kwargs)`，会发生什么？

**参考答案**：因为实现完全无状态、不需要 `self`。uarray 调用时统一传入 `(method, args, kwargs)` 三参；若写成实例方法，Python 的方法绑定会自动把实例塞进第一个参数 `self`，导致参数错位（`method` 收到的其实是后端实例）。用 `@staticmethod` 则无论用类还是实例访问，都拿到这个纯三参函数。

**练习 2**：路由顺序是「FFT → DCT/DST → Hankel」。如果把 `_realtransforms_backend` 放到第一站，会影响结果吗？为什么？

**参考答案**：不会。因为三个模块里的函数名互不重叠（`fft` 只在 `_basic_backend`，`dct` 只在 `_realtransforms_backend`…），`getattr` 对不存在的名字返回 `None` 自动跳到下一站，所以顺序只影响「查找几个模块才命中」的效率，不影响命中的结果。

**练习 3**：`_ScipyBackend` 对一个它能命中的方法，会不会返回 `NotImplemented`？

**参考答案**：不会。只要三站中任一站命中，就走 `return fn(*args, **kwargs)`，返回的是实现函数的真实结果（哪怕结果本身是 `None`）。只有三站全 miss 才返回 `NotImplemented`。由于它覆盖了全部 28 个变换多方法，实际运行中几乎不会让位。

---

### 4.2 NumPyBackend：用 numpy.fft 当参照后端

#### 4.2.1 概念说明

`NumPyBackend` 是一个**调试 / 参照后端**：它把对 scipy.fft 多方法的调用**转交给 `numpy.fft`**，让你能用 NumPy 自带的 FFT 实现来跑同一个调用。它最大的用途是**交叉验证**——当你怀疑 scipy 的 ducc 核心结果有出入时，临时切到 NumPyBackend 跑一遍，对照结果是否一致。

它和 `_ScipyBackend` 一样实现 uarray 协议（`__ua_domain__` + `__ua_function__`），但路由表极简：不是去三个模块查找，而是直接 `getattr(np.fft, method.__name__)`——因为 `numpy.fft` 恰好也用相同的名字（`fft`、`ifft`、`rfft`…）暴露这些函数。

> 关键局限：`numpy.fft` **只提供 FFT 族**（`fft`/`ifft`/`rfft`/`irfft`/`hfft`/`ihfft` 及 2-D/n-D 变体，外加 `fftfreq`/`fftshift` 等 helper）。它**没有** `dct`/`dst`/`fht`。所以对 `dct` 调用 NumPyBackend 时，`getattr(np.fft, 'dct', None)` 返回 `None`，`__ua_function__` 返回 `NotImplemented`，uarray 自动让位回退到下一个后端（如 scipy）。这正是「让位」机制的现实价值。

#### 4.2.2 核心流程

```
输入：method, args, kwargs
  │
  ├─ kwargs.pop("overwrite_x", None)     # 丢掉 numpy.fft 不认的参数
  │
  ├─ fn = getattr(np.fft, method.__name__, None)
  │
  ├─ 若 fn is None：return NotImplemented   # numpy.fft 没这个方法 → 让位
  │
  └─ return fn(*args, **kwargs)             # 用 numpy.fft 跑
```

要点：

1. **先剥掉 `overwrite_x`**：`numpy.fft.fft` 等不接受 scipy 专有的 `overwrite_x` 参数，所以 `pop` 掉它，避免 `TypeError`。
2. **找不到就让位**：与 `_ScipyBackend` 同样的 `NotImplemented` 语义。
3. **不剥 `workers`/`plan`**：这是有意为之的「轻量」设计——NumPyBackend 主要服务于「用默认参数快速对照」的场景；若调用方显式传了 `workers`/`plan`，它们仍会被转发，`numpy.fft` 可能因此报错。这提醒我们：NumPyBackend 是**调试工具**而非生产后端。

#### 4.2.3 源码精读

整个 `NumPyBackend` 只有 11 行：

[_debug_backends.py:3-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L3-L13) — 调试后端 `NumPyBackend`，把调用转给 `numpy.fft`。

```python
class NumPyBackend:
    """Backend that uses numpy.fft"""
    __ua_domain__ = "numpy.scipy.fft"

    @staticmethod
    def __ua_function__(method, args, kwargs):
        kwargs.pop("overwrite_x", None)

        fn = getattr(np.fft, method.__name__, None)
        return (NotImplemented if fn is None
                else fn(*args, **kwargs))
```

逐行拆解：

- `__ua_domain__ = "numpy.scipy.fft"`（[_debug_backends.py:5](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L5)）：与 `_ScipyBackend` 同域，所以它能被 `set_backend` / `register_backend` 识别，也通得过 [_backend.py:46-47](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L46-L47) 的 `_backend_from_arg` 校验。
- `kwargs.pop("overwrite_x", None)`（[_debug_backends.py:9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L9)）：第二个参数 `None` 是默认值——`kwargs` 里没有 `overwrite_x` 也不报错，静默返回 `None`。这样无论调用方是否传了 `overwrite_x`，都能安全剥离。
- `fn = getattr(np.fft, method.__name__, None)`（[_debug_backends.py:11](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L11)）：与 `_ScipyBackend` 完全相同的「按名字取函数」思路，只是查找对象换成了 `np.fft`。这能成立的前提是 scipy.fft 与 numpy.fft 对 FFT 族**用了相同的函数名**——这正是 scipy.fft 被设计为「numpy.fft 超集」的体现（见 [u1-l1](u1-l1-overview.md)）。
- `return (NotImplemented if fn is None else fn(*args, **kwargs))`（[_debug_backends.py:12-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L12-L13)）：三元表达式——numpy.fft 没有该方法就让位，否则用 numpy.fft 执行。

#### 4.2.4 代码实践

**实践目标**：用 `NumPyBackend` 跑一次 `fft`，验证结果与直接调用 `numpy.fft.fft` 完全一致，体会「参照后端」的用法。

**操作步骤**（待本地验证）：

```python
import numpy as np
import scipy.fft as spf
from scipy.fft._debug_backends import NumPyBackend   # 必须显式导入

x = np.arange(8)

# 1) 切到 NumPyBackend 跑 fft
with spf.set_backend(NumPyBackend()):
    y_via_backend = spf.fft(x)

# 2) 直接用 numpy.fft 跑
y_direct = np.fft.fft(x)

print("一致？", np.array_equal(y_via_backend, y_direct))
```

**需要观察的现象**：

- `y_via_backend` 与 `y_direct` 应当**逐元素相等**（不只是 `allclose`），因为 NumPyBackend 内部调用的就是同一个 `np.fft.fft`。
- 由于 `set_backend(NumPyBackend())` 是局部后端、优先级最高，本次 `spf.fft(x)` 会**先于** scipy 默认后端被尝试，且因为 `np.fft.fft` 存在而直接命中。

**预期结果**：打印 `一致？ True`。

**再试一个让位场景**（待本地验证）：

```python
# numpy.fft 没有 dct → NumPyBackend 让位 → 回退到 scipy 默认后端
with spf.set_backend(NumPyBackend()):
    y = spf.dct(x)            # 不会报错，结果来自 scipy
print("dct 仍可用：", y is not None)
```

**预期结果**：`dct 仍可用： True`。因为 NumPyBackend 对 `dct` 返回 `NotImplemented`，uarray 回退到全局兜底的 `_ScipyBackend`，由 ducc 核心算出 DCT。

#### 4.2.5 小练习与答案

**练习 1**：`NumPyBackend` 为什么要 `kwargs.pop("overwrite_x", None)`，而 `_ScipyBackend` 不需要？

**参考答案**：因为 `_ScipyBackend` 路由到的 `_basic_backend.fft` 等函数**本身就接受** `overwrite_x` 参数（见 [_basic_backend.py:77-80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L77-L80)）；而 `NumPyBackend` 转给的 `numpy.fft.fft` **不接受** `overwrite_x`，若不剥掉会触发 `TypeError`。`pop` 的第二参数 `None` 保证「没传也不报错」。

**练习 2**：若调用 `spf.fft(x, workers=4)` 时切到了 `NumPyBackend`，会发生什么？这说明了什么？

**参考答案**：`workers=4` 会被原样转发给 `np.fft.fft`，而 `numpy.fft.fft` 不接受 `workers`，于是抛 `TypeError`。这说明 `NumPyBackend` 是**轻量调试后端**，只适配「默认参数 / scipy 专有参数已被妥善处理」的常见情形，并非完备的生产后端——也解释了它为何放在 `_debug_backends.py`。

**练习 3**：用 `set_global_backend(NumPyBackend())` 把它设为全局后端，再调用 `spf.dct(x)`，结果由谁算出？为什么？

**参考答案**：仍由 scipy 的 `_ScipyBackend`（ducc）算出。因为 NumPyBackend 对 `dct` 返回 `NotImplemented` 让位；而 `set_global_backend` 只是设置全局兜底，[u4-l2](u4-l2-backend-management.md) 讲过——当唯一的全局后端让位且无其他后端时，uarray 会继续尝试，最终由能处理 `dct` 的后端承担（若设置了 `try_last=True` 的 scipy 注册，scipy 仍在候选名单里）。（具体回退路径「待本地验证」，但「dct 不会由 NumPyBackend 算」是确定的。）

---

### 4.3 EchoBackend：打印分派参数的探针后端

#### 4.3.1 概念说明

`EchoBackend` 是最极简的**探针后端**：它不做任何计算，只把 uarray 传给它的 `method`、`args`、`kwargs` **原样打印**出来。它的用途是**调试分派本身**——当你想知道「后端到底收到了哪个方法对象？收到了哪些参数？」时，挂上 EchoBackend 跑一次就能看清楚。

这是排查「参数有没有被正确转发」「某个多方法是否被调用」「后端优先级是否如预期」的最直接手段。它的存在也说明了一件事：uarray 后端的 `__ua_function__` 可以是一个**纯副作用函数**——协议不要求它真去算 FFT，只要它返回值或 `NotImplemented` 即可。

#### 4.3.2 核心流程

```
输入：method, args, kwargs
  │
  └─ print(method, args, kwargs, sep='\n')   # 三行分别打印方法、位置参数、关键字参数
     （返回 None）
```

要点：

1. **只打印，不计算**：`__ua_function__` 的返回值是 `print(...)` 的返回值，即 `None`。
2. **`None` 不是 `NotImplemented`**：所以 uarray 认为「本后端成功处理了」，不会再回退——本次调用的「结果」就是 `None`。这是用 EchoBackend 时必须知道的副作用（见 4.3.4）。
3. **`sep='\n'`**：三个对象分三行打印，便于阅读 `method`（多方法的 repr）、`args`（元组）、`kwargs`（字典）。

#### 4.3.3 源码精读

整个 `EchoBackend` 只有 7 行，是全仓最短的后端：

[_debug_backends.py:16-22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L16-L22) — 探针后端 `EchoBackend`，只打印分派参数。

```python
class EchoBackend:
    """Backend that just prints the __ua_function__ arguments"""
    __ua_domain__ = "numpy.scipy.fft"

    @staticmethod
    def __ua_function__(method, args, kwargs):
        print(method, args, kwargs, sep='\n')
```

逐行拆解：

- `__ua_domain__ = "numpy.scipy.fft"`（[_debug_backends.py:18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L18)）：同样满足协议，可被 `set_backend` 接受。
- `print(method, args, kwargs, sep='\n')`（[_debug_backends.py:22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L22)）：
  - `method` 是多方法对象，其 `repr` 形如 `<uarray multimethod 'fft'>`，`__name__` 为 `'fft'`。
  - `args` 是位置参数元组，`kwargs` 是关键字参数字典。
  - `sep='\n'` 让三者各占一行，输出形如：

    ```
    <uarray multimethod 'fft'>
    ([1, 2, 3, 4],)
    {}
    ```

  这三行就完整回答了「后端收到了什么」。

> 对比三种后端的 `__ua_function__` 写法，能看出 uarray 协议的灵活：
> - `_ScipyBackend`：三级 `getattr` 链，按名字路由到实现模块（[_backend.py:20-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L20-L29)）。
> - `NumPyBackend`：单级 `getattr(np.fft, name)`，转交 numpy.fft（[_debug_backends.py:8-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L8-L13)）。
> - `EchoBackend`：不查找、不计算，只打印（[_debug_backends.py:21-22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L21-L22)）。
> - 测试里的 [mock_backend.py:93-96](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/mock_backend.py#L93-L96) 则用**字典** `_implements.get(method)`（以多方法对象本身为键）做路由——与 `_ScipyBackend` 的「按名字」形成对照。两种查找方式都对，因为多方法的 `__name__` 与各实现同名。

#### 4.3.4 代码实践

**实践目标**：用 `EchoBackend` 观察一次 `fft` 调用，看清 uarray 传给后端的 `method/args/kwargs`；并体会「EchoBackend 返回 None」的副作用。

**操作步骤**（待本地验证）：

```python
import scipy.fft as spf
from scipy.fft._debug_backends import EchoBackend   # 必须显式导入

with spf.set_backend(EchoBackend()):
    result = spf.fft([1, 2, 3, 4])

print("返回值：", result)
```

**需要观察的现象**：

- 标准输出会先打印三行（来自 EchoBackend 的 `print`），分别是：
  - 第 1 行：多方法对象，如 `<uarray multimethod 'fft'>`；
  - 第 2 行：位置参数元组，如 `([1, 2, 3, 4],)`；
  - 第 3 行：关键字参数字典，如 `{}`。
- 随后打印 `返回值： None`——因为 EchoBackend 的 `__ua_function__` 返回的是 `print(...)` 的结果 `None`，且 `None` 不是 `NotImplemented`，所以 uarray 认为本次分派「成功」，结果就是 `None`。

**预期结果**：

```
<uarray multimethod 'fft'>
([1, 2, 3, 4],)
{}
返回值： None
```

（具体 repr 文本「待本地验证」，但「三行打印 + 返回 None」的形态是确定的。）

**进阶观察**——传入关键字参数，看 `kwargs` 如何变化（待本地验证）：

```python
with spf.set_backend(EchoBackend()):
    spf.fft([1, 2, 3, 4], n=8, norm='ortho')
```

**预期结果**：`kwargs` 那一行应体现 `{'n': 8, 'norm': 'ortho'}`（或位置参数变化），具体哪些进 `args`、哪些进 `kwargs` 取决于多方法如何绑定调用——这正是 EchoBackend 能帮你查清的事。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `with spf.set_backend(EchoBackend()): spf.fft(...)` 的返回值是 `None`，而不是报错或回退到 scipy？

**参考答案**：EchoBackend 的 `__ua_function__` 返回 `print(...)` 的值即 `None`。在 uarray 协议里，**只有返回 `NotImplemented` 才表示「让位」**；返回 `None` 被视为「本后端已处理，结果就是 None」。EchoBackend 作为局部后端优先级最高且「成功返回」，于是不再回退，调用结果为 `None`。

**练习 2**：若希望「既打印参数、又让 scipy 真正算出结果」，该怎样改造 EchoBackend？

**参考答案**：在 `__ua_function__` 里先 `print(...)`，再把调用转发给 scipy 的实现，例如：

```python
# 示例代码（非项目原有）
@staticmethod
def __ua_function__(method, args, kwargs):
    print(method, args, kwargs, sep='\n')
    from scipy.fft._backend import _ScipyBackend   # 示例：复用默认后端
    return _ScipyBackend.__ua_function__(method, args, kwargs)
```

这样既保留探针的打印，又返回真实结果。注意这是**示例代码**，项目自带的 EchoBackend 不做转发。

**练习 3**：EchoBackend 和 NumPyBackend 都没有出现在 `scipy.fft.__all__` 里。为什么？

**参考答案**：因为它们是**调试工具**而非稳定公共 API。`__init__.py` 只把生产用函数与管理 API 导入公共命名空间（见 [__init__.py:86-97](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L86-L97)）；调试后端放在 `_debug_backends.py`（下划线前缀表私有），需要者显式 `from scipy.fft._debug_backends import ...` 导入。这种「公共 API 干净、调试工具按需导入」的分层，避免了普通用户误用调试后端导致结果变成 `None`。

---

## 5. 综合实践

**任务**：用本讲三个后端（`_ScipyBackend`、`NumPyBackend`、`EchoBackend`）串起一次完整的「分派观察 + 交叉验证」流程，把 [u4-l2](u4-l2-backend-management.md) 的优先级模型与本讲的路由表打通。

**操作步骤**（待本地验证）：

1. **先探针，看清参数**：用 EchoBackend 观察 `spf.fft(x, n=8)` 收到的 `method/args/kwargs`，确认 `n=8` 确实被转发到后端。

   ```python
   import numpy as np
   import scipy.fft as spf
   from scipy.fft._debug_backends import EchoBackend, NumPyBackend

   x = np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=float)

   print("=== EchoBackend 探针 ===")
   with spf.set_backend(EchoBackend()):
       spf.fft(x, n=8)
   ```

2. **再用 NumPyBackend 交叉验证**：把 fft 切到 numpy 跑，与 scipy 默认结果对照。

   ```python
   # scipy 默认后端的结果
   y_scipy = spf.fft(x, n=8)

   # NumPyBackend 的结果
   with spf.set_backend(NumPyBackend()):
       y_numpy = spf.fft(x, n=8)

   print("scipy vs numpy 一致：", np.allclose(y_scipy, y_numpy))
   ```

3. **观察让位**：用 NumPyBackend 调 `dct`，确认它让位、由 scipy 兜底算出（可结合 EchoBackend 在前观察「NumPyBackend 是否被调用过」——这需要自定义计数后端，作为进阶）。

4. **梳理一张对照表**：

   | 后端 | `__ua_function__` 做什么 | 对 `fft` | 对 `dct` |
   |------|--------------------------|----------|----------|
   | `_ScipyBackend` | 三级 `getattr` 路由到 `*_backend` | 命中 `_basic_backend.fft` → ducc | 命中 `_realtransforms_backend.dct` → ducc |
   | `NumPyBackend` | `getattr(np.fft, name)` | 命中 `np.fft.fft` | 让位（`np.fft` 无 `dct`）→ 回退 scipy |
   | `EchoBackend` | 只 `print`，返回 `None` | 打印后返回 `None` | 打印后返回 `None` |

**需要观察的现象 / 预期结果**：

- 步骤 1：EchoBackend 打印出方法名 `fft`、参数 `x` 与 `{'n': 8}`。
- 步骤 2：`scipy vs numpy 一致： True`（数值上 allclose）。
- 步骤 3：NumPyBackend 下 `dct` 不报错，结果由 scipy 算出（数值正确）。
- 步骤 4：三种后端对同一调用的「处理 / 让位」差异一目了然。

**提示**：

- 调试后端必须显式 `from scipy.fft._debug_backends import ...` 导入。
- EchoBackend 返回 `None`，所以不要在它的 `with` 块里把返回值当成真实 FFT 结果使用。
- 实践结束后无需复位——`set_backend` 是上下文管理器，离开 `with` 块自动还原；若你用了 `set_global_backend`，记得用 `set_global_backend('scipy', try_last=True)` 复位（见 [u4-l2](u4-l2-backend-management.md)）。

完成本实践后，你应该能用一句话向别人解释：**scipy.fft 的默认后端是一张按方法名路由的三级表，调试后端则用极简路由（转 numpy 或只打印）让你看清分派、对照结果。**

---

## 6. 本讲小结

- `_ScipyBackend` 是 scipy.fft 的**默认后端**，由 [_backend.py:211](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L211) 的 `set_global_backend('scipy', try_last=True)` 在 import 时注册为全局兜底。
- 它的 `__ua_function__` 是一个**无状态的方法名路由器**：用 `method.__name__` 在 `_basic_backend` → `_realtransforms_backend` → `_fftlog_backend` 三站依次 `getattr`，命中就 `fn(*args, **kwargs)` 转发，全 miss 就返回 `NotImplemented` 让位（[_backend.py:20-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L20-L29)）。
- 真正计算仍由三个 `*_backend` 模块下沉到 `_duccfft`；`_ScipyBackend` 自身没有一行计算代码——它是「一个门面 + 三张分表」的统一入口。
- `NumPyBackend`（[_debug_backends.py:3-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L3-L13)）是**参照后端**：`getattr(np.fft, name)` 把调用转给 numpy.fft，用于交叉验证；它会 `pop` 掉 numpy 不认的 `overwrite_x`，对 numpy 没有的方法（如 `dct`）返回 `NotImplemented` 让位。
- `EchoBackend`（[_debug_backends.py:16-22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py#L16-L22)）是**探针后端**：只 `print(method, args, kwargs)`、返回 `None`，用来观察后端到底收到了什么；注意 `None` 不是 `NotImplemented`，故不会回退。
- 两个调试后端都**不在公共命名空间**（`__init__.py` 未导入 `_debug_backends`），需 `from scipy.fft._debug_backends import ...` 显式导入；它们与测试里的 [mock_backend](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/mock_backend.py) 一起，展示了 uarray 协议「按名字 / 按字典 / 只打印」等多种 `__ua_function__` 写法。

---

## 7. 下一步学习建议

- 想看清「优先级队列到底把后端排成什么顺序」，回看 [u4-l2](u4-l2-backend-management.md)，并用本讲的 EchoBackend 做实验，亲眼验证 `set_backend` / `skip_backend` 的影响。
- 进入 [u5-l1](u5-l1-ducc-basic-kernels.md)「ducc 内核：c2c / r2c / c2r」——顺着 `_ScipyBackend` 路由到的 `_basic_backend.fft`，往下钻进 `_duccfft` 看真正的 C 扩展 `pyduccfft` 如何算 FFT。
- 想亲手写一个合格的后端，跳到 [u8-l2](u8-l2-custom-backend.md)「实战：编写并注册自定义第三方后端」，那里会综合运用本讲的 `__ua_domain__`/`__ua_function__` 协议与 [u4-l2](u4-l2-backend-management.md) 的 `set_backend`/`register_backend`/`NotImplemented` 回退。
- 建议同步通读 [_debug_backends.py](_debug_backends.py)（仅 23 行）与 [_backend.py](_backend.py) 的 `_ScipyBackend`（22 行），这两个极短文件是理解「后端 = 路由表」心智模型的最佳样本。
