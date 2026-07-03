# 后端管理：set_backend / set_global_backend / register_backend / skip_backend

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `set_backend`、`skip_backend`、`set_global_backend`、`register_backend` 四个 API 的**作用域**差异（临时 vs 永久、局部 vs 全局）。
- 复述 uarray 的**优先级模型**：当多个后端同时存在时，谁先被尝试、谁最后兜底。
- 解释 `only`、`coerce`、`try_last` 三个标志如何改变默认的尝试与回退行为。
- 看懂 `scipy.fft` 在 import 时执行的那一行 `set_global_backend('scipy', try_last=True)` 为什么让「scipy 默认后端反而排在最后」。
- 理解 `_backend_from_arg` 如何把字符串 `'scipy'` 翻译成后端对象、以及它对 `__ua_domain__` 的硬性校验。

本讲承接 [u4-l1](u4-l1-uarray-dispatch.md) 已建立的「函数即协议 / Dispatchable / domain」认知，回答下一个自然问题：**既然计算要靠后端，那后端到底由谁、按什么顺序被选中？**

---

## 2. 前置知识

### 2.1 回顾：后端是什么

在 [u4-l1](u4-l1-uarray-dispatch.md) 中我们看到，`scipy.fft.fft` 的函数体只有一行 `return (Dispatchable(x, np.ndarray),)`——它不计算 FFT，只是声明「我有一个可被替换的数组参数」。真正计算 FFT 的是**后端（backend）**：一个实现了 uarray 协议的对象，提供 `__ua_domain__`（声明自己属于哪个域）和 `__ua_function__`（声明自己如何执行某个多方法）。

scipy.fft 内置一个默认后端 `_ScipyBackend`，它最终调用 C 扩展 `pyduccfft` 真正算 FFT。但 uarray 的设计允许**同时存在多个后端**，并由一套「优先级 + 回退」规则决定用哪个。

### 2.2 本讲要用到的两个术语

- **作用域（scope）**：一个后端设置「活多久」。临时后端只在 `with` 块内有效；永久后端会一直存在，直到被显式覆盖。
- **回退（fallback）**：当被尝试的后端对某个调用返回 `NotImplemented`（表示「我不会/我不接这个活」），uarray 会**自动尝试下一个**后端；只有所有后端都拒绝时，才抛 `BackendNotImplementedError`。

> 关键直觉：后端不是「唯一实现」，而是「一条按优先级排好队的候选名单」。`NotImplemented` 是「让位」信号，不是错误。

---

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开：

| 文件 | 作用 |
|------|------|
| [`_backend.py`](_backend.py) | 定义四个后端控制 API、`_backend_from_arg` 校验、`_named_backends` 命名映射，以及默认后端 `_ScipyBackend`；末尾在 import 时注册 scipy 为全局后端。 |
| `scipy/_lib/_uarray/_uarray_dispatch.cxx` | uarray 的 C++ 分派核心，**优先级规则的真正出处**。本讲用它佐证「谁先谁后」。 |
| `tests/test_backend.py` | 后端测试，`with set_backend(mock_backend, only=True)` 是本讲实践的代码依据。 |

四个 API 在 [`__init__.py:95-96`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L95-L96) 被重新导出，所以你可以直接写 `scipy.fft.set_backend(...)`。

---

## 4. 核心概念与源码讲解

### 4.1 set_backend / skip_backend：局部作用域的上下文管理器

#### 4.1.1 概念说明

`set_backend` 和 `skip_backend` 都是**上下文管理器**（用 `with` 使用）。它们改变后端名单的效果**只在 `with` 块内生效**，离开块后名单恢复原状。这对临时切换实现非常安全——比如你只想在一段代码里用一个实验性后端，而不污染全局状态。

- `set_backend(b)`：把后端 `b` **临时加入**名单，且**优先级最高**（局部后端总是最先被尝试）。
- `skip_backend(b)`：在块内**临时屏蔽**后端 `b`，使其不被尝试；离开块后恢复。它同时屏蔽「局部设置的后端」和「全局/注册的后端」。

#### 4.1.2 核心流程

`set_backend` 的执行过程可以概括为：

```
进入 with 块
  → 把 backend 压入「局部后端栈」(pref)，优先级最高
  → 块内调用 fft：局部后端先被尝试
离开 with 块
  → 弹出该 backend，名单恢复进入前状态
```

`skip_backend` 类似，只是把 backend 加入「临时跳过集合」，分派时跳过它。

#### 4.1.3 源码精读

`set_backend` 的 Python 包装很薄——校验后直接委托给 uarray：

[_backend.py:138-173](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L138-L173) —— `set_backend` 接受 `backend`、`coerce`、`only` 三个参数，先用 `_backend_from_arg` 校验，再返回 `ua.set_backend(...)` 产生的上下文管理器。

关键点：

- 它**返回**一个上下文管理器（`return ua.set_backend(...)`），所以必须配合 `with` 使用；若不 `with`，它不会真正切换后端。
- `only` 参数（默认 `False`）：若为 `True`，且该后端返回 `NotImplemented`，则**立即抛错**，不再尝试更低优先级的后端。
- `coerce` 参数（默认 `False`）：允许「昂贵的类型转换」（例如把 numpy 数组拷到 GPU），且**隐含 `only=True`**（见 [docstring 第 154 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L152-L158)：「Implies `only`」）。

`skip_backend` 结构相同，但没有 `coerce`/`only` 参数——屏蔽就是屏蔽：

[_backend.py:176-208](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L176-L208) —— `skip_backend` 校验后返回 `ua.skip_backend(...)`。其 docstring 的示例（第 199-205 行）直接展示了「跳过 scipy 后导致没有任何可用实现」从而抛 `BackendNotImplementedError` 的场景。

> 在 C++ 分派核心中，「跳过」由 `skipped` 集合和 `should_skip` 谓词实现：每个层级的后端在尝试前都会先过一遍 `should_skip`，命中则 `continue`。见 [_uarray_dispatch.cxx:974-987](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L974-L987)。

#### 4.1.4 代码实践

**实践目标**：体会 `set_backend`/`skip_backend` 的临时作用域，以及 `skip_backend` 触发 `BackendNotImplementedError` 的回退终止。

**操作步骤**：

```python
# 示例代码
import scipy.fft as sfft
from uarray import BackendNotImplementedError   # 若未安装 uarray，可改:
# from scipy._lib.uarray import BackendNotImplementedError
import numpy as np

x = np.arange(8)

# (a) 局部强制只用 scipy 后端
with sfft.set_backend('scipy', only=True):
    y = sfft.fft(x)
print("set_backend('scipy', only=True) ->", y[:3])

# (b) 局部屏蔽 scipy：默认情况下 scipy 是唯一后端，屏蔽后无人兜底
try:
    with sfft.skip_backend('scipy'):
        y = sfft.fft(x)
except BackendNotImplementedError as e:
    print("skip_backend('scipy') 触发:", type(e).__name__)
```

**需要观察的现象**：

- (a) 正常打印 FFT 结果前三个值（约 `[28.-0.j, -4.+9.66j, -4.+4.j]`）。
- (b) 打印出 `skip_backend('scipy') 触发: BackendNotImplementedError`。

**预期结果**：`only=True` 时 scipy 成功实现 fft，不会回退；`skip_backend` 把唯一的 scipy 后端排除后，整条候选名单耗尽，uarray 抛 `BackendNotImplementedError`。

**说明**：本实践行为可直接由 [`_backend.py:199-205`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L199-L205) 的官方 docstring 示例佐证；具体数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果不写 `with`，直接调用 `sfft.set_backend('scipy', only=True)`（不进入上下文），后续 `sfft.fft(x)` 会受影响吗？

**参考答案**：不会生效。`set_backend` 返回的是一个上下文管理器对象，必须用 `with` 进入才会真正把后端压入局部栈。直接调用只是创建并丢弃了该上下文管理器，没有 `__enter__`。

**练习 2**：在 `with set_backend('scipy', only=True)` 内，如果某次调用 scipy 后端返回了 `NotImplemented`，会发生什么？

**参考答案**：因为 `only=True`，uarray 在该后端尝试失败后**立即停止**（C++ 中的 `break`，见 [_uarray_dispatch.cxx:1002-1003](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1002-L1003)），不再尝试更低优先级的后端，直接抛 `BackendNotImplementedError`。

---

### 4.2 set_global_backend / register_backend：永久作用域与默认设置

#### 4.2.1 概念说明

与上一节的「临时」相对，这两个 API 是**永久**的：调用后效果一直存在，除非再次覆盖。它们的区别在于**在候选名单中的排位**：

- `set_global_backend(b)`：设置**全局后端**。全局只有一个，后设置的覆盖前者。
- `register_backend(b)`：把后端加入**注册列表**。可以注册多个。

二者都不需要 `with`，是「设置一次、长期有效」的全局配置。

#### 4.2.2 核心流程

二者的排位关系（`try_last` 取默认 `False` 时）如下，优先级**从高到低**：

```
局部后端 (set_backend)  >  全局后端 (set_global_backend)  >  注册后端 (register_backend)
```

但 scipy.fft 在 import 时做了一个关键设置（见 4.2.3），把全局后端的 `try_last` 设为 `True`，使排位变成：

```
局部后端 (set_backend)  >  注册后端 (register_backend)  >  全局后端 scipy (try_last=True)
```

也就是说：**在 scipy.fft 里，你 register 的后端会比默认的 scipy 后端更优先**。这是为了让 CuPy/PyTorch 这类加速后端一旦注册，就能自动抢在 CPU 的 scipy 之前执行。

#### 4.2.3 源码精读

`set_global_backend` 的签名与参数：

[_backend.py:52-94](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L52-L94) —— `set_global_backend(backend, coerce=False, only=False, try_last=False)`。注意它比 `set_backend` 多了一个 `try_last` 参数。

- `try_last`（默认 `False`）：若为 `True`，全局后端会**排在注册后端之后**被尝试。docstring 原文（[第 72-73 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L72-L73)）：「If True, the global backend is tried after registered backends.」

`register_backend` 没有这些标志，只接受 `backend`：

[_backend.py:97-135](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L97-L135) —— docstring 明确（[第 102-103 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L102-L103)）：「Registered backends have the lowest priority and will be tried after the global backend.」注意这句描述的是 `try_last=False` 的默认情形；scipy.fft 自己用了 `try_last=True`，所以实际顺序被翻转了。

**最关键的一行——import 时的默认设置**：

[_backend.py:211](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L211) —— 模块最后直接执行 `set_global_backend('scipy', try_last=True)`。这意味着：**只要你 `import scipy.fft`，scipy 就被注册为全局后端，且 try_last=True**。这就是为什么平时调用 `scipy.fft.fft` 能正常工作——scipy 后端作为最后的兜底存在；同时它排在最后，给未来注册的加速后端让出优先位置。

> 这是一个容易踩坑的点：很多人以为「全局后端 = 最先被尝试」，但在 scipy.fft 里恰好相反——全局后端 scipy 因为 `try_last=True` 而**最后**被尝试。

#### 4.2.4 代码实践

**实践目标**：用 docstring 给出的 `NoopBackend` 思路，验证「全局后端拒绝时回退到注册后端」。

**操作步骤**：

```python
# 示例代码
import scipy.fft as sfft
import numpy as np

# 一个「什么都不干」的后端：对所有调用返回 NotImplemented
class NoopBackend:
    __ua_domain__ = "numpy.scipy.fft"
    @staticmethod
    def __ua_function__(func, args, kwargs):
        return NotImplemented

x = np.arange(8)

# 把 noop 设为全局；再把 scipy 注册进去
sfft.set_global_backend(NoopBackend())
sfft.register_backend('scipy')
print("回退结果:", sfft.fft(x)[:3])

# 收尾：恢复默认
sfft.set_global_backend('scipy', try_last=True)
```

**需要观察的现象**：尽管全局后端是 `NoopBackend`（返回 `NotImplemented`），`fft(x)` 仍能算出正确结果。

**预期结果**：分派先尝试全局 `NoopBackend` → 得到 `NotImplemented` → 回退到注册的 `scipy` 后端 → 返回正确 FFT。这复刻了 [`_backend.py:120-131`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L120-L131) 的官方示例。具体数值「待本地验证」。

> 注意：本例把 scipy `register_backend` 进去只是为了演示回退；离开脚本前务必 `set_global_backend('scipy', try_last=True)` 还原，避免污染同进程后续计算。

#### 4.2.5 小练习与答案

**练习 1**：为什么 scipy.fft 要用 `try_last=True` 而不是默认的 `try_last=False` 注册自己的后端？

**参考答案**：这样未来用户 `register_backend` 的加速后端（如 CuPy GPU 后端）会排在 scipy 之前被尝试；scipy 作为 CPU 兜底放在最后。若用 `try_last=False`，scipy 全局后端会排在注册后端之前，导致加速后端永远没机会执行（除非 scipy 先返回 `NotImplemented`）。

**练习 2**：连续调用两次 `set_global_backend`，哪个生效？

**参考答案**：后一次覆盖前一次。docstring（[第 80-82 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L80-L82)）：「This will overwrite the previously set global backend.」全局后端只有一个槽位。

---

### 4.3 优先级全景：local → registered → global(try_last) 与 only/coerce/try_last

#### 4.3.1 概念说明

把 4.1、4.2 串起来，uarray 在一次调用里按以下顺序遍历候选后端（**这是本讲的核心**）。优先级从高到低：

1. **局部后端**（来自所有嵌套的 `set_backend`），按「后进先出」尝试——最后 `with` 进来的优先级最高。
2. **注册后端**（来自 `register_backend`），按注册顺序尝试。
3. **全局后端**（来自 `set_global_backend`）：
   - `try_last=False`（uarray 默认）：排在注册后端**之前**；
   - `try_last=True`（scipy.fft 的实际设置）：排在注册后端**之后**。

任何后端返回正常结果即结束；返回 `NotImplemented` 则继续下一个；全部耗尽则抛 `BackendNotImplementedError`。

#### 4.3.2 核心流程

下面是带 `try_last` 分支的完整伪代码（直接对应 C++ 实现）：

```
for backend in reverse(local_backends):     # 局部：后进先出
    if backend in skipped: continue
    result = try(backend)
    if result != NotImplemented: return result
    if backend.only or backend.coerce: break   # only/coerce → 不再回退

if not global.try_last:                       # try_last=False：全局在前
    result = try(global_backend); ...
    if global.only or global.coerce: break

for backend in registered_backends:           # 注册：正序
    if backend in skipped: continue
    result = try(backend); if result != NotImplemented: return result

if global.try_last:                            # try_last=True：全局在最后
    result = try(global_backend); ...

若仍未得到结果 → raise BackendNotImplementedError
```

#### 4.3.3 源码精读

优先级规则的**真正出处**是 C++ 分派核心 `for_each_backend_in_domain`：

[_uarray_dispatch.cxx:967-1047](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L967-L1047) —— 这段代码精确实现了上面的伪代码。几个关键行：

- [第 990 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L990) `for (int i = pref.size() - 1; i >= 0; --i)`：局部后端**逆序**遍历（后进先出）。
- [第 1002-1003 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1002-L1003) `if (options.only || options.coerce) return Break;`：`only` 或 `coerce` 命中即终止回退。
- [第 1021-1028 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1021-L1028)：`try_global_backend_last == false` 时，全局后端在注册后端**之前**尝试。
- [第 1030 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1030) `for (size_t i = 0; ...)`：注册后端**正序**遍历。
- [第 1043-1046 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L1043-L1046)：`try_global_backend_last == true` 时，全局后端在注册后端**之后**（最后）尝试。

三个标志小结：

| 标志 | 可用于 | 作用 |
|------|--------|------|
| `only` | `set_backend`、`set_global_backend` | 该后端若返回 `NotImplemented`，立即停止、抛错，不再回退。 |
| `coerce` | `set_backend`、`set_global_backend` | 允许对 `x` 做昂贵类型转换（如拷到 GPU）；**隐含 `only=True`**。 |
| `try_last` | 仅 `set_global_backend` | 让全局后端排到注册后端之后。scipy.fft 默认 `True`。 |

#### 4.3.4 代码实践

**实践目标**：用一个「会计数」的自定义后端，亲眼看到优先级顺序。

**操作步骤**：

```python
# 示例代码
import scipy.fft as sfft
import numpy as np

calls = []

class CountingBackend:
    def __init__(self, name, implement=True):
        self.name = name
        self.implement = implement
    __ua_domain__ = "numpy.scipy.fft"
    def __ua_function__(self, func, args, kwargs):
        calls.append(self.name)
        return func(*args, **kwargs) if self.implement else NotImplemented

x = np.arange(8)
reg   = CountingBackend("registered")
local = CountingBackend("local")

sfft.register_backend(reg)                 # 注册：优先级中
with sfft.set_backend(local):              # 局部：优先级最高
    sfft.fft(x)
print("尝试顺序:", calls)                  # 预期 ['local']（局部一次就成功）

calls.clear()
with sfft.set_backend(local, only=True):
    sfft.fft(x)
print("only=True 时:", calls)              # 预期仍 ['local']

# 清理
sfft.set_global_backend('scipy', try_last=True)
```

**需要观察的现象**：因为局部后端 `local` 成功实现了 fft，分派在第一个就返回，`calls` 只含 `['local']`，根本没轮到 registered / scipy。

**预期结果**：`['local']`。这说明**高优先级后端一旦成功，低优先级后端不会被尝试**——这正是回退机制「短路」的特性。具体「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把上例 `local.implement` 改为 `False`（返回 `NotImplemented`），不设 `only`，`calls` 会是什么？

**参考答案**：`['local', 'registered', 'scipy']`（scipy 是 try_last 的全局兜底）。`local` 让位 → 尝试 `registered`（成功则止于它，此时若 reg 也 NotImplemented 才轮到 scipy）。具体取决于 `registered` 是否 implement。

**练习 2**：`coerce=True` 为什么隐含 `only=True`？

**参考答案**：`coerce` 意味着已为该后端付出了「昂贵类型转换」的代价（如数组拷到 GPU）。如果它还回退到别的后端，转换就白做了，且可能把数据留在错误的设备上。因此 coerce 后端一旦尝试就「破釜沉舟」，不再回退——这正是 C++ 中 `if (options.only || options.coerce) Break` 把二者并列的原因。

---

### 4.4 _backend_from_arg 与 _named_backends：校验与命名映射

#### 4.4.1 概念说明

四个 API 都允许你传字符串 `'scipy'` 或一个后端对象。这个「字符串 → 对象」的翻译，以及对后端合法性的校验，统一由 `_backend_from_arg` 完成。它背后是一张**命名映射表** `_named_backends`。

#### 4.4.2 核心流程

```
_backend_from_arg(backend):
  if backend 是字符串:
      在 _named_backends 中查表 → 找不到则 ValueError("Unknown backend")
  if backend.__ua_domain__ != "numpy.scipy.fft":
      ValueError("Backend does not implement numpy.scipy.fft")
  return backend
```

两层校验确保：传进 uarray 的后端**一定属于正确的域**，否则 uarray 会因为找不到对应实现而行为异常。

#### 4.4.3 源码精读

命名映射表，目前只有一个条目：

[_backend.py:32-34](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L32-L34) —— `_named_backends = {'scipy': _ScipyBackend}`。所以字符串 `'scipy'` 是当前唯一被识别的快捷名。

校验函数本体：

[_backend.py:37-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L37-L49) —— 注意两点细节：

- 第 46 行用 `backend.__ua_domain__` 直接访问属性。`_ScipyBackend` 的 `__ua_domain__` 是类属性（[第 17 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L17) `"numpy.scipy.fft"`），所以**传类本身**（如 `_named_backends['scipy']` 就是类）也能通过校验——这就是为什么 `set_global_backend('scipy')` 最终传入的是 `_ScipyBackend` 类而非实例，却依然能正常工作（其 `__ua_function__` 是 `staticmethod`）。
- 自定义后端既可传实例也可传类，只要暴露正确的 `__ua_domain__`。

`_ScipyBackend` 本身回顾（[u4-l1](u4-l1-uarray-dispatch.md) 已展开其 `__ua_function__` 如何按方法名在三个 `*_backend` 模块里查找实现）：

[_backend.py:8-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L8-L29) —— 默认后端，`__ua_domain__ = "numpy.scipy.fft"`，`__ua_function__` 依次在 `_basic_backend`/`_realtransforms_backend`/`_fftlog_backend` 中按 `method.__name__` 找实现，找不到则返回 `NotImplemented`（让位给下一个后端）。

#### 4.4.4 代码实践

**实践目标**：触发 `_backend_from_arg` 的两条错误分支，理解校验时机。

**操作步骤**：

```python
# 示例代码
import scipy.fft as sfft

# (a) 未知名字
try:
    sfft.set_backend('cupy-not-installed')
except ValueError as e:
    print("(a)", e)

# (b) 域不对的后端
class WrongDomain:
    __ua_domain__ = "some.other.domain"
    @staticmethod
    def __ua_function__(func, args, kwargs): return NotImplemented
try:
    sfft.set_backend(WrongDomain())
except ValueError as e:
    print("(b)", e)
```

**需要观察的现象**：两条都应在**进入上下文之前**（即调用 `set_backend` 时、还未 `with`）就抛 `ValueError`，而不是等到真正调用 `fft` 时。

**预期结果**：

- (a) `Unknown backend cupy-not-installed`
- (b) `Backend does not implement "numpy.scipy.fft"`

**说明**：校验发生在 `_backend_from_arg` 内（[第 44、47 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L44-L47)），属于「快速失败」——避免把非法后端塞进 uarray 后再产生难以追踪的错误。具体「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_backend_from_arg` 必须检查 `__ua_domain__`，而不是信任调用者传对？

**参考答案**：uarray 按 domain 路由多方法。如果一个后端声明属于别的 domain，却被强行塞进 `numpy.scipy.fft` 域的分派链，uarray 不会调用它的 `__ua_function__`（domain 不匹配），导致行为静默错误或意外回退。提前校验把这类错误变成显式的 `ValueError`。

**练习 2**：`_named_backends` 目前只有 `'scipy'`。如果第三方包想让自己的后端支持 `set_backend('myname')` 这种字符串写法，该怎么做？

**参考答案**：`_named_backends` 是模块级私有字典，scipy.fft 未提供公开注册入口。第三方通常直接传后端**对象/类**（`set_backend(MyBackend())`），而非字符串名。字符串快捷方式目前仅 scipy 自己用。

---

## 5. 综合实践

**任务**：编写一个脚本，用「计数后端」把本讲的优先级模型完整跑一遍，验证以下断言并记录每次 `calls` 列表：

1. 默认状态下（不设任何后端），`fft` 由全局 `scipy`（try_last）兜底。
2. `register_backend(B)` 后，`B` 比 scipy 先被尝试。
3. `with set_backend(L)` 包裹后，`L` 比 registered 和 scipy 都先。
4. `with set_backend(L, only=True)` 且 `L` 返回 `NotImplemented` 时，**不回退**，直接抛 `BackendNotImplementedError`。
5. `with skip_backend('scipy')` 在没有其他实现时抛 `BackendNotImplementedError`。

**提示**：

- 用 4.3.4 的 `CountingBackend`，给不同实例设不同 `name`。
- 每个场景前后注意 `set_global_backend('scipy', try_last=True)` 复位，避免状态串扰。
- 对断言 4、5，用 `try/except BackendNotImplementedError` 捕获并打印。

**预期产出**：一张「场景 → 尝试顺序 / 结果」的表格，例如：

| 场景 | calls（尝试顺序） | 结果 |
|------|------------------|------|
| 默认 | `['scipy']` | 成功 |
| register B | `['B', 'scipy']`（B 成功则止于 B） | 成功 |
| with set_backend L | `['L']` | 成功 |
| L NotImplemented + only=True | `['L']` | `BackendNotImplementedError` |
| skip_backend('scipy') | `[]` | `BackendNotImplementedError` |

（具体是否止步于某个后端取决于该后端 `implement` 设置；表格为「待本地验证」的预期形态。）

完成本实践后，你应该能用一句话向别人解释：**scipy.fft 的后端是一条「局部 > 注册 > 全局(try_last)」的候选队列，`NotImplemented` 让位、`only/coerce` 终止、`skip` 排除。**

---

## 6. 本讲小结

- scipy.fft 用四个 API 管理后端：`set_backend`/`skip_backend` 是**临时**上下文管理器（局部，最高优先级）；`set_global_backend`/`register_backend` 是**永久**设置（全局唯一 / 可多个）。
- 优先级模型（`_uarray_dispatch.cxx` 的 `for_each_backend_in_domain` 为权威）：**局部（后进先出）→ 注册（正序）→ 全局**；全局后端的位置由 `try_last` 决定，`False` 时在前、`True` 时在后。
- **scipy.fft 在 import 时执行 `set_global_backend('scipy', try_last=True)`**（`_backend.py:211`），所以默认的 scipy 后端反而**最后**被尝试——把优先位置让给注册进来的加速后端。
- 三个标志：`only`（失败即终止回退）、`coerce`（允许昂贵类型转换且隐含 `only`）、`try_last`（仅全局后端可用，scipy 默认开）。
- 后端返回 `NotImplemented` 表示「让位」，uarray 自动尝试下一个；全部拒绝才抛 `BackendNotImplementedError`。
- `_backend_from_arg` 负责把字符串 `'scipy'` 经 `_named_backends` 翻译成后端，并强制校验 `__ua_domain__ == "numpy.scipy.fft"`，实现「快速失败」。

---

## 7. 下一步学习建议

- 接下来读 [u4-l3](u4-l3-default-and-debug-backends.md)：剖析 `_ScipyBackend.__ua_function__` 如何按方法名在三个 `*_backend` 模块里查找实现，以及 `_debug_backends.py` 的 `NumPyBackend`/`EchoBackend` 如何成为调试分派的利器。
- 想亲手写一个合格后端，跳到 [u8-l2](u8-l2-custom-backend.md)「实战：编写并注册自定义第三方后端」，那里会综合运用本讲的 `set_backend`/`register_backend` 与 `NotImplemented` 回退。
- 建议同步阅读 [`_backend.py`](_backend.py) 全文（仅 212 行），对照本讲的每个断言；以及 [`_uarray_dispatch.cxx` 的 `for_each_backend_in_domain`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_uarray/_uarray_dispatch.cxx#L967-L1047)，亲手核对优先级顺序。
