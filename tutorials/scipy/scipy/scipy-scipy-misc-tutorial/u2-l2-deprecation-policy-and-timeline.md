# SciPy 的弃用约定与版本时间线

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 SciPy 通用的弃用政策：**先标记弃用 → 跨若干版本保留 → 在公告的版本里彻底移除**。
2. 读懂 [`scipy/_lib/deprecation.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py) 里几个关键辅助——`_NoValue`、`_deprecated`、`_sub_module_deprecation`、`_deprecate_positional_args`、`deprecate_cython_api`——各自解决什么问题。
3. 理解「版本号驱动」的移除时间线如何写进源码（`dep_version`、`2.0.0`）。
4. 把 `scipy.misc` 的真实生命周期（1.10.0 弃用 → PR #21864 移除大部分内容 → 2.0.0 完全移除）套进这套政策里，解释为什么它最后只剩一个裸 `warnings.warn`。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 什么是「弃用（deprecation）」

一个库的功能不能说删就删——上游删一个函数，下游成千上万个脚本就会立刻崩溃。负责任的做法是**弃用**：先在旧入口上挂一个 `DeprecationWarning`，告诉用户「这个东西以后会消失，请换用法」，然后让旧入口**继续可用**一段时间，给用户留迁移窗口，最后在某个公告过的版本里才真正删除。

`DeprecationWarning` 是 Python 内置的警告类别，专门标记这类「还能用、但别再用了」的接口。它是**非致命**的——默认只打印一行提示，不会中断程序。

### 2.2 版本号是弃用的「闹钟」

光发一句警告还不够，必须**告诉用户什么时候删**。SciPy 的约定是：警告消息里写明目标版本（例如 "removed in SciPy 2.0.0"）。这样用户能算出自己还剩多少个版本的时间迁移，维护者也能在版本到达时果断删除而「不算意外」。

本讲要回答的核心问题就是：**这套「先标记、定闹钟、到点删除」的政策，在源码里是如何被落地的？** 答案集中在 [`scipy/_lib/deprecation.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `scipy/_lib/deprecation.py` | SciPy 通用的弃用工具箱，定义 `_NoValue`、`_deprecated`、`_sub_module_deprecation`、`_deprecate_positional_args`、`deprecate_cython_api` 等 |
| `scipy/misc/__init__.py` | `scipy.misc` 的弃用桩文件，整模块退役的最简实现 |
| `scipy/io/matlab/_mio.py` | 真实使用 `_NoValue` 哨兵的范例（`loadmat` 的 `spmatrix` 参数） |
| `scipy/_lib/tests/test_deprecation.py` | `deprecate_cython_api` 的测试，演示如何断言弃用警告 |

## 4. 核心概念与源码讲解

### 4.1 版本驱动的移除时间线：弃用政策的骨架

#### 4.1.1 概念说明

SciPy 的弃用政策可以抽象成一条时间线，每个 API 都在这条线上走一遍：

```
[版本 A] 标记弃用：挂上 DeprecationWarning + 写明移除版本
   │   （旧入口继续可用，只发警告）
   │   ……跨若干个 minor 版本的「迁移窗口」……
[版本 B] 个别属性可按 dep_version 提前移除
   │
[2.0.0] 整个命名空间/旧模块被彻底删除
```

这条线有两个关键「闹钟」：

- **`dep_version`**：针对**单个属性**的移除版本，通常是个近期的 minor 版本（如 `1.16.0`）。
- **`2.0.0`**：针对**整个命名空间/模块**的移除版本。SciPy 承诺在下一个大版本 `2.0.0` 清理掉所有已弃用 API。

#### 4.1.2 核心流程

把「定闹钟」写成代码，本质就是**把版本字符串写死在警告消息里**，再在版本到达时由维护者手动删除代码。我们会在两个地方看到这个模式：

1. 在 `_sub_module_deprecation` 的签名里，`dep_version="1.16.0"` 是「单个属性何时删」的默认闹钟。
2. 在几乎所有弃用消息的结尾，都固定带一句 `removed in SciPy 2.0.0`——这是「整个旧命名空间何时删」的统一闹钟。

#### 4.1.3 源码精读

先看 `_sub_module_deprecation` 的签名，注意它的最后一个参数：

[\[scipy/\_lib/deprecation.py:15-16\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L15-L16) 定义了 `dep_version="1.16.0"`，这就是「单个弃用属性在哪个版本被移除」的可配置闹钟。

当被访问的属性根本不存在时，函数直接抛 `AttributeError`，并固定写明 `2.0.0`：

[\[scipy/\_lib/deprecation.py:44-49\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L44-L49) —— 属性不在白名单里，抛出「命名空间弃用、将在 2.0.0 移除」的错误。

当属性存在、但已被搬到新命名空间时，函数构造两条不同的消息，分别带上 `dep_version` 和 `2.0.0` 两个闹钟：

[\[scipy/\_lib/deprecation.py:53-66\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L53-L66) —— 「属性在 SciPy `dep_version` 移除；命名空间在 2.0.0 移除」，两个闹钟同时出现在消息里。

对比一下 `scipy.misc` 的桩文件，它的「闹钟」只有一个：

[scipy/misc/\_\_init\_\_.py:1-7](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L7) —— 直接写死「removed in 2.0.0」，没有 `dep_version`。这是「整模块退役」的终态特征：没有需要逐个迁移的属性，只剩整个命名空间的删除时间点。

#### 4.1.4 代码实践

**实践目标**：亲手验证「版本号驱动」的移除时间线是写在消息里的纯文本。

**操作步骤**：

1. 用 `catch_warnings(record=True)` 捕获 `scipy.misc` 的导入警告，断言消息里含 `2.0.0`。
2. 用 `git show 43fc97efa8^:scipy/_lib/deprecation.py`（或在浏览器看历史版本）确认 `_sub_module_deprecation` 当时的 `dep_version` 默认值，对比当前 HEAD 的默认值。

```python
# 示例代码：验证 scipy.misc 的「闹钟」是 2.0.0
import warnings
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    import scipy.misc
assert len(w) == 1
assert issubclass(w[0].category, DeprecationWarning)
assert "2.0.0" in str(w[0].message)   # 闹钟写在消息里
print("闹钟版本:", "2.0.0")
```

**需要观察的现象**：`w[0].message` 是 `"scipy.misc is deprecated and will be removed in 2.0.0"`，与源码第 3 行一字不差。

**预期结果**：断言全部通过，说明移除时间线就是源码里的字符串，到达该版本后维护者手动删代码即可。

第 2 步涉及 git 历史比较，若本地无法切换提交，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_sub_module_deprecation` 要区分 `dep_version`（属性级）和 `2.0.0`（命名空间级）两个闹钟？

**参考答案**：单个属性可以较早移除（它在 `dep_version` 就消失），但整个旧命名空间要给所有下游用户留更长的迁移窗口，统一在 `2.0.0` 删。两个闹钟让用户能精确预判「哪个先没、哪个后没」。

**练习 2**：`scipy.misc` 的桩文件只有 `2.0.0` 一个闹钟，这说明了什么？

**参考答案**：说明 `scipy.misc` 已进入「整模块退役」终态——没有需要逐个迁移、分批移除的属性了，整个命名空间在 `2.0.0` 一次性删除。

---

### 4.2 deprecation.py 的核心辅助：`_deprecated` 与 `_NoValue`

#### 4.2.1 概念说明

`scipy/_lib/deprecation.py` 是一个工具箱，它把「弃用」这件反复发生的事抽象成几个可复用的辅助。本模块精读其中最基础的两个：

- **`_NoValue`**：一个**哨兵对象（sentinel）**，用来做函数参数的默认值，区分「用户没传这个参数」和「用户传了 `None`」。
- **`_deprecated`**：一个**装饰器**，把「调用函数就发弃用警告」的逻辑包到任意函数上。

文件顶部 `__all__ = ["_deprecated"]`（[\[deprecation.py:8\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L8)）说明：在所有辅助里，只有 `_deprecated` 算作「对外公开」的名字，其余都是 SciPy 内部用的下划线私有辅助。

#### 4.2.2 核心流程

**`_NoValue` 的工作流程**（以「弃用某个参数的默认行为」为例）：

```
函数签名: def f(x, mode=_NoValue):       # 用哨兵占位
    if mode is _NoValue:                  # 检测「用户没传」
        warnings.warn("mode 默认值将变更", DeprecationWarning)
        mode = 旧默认值                   # 旧行为兜底
    ...正常逻辑...
```

关键：用 `is _NoValue` 判断，而不是 `is None`。因为 `None` 是用户可能合法传入的值，而 `_NoValue` 是 SciPy 私有的、用户不可能也不应该传的「占位符」。

**`_deprecated` 的工作流程**：

```
@_deprecated("foo() 将在 X 版本移除，请改用 bar()")
def foo(...):
    ...
# 每次调用 foo() → 先发 DeprecationWarning → 再执行原函数
```

#### 4.2.3 源码精读

**`_NoValue`**——注意它上方的注释，这正是「为什么不用 `None`」的官方解释：

[\[scipy/\_lib/deprecation.py:11-13\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L11-L13) —— `# Object to use as default value for arguments to be deprecated. This should be used over 'None' as the user could parse 'None' as a positional argument`。一句话：用户可能把 `None` 当成实参传进来，所以不能用 `None` 当哨兵。

SciPy 里大量真实代码就是这么用的。以 `loadmat` 为例：

[\[scipy/io/matlab/\_mio.py:82\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L82) —— `def loadmat(..., spmatrix=_NoValue, ...)`，用 `_NoValue` 标记「这个参数的默认行为正在弃用」。

[\[scipy/io/matlab/\_mio.py:262-265\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L262-L265) —— `if spmatrix is _NoValue:` 才发 `DeprecationWarning` 并回退到旧默认值 `True`。如果用户显式传了 `spmatrix=True/False`，就**不**发警告——这正是哨兵的价值：只对「依赖旧默认值」的人提示，不骚扰「已显式选择」的人。

**`_deprecated`**——一个标准的 `functools.wraps` 装饰器：

[\[scipy/\_lib/deprecation.py:81-98\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L81-L98) —— `wrap` 内部用 `@functools.wraps(fun)` 包装出 `call`，`call` 先 `warnings.warn(msg, DeprecationWarning, stacklevel=stacklevel)` 再 `return fun(*args, **kwargs)`。

注意一个细节：[\[deprecation.py:84-88\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L84-L88) 里如果发现被装饰的是个**类**（`isinstance(fun, type)`），它会发 `RuntimeWarning` 并原样返回——因为这套「每次调用包一层」的逻辑只对函数有效，套在类上会出问题，所以 SciPy 选择「拒绝静默装饰类」，这是防御性设计。

> 这里的 `stacklevel=2` 与上一讲（u2-l1）桩文件的 `stacklevel=2` 同值，但**含义不同**：装饰器写在函数包装器里，`2` 指向「调用被弃用函数的那一行」；桩文件写在模块顶层，`2` 指向「触发导入的 `import` 那一行」。可见 `stacklevel` 的取值必须随代码结构量取，不能背数字。

#### 4.2.4 代码实践

**实践目标**：用 `_NoValue` + `_deprecated` 自己复刻一次 SciPy 的弃用模式，并观察到警告。

**操作步骤**：

```python
# 示例代码：复刻 _NoValue 哨兵 + _deprecated 装饰器
import warnings
import functools

# 1) 哨兵
_NoValue = object()

# 2) 装饰器（仿 scipy._lib.deprecation._deprecated）
def _deprecated(msg, stacklevel=2):
    def wrap(fun):
        @functools.wraps(fun)
        def call(*args, **kwargs):
            warnings.warn(msg, category=DeprecationWarning, stacklevel=stacklevel)
            return fun(*args, **kwargs)
        return call
    return wrap

# 3) 用法 A：弃用一个函数
@_deprecated("old_func() 将在 9.9 移除，请用 new_func()")
def old_func(x):
    return x + 1

# 4) 用法 B：弃用某个参数的默认值
def f(x, mode=_NoValue):
    if mode is _NoValue:
        warnings.warn("mode 的默认值将变更", DeprecationWarning)
        mode = "legacy"
    return x, mode

# 观察
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    print(old_func(1))      # (1, 1)? 不，应是 2
    f(1)                    # 用旧默认值 → 应触发警告
    f(1, mode="new")        # 显式传值 → 不触发警告
    for warning in w:
        print("WARN:", warning.category.__name__, "-", warning.message)
```

**需要观察的现象**：

- `old_func(1)` 输出 `2`，同时产生一条 `DeprecationWarning`。
- `f(1)`（不传 `mode`）产生一条 `DeprecationWarning`，因为 `mode is _NoValue` 为真。
- `f(1, mode="new")` **不**产生警告，因为 `mode is _NoValue` 为假——验证了「哨兵只提示依赖默认值的人」。

**预期结果**：共 2 条 `DeprecationWarning`（来自 `old_func` 和不带 `mode` 的 `f(1)`），带 `mode="new"` 的那次不报警。

#### 4.2.5 小练习与答案

**练习 1**：把上面 `f` 的 `if mode is _NoValue` 改成 `if mode is None`，并把默认值改成 `mode=None`，会出什么问题？

**参考答案**：当用户**显式**写 `f(1, mode=None)`（表示「我就要 None」）时，会被误判成「用户没传」，从而错误地发出弃用警告并悄悄改成 `"legacy"`。哨兵 `_NoValue` 就是为了排除这种歧义。

**练习 2**：`_deprecated` 为什么用 `@functools.wraps(fun)`？

**参考答案**：为了让包装后的 `call` **保留原函数的名字（`__name__`）、文档字符串（`__doc__`）和签名**。否则被装饰函数在帮助文档、错误回溯里会显示成无意义的 `call`，破坏用户体验。

**练习 3**：为什么 `_deprecated` 检测到类（`isinstance(fun, type)`）就拒绝装饰？

**参考答案**：这套「每次调用包一层、先警告再执行」的逻辑是针对函数调用设计的；类的实例化、属性访问、方法解析路径不同，套用会破坏行为或漏发警告。SciPy 选择用 `RuntimeWarning` 提醒维护者「你用错了」，而不是静默放过。

---

### 4.3 工具箱的其余利器：模块弃用、位置参数、Cython 接口

#### 4.3.1 概念说明

除了 `_NoValue` 和 `_deprecated`，`deprecation.py` 还有三件针对**不同弃用场景**的利器。它们共同覆盖了 SciPy 里几乎所有需要弃用的场合：

| 辅助 | 适用场景 |
|------|----------|
| `_sub_module_deprecation` | 弃用「公开但其实想私有」的子模块（如 `scipy.stats._foo`），把用户引导到新导入路径 |
| `_deprecate_positional_args` | 把「可以按位置传」的参数改成「只能按关键字传」，或彻底删除某参数 |
| `deprecate_cython_api` | 弃用公开 Cython 模块（如 `scipy.linalg.cython_blas`）里导出的 `cdef` 函数 |

#### 4.3.2 核心流程

**`_sub_module_deprecation` 的流程**（这是 SciPy 里最常见的「软弃用」）：

```
用户写:  from scipy.stats._mstats_basic import gmean   # 私有模块
   │
   ├─ 属性不在白名单 all → 抛 AttributeError("...removed in 2.0.0")
   ├─ 属性在新公共模块里 → 警告 "请从 scipy.stats 导入；旧命名空间 2.0.0 移除"
   └─ 属性只在私有模块里 → 警告 "属性在 dep_version 移除，命名空间 2.0.0 移除"
        └─ 从私有模块取到属性并 return（旧入口仍可用）
```

它的关键设计是：**旧入口在迁移窗口内仍然能用、仍然返回正确结果**，只是每次访问都发一条「请改路」的警告。这与 `scipy.misc` 的「直接删空」形成鲜明对比。

#### 4.3.3 源码精读

**`_sub_module_deprecation`**——注意它用 `stacklevel=3`：

[\[scipy/\_lib/deprecation.py:15-38\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L15-L38) —— 完整签名与参数文档。它通常被一个模块级 `__getattr__`（PEP 562）调用，所以栈深多一层，`stacklevel=3` 才能回退到用户的那行 `import`。

[\[scipy/\_lib/deprecation.py:68\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L68) —— `warnings.warn(message, category=DeprecationWarning, stacklevel=3)`。

[\[scipy/\_lib/deprecation.py:70-78\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L70-L78) —— 从私有模块取到属性并 `return`。即便抛了警告，**旧入口照常返回正确结果**——这就是「软弃用」。

**`_deprecate_positional_args`**——借自 scikit-learn，文件里还留了出处注释：

[\[scipy/\_lib/deprecation.py:182-185\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L182-L185) —— `# taken from scikit-learn`，签名 `func=None, *, version=None, deprecated_args=None, custom_message=""`。

[\[scipy/\_lib/deprecation.py:228-251\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L228-L251) —— 核心逻辑：如果用户多传了位置参数，就警告「请在 `version` 之后改成关键字传」，否则从 `version` 起会报错。它还会往函数 docstring 里注入一段 Sphinx `.. deprecated::` 标记（[\[deprecation.py:255-264\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L255-L264)），让文档自动显示弃用提示。

**`deprecate_cython_api` + `_DeprecationHelperStr`**——这是最巧妙的一招。Cython 模块通过 `__pyx_capi__` 字典向下游导出 `cdef` 函数，下游用 `from ... cimport foo` 时 Cython 会用字符串键去字典里**比对**（`__eq__`）。SciPy 把原来的字符串键换成 `_DeprecationHelperStr` 对象：

[\[scipy/\_lib/deprecation.py:101-117\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L101-L117) —— `_DeprecationHelperStr` 重写了 `__eq__`：**当比对成功时**就发弃用警告。这样下游 Cython 模块一 `cimport` 那个旧名字，比对触发，警告就发出来了。

[\[scipy/\_lib/deprecation.py:120-179\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L120-L179) —— `deprecate_cython_api` 的实现：取出 `module.__pyx_capi__`，把旧键 `pop` 出来、用 `_DeprecationHelperStr` 重新塞回去，并兼容「融合类型（fused-type）」函数产生的 `__pyx_fuse_*` 变体名。

它的测试在 `test_deprecation.py`，演示了「导入下游模块即触发警告」：

[\[scipy/\_lib/tests/test\_deprecation.py:3-10\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/tests/test_deprecation.py#L3-L10) —— 用 `pytest.warns(DeprecationWarning, match=...)` 断言：`from .. import _test_deprecation_call` 这一步导入会发出包含「is deprecated, use `foo` instead」和「Deprecated in Scipy 42.0.0」的警告，且随后 `call()` 仍返回正确结果 `(1, 1)`。注意这里用了测试用的「玩具版本」`42.0.0`，说明版本号本身只是消息文本。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试理解 `_DeprecationHelperStr` 的「比对即警告」机制。

**操作步骤**：

1. 阅读 [`scipy/_lib/tests/test_deprecation.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/tests/test_deprecation.py)（仅 10 行）。
2. 手写一个最小复刻，验证「`__eq__` 成功时发警告」：

```python
# 示例代码：复刻 _DeprecationHelperStr 的「比对即警告」
import warnings

class DepHelper:
    def __init__(self, content, message):
        self._content = content
        self._message = message
    def __hash__(self):
        return hash(self._content)
    def __eq__(self, other):
        res = (self._content == other)
        if res:
            warnings.warn(self._message, DeprecationWarning, stacklevel=2)
        return res

key = DepHelper("old_name", "old_name 已弃用，请用 new_name")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    hit = (key == "old_name")   # 比对成功 → 触发警告
    miss = (key == "xxx")       # 比对失败 → 不触发
print("hit:", hit, "miss:", miss, "warnings:", len(w))
```

**需要观察的现象**：`key == "old_name"` 返回 `True` 并触发 1 条警告；`key == "xxx"` 返回 `False`、不触发警告。最终 `len(w) == 1`。

**预期结果**：与 `_DeprecationHelperStr` 的源码行为一致——「比对命中」才报警，这正是下游 `cimport` 旧名字时发生的事。

#### 4.3.5 小练习与答案

**练习 1**：`_sub_module_deprecation` 为什么用 `stacklevel=3`，而 `_deprecated` 用 `stacklevel=2`？

**参考答案**：`_sub_module_deprecation` 通常被模块级 `__getattr__` 调用，调用链是「用户的 import → 模块 `__getattr__` → `_sub_module_deprecation` → `warnings.warn`」，比 `_deprecated` 多一层（用户调用 → `call` 包装器 → `warnings.warn`），所以要多回退一帧，用 `3`。

**练习 2**：`_sub_module_deprecation` 与 `scipy.misc` 桩文件的根本区别是什么？

**参考答案**：`_sub_module_deprecation` 是**软弃用**——旧入口仍可用、仍返回正确结果，只是引导用户改导入路径，最终在 `2.0.0` 删命名空间。`scipy.misc` 桩文件是**硬退役**——真实内容已被 PR #21864 删空，旧入口里已经**没有任何功能**可以返回，只剩一个「我马上要被删」的墓碑警告。

**练习 3**：`deprecate_cython_api` 为什么不直接 `warnings.warn`，而要绕一道 `_DeprecationHelperStr`？

**参考答案**：因为下游对 Cython `cdef` 函数的引用发生在**编译期/导入期**，通过 `__pyx_capi__` 字典的键比对来解析。直接 `warnings.warn` 找不到合适的触发时机；而把键换成「比对命中就报警」的 `_DeprecationHelperStr`，能精确地在下游 `cimport` 旧名字的那一刻触发警告。

---

### 4.4 把时间线套回 scipy.misc：一个模块的完整生命周期

#### 4.4.1 概念说明

前三个模块讲的是**通用工具**。本模块把它们和 `scipy.misc` 的真实历史对上号，说明 `scipy.misc` 为什么最终长成现在这副「只剩一个裸警告」的样子。这是把「政策」套到「实例」上的收口模块。

#### 4.4.2 核心流程：scipy.misc 的三个里程碑

| 里程碑 | 含义 | 对应到弃用政策 |
|--------|------|----------------|
| **SciPy 1.10.0** | `scipy.misc` 正式标记弃用；`face`/`ascent`/`electrocardiogram` 迁到新建的 `scipy.datasets`；当时用 PEP 562 模块级 `__getattr__` 对每个名字给定制化弃用提示 | 「先标记弃用 + 定闹钟」阶段 |
| **PR #21864（提交 `43fc97efa8`）** | 移除 `scipy.misc` 剩下的大部分内容（`derivative`/`central_diff_weights` 等数值工具、`_common.py`）；逐名字的 `__getattr__` 被拆掉，退化为现在的裸 `warnings.warn` 桩文件 | 「跨版本逐步删除内部实现」阶段 |
| **SciPy 2.0.0** | 整个 `scipy.misc` 命名空间（含 `common`/`doccer` 子模块）被彻底删除 | 「到闹钟、一次性移除」阶段 |

#### 4.4.3 源码精读

把当前桩文件和弃用政策逐项对照：

[scipy/misc/\_\_init\_\_.py:1-7](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L7) —— 这是 PR #21864 之后的**终态**。逐行对照政策：

- 第 1 行 `import warnings`：用 Python 标准库发警告，**没有** import `scipy._lib.deprecation` 的任何辅助。
- 第 3 行消息 `"scipy.misc is deprecated and will be removed in 2.0.0"`：闹钟是 `2.0.0`，对应政策里「整个命名空间的移除版本」。
- 第 4 行 `DeprecationWarning`：用的是标准弃用类别。
- 第 5 行 `stacklevel=2`：把警告归因到用户的 `import scipy.misc` 那一行（机制详见上一讲 u2-l1）。

**为什么不用 `_sub_module_deprecation`？** 因为 `_sub_module_deprecation` 的前提是「旧入口背后还有可返回的正确结果」（[\[deprecation.py:70-78\]](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L70-L78) 那段 `return getattr(...)`）。而 PR #21864 之后 `scipy.misc` 背后**什么都没了**——数据集搬去了另一个顶层包 `scipy.datasets`，数值工具被直接删掉，没有同包内的私有模块可作 `private_modules` 兜底。于是它无法走「软弃用 + 重定向」路线，只能退化为一个**纯墓碑桩文件**：仅声明「我将在 2.0.0 消失」，不提供任何功能。

这也解释了为什么桩文件**没有用 `_deprecated` 装饰器**：`_deprecated` 是装饰「还在的函数」的，而 `scipy.misc` 里已经没有函数可装饰了，弃用发生在**模块导入**这一层，所以直接在模块顶层写一句 `warnings.warn` 最贴切。

#### 4.4.4 代码实践

**实践目标**：把 `scipy.misc` 的三个里程碑连成一条可验证的时间线说明。

**操作步骤**：

1. 阅读当前 [scipy/misc/\_\_init\_\_.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py)，确认它只发一条「removed in 2.0.0」的警告、无任何可调用对象。
2. 用 `git show 43fc97efa8^:scipy/misc/__init__.py` 还原 PR #21864 **之前**的入口（前置讲义 u1-l3 已演示），对比它能看出：旧版用模块级 `__getattr__` 按名字分流、消息里带 `dataset_methods` 等分类，是「逐属性软弃用」；新版只剩裸警告。
3. 写一份约 150 字的说明，把下表填全：

| 里程碑 | 旧入口里还剩什么 | 走的是哪条弃用路线 |
|--------|------------------|--------------------|
| 1.10.0 | 数据集函数 + `__getattr__` 定制提示 | 逐属性软弃用（类似 `_sub_module_deprecation` 思路） |
| PR #21864 后 | 仅一个裸 `warnings.warn` | 纯墓碑，硬退役 |
| 2.0.0 | （整个模块删除） | 到闹钟、移除 |

**需要观察的现象**：当前桩文件里搜不到任何 `def`/`class`/`return`；而 `43fc97efa8^` 版本的 `__init__.py` 里能看到 `__getattr__`、`dataset_methods`、按名字拼消息的分支。

**预期结果**：能清晰讲出「`scipy.misc` 从逐属性软弃用 → 拆空后只剩墓碑桩 → 2.0.0 物理删除」这条退化路径，并指出每一步对应弃用政策里的哪个阶段。

第 2 步依赖 git 历史访问；若本地无法切换提交，可标注「待本地验证」，仅依据本讲与前置讲义 u1-l3 提供的描述完成对比。

#### 4.4.5 小练习与答案

**练习 1**：假设 `scipy.misc` 当初选择「只弃用、不删内容」，今天的桩文件会长什么样？

**参考答案**：它会更像 `_sub_module_deprecation` 的形态——保留 `face`/`derivative` 等名字，用一个模块级 `__getattr__` 在每次访问时发「请改用 scipy.datasets / 请自行实现」的警告，并 `return` 一个仍能工作的函数。正是因为 PR #21864 把内容**物理删除**了，才退化为今天的纯墓碑桩。

**练习 2**：给定一段 `from scipy.misc import derivative` 的旧代码，如何**只看版本号**判断它在当前 SciPy 里还能不能用？

**参考答案**：`derivative` 在 PR #21864（介于 1.10.x 与 2.0.0 之间）被物理删除。因此：版本 `< 该 PR 合入的发布版本` 时仍可用（会带 `DeprecationWarning`）；之后任何版本都会直接 `ImportError`。而整个 `import scipy.misc` 则要等 `2.0.0` 才会彻底报错——这正是「属性级移除早于命名空间级移除」政策的具体体现。

## 5. 综合实践

把本讲三块内容串起来，完成一次「弃用政策速写」：

**任务**：为「某个假想模块 `myproj.legacy` 设计一条合规的退役时间线」，要求覆盖本讲全部要点。

1. **政策**：写出该模块的三个里程碑版本（弃用版本、属性级移除版本、命名空间移除版本 `2.0.0`），说明每个版本用户会看到什么。
2. **工具选型**：分别用本讲学到的辅助给三类场景写「伪代码」：
   - 弃用一个函数 → 用 `_deprecated`
   - 弃用某个参数的默认行为 → 用 `_NoValue` 哨兵 + `if x is _NoValue`
   - 弃用整个模块、内容已删空 → 仿 `scipy.misc` 桩文件写裸 `warnings.warn(..., stacklevel=2)`，并解释为什么这里**不能**用 `_sub_module_deprecation`。
3. **验证**：参照 4.2.4 的示例代码，实际跑一遍 `_deprecated` 与 `_NoValue` 两种用法，确认警告在「该出现时出现、不该出现时不出现」。

**验收标准**：能口头回答「`stacklevel` 为什么装饰器用 2、`_sub_module_deprecation` 用 3」「`_NoValue` 为什么不用 `None`」「为什么 `scipy.misc` 不复用 `deprecation.py` 的辅助」这三个问题。

## 6. 本讲小结

- SciPy 的弃用政策是一条**版本驱动的时间线**：先标记弃用（`DeprecationWarning`）→ 跨若干版本保留 → 在公告版本（属性级 `dep_version`、命名空间级 `2.0.0`）删除；「闹钟」就是写死在警告消息里的版本字符串。
- `_NoValue = object()` 是**哨兵**，用 `is _NoValue` 区分「用户没传参数」与「用户传了 `None`」，`loadmat` 的 `spmatrix` 是真实范例。
- `_deprecated` 是 `functools.wraps` 装饰器，让被弃用函数每次调用都发警告；它是 `__all__` 里唯一对外公开的辅助，且会拒绝装饰类。
- `_sub_module_deprecation`（`stacklevel=3`）是「软弃用 + 重定向」：旧入口仍可用、仍返回正确结果，引导用户改导入路径。
- `_deprecate_positional_args`（借自 scikit-learn）把位置参数强制改为关键字；`deprecate_cython_api` 靠 `_DeprecationHelperStr.__eq__` 实现「比对即警告」来弃用 Cython `cdef` 接口。
- `scipy.misc` 走的是**硬退役**：PR #21864 把内容删空后，无法再用 `_sub_module_deprecation`（没有可返回的结果），退化为只剩一个裸 `warnings.warn` 的墓碑桩，闹钟是 `2.0.0`。

## 7. 下一步学习建议

- 下一讲 **u2-l3《Meson 构建：py3.install_sources 与源码清单》** 会从构建系统角度解释：为什么这个「只剩桩文件」的模块仍必须在 `meson.build` 里保留安装条目，直到 `2.0.0` 物理删除。
- 进阶方向：阅读 [`scipy/_lib/deprecation.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py) 中 `_sub_module_deprecation` 的真实调用方——在 `scipy/stats`、`scipy/sparse` 等子包里搜索 `from scipy._lib.deprecation import _sub_module_deprecation`，观察「软弃用 + PEP 562 `__getattr__`」这套组合在真实子模块里如何落地，为专家层 u3-l2（PEP 562 演进）做铺垫。
