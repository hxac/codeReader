# numpy.core 是什么：NumPy 2.0 命名空间迁移与兼容层定位

## 1. 本讲目标

本讲是整本手册的第一篇。读完本讲，你应该能够：

- 说出 `numpy.core` 这个目录的**真实身份**——它早已不再包含任何实现，只是一个「向后兼容垫片（shim）」。
- 解释 NumPy 2.0 把原来的 `core` 重命名为私有 `_core` 的迁移背景，以及为什么还要保留 `numpy.core`。
- 通过阅读 `numpy/core/__init__.py` 的模块文档字符串，看懂它对自己命运的官方说明。
- 用一个小脚本验证：访问 `numpy.core` 下的属性，真正提供实现的是 `numpy._core`。

本讲**不会**深入 `__getattr__` 的转发细节（那是第 2 单元的内容），只建立「它是什么、为什么存在」的全局认知。

## 2. 前置知识

在进入源码前，先用大白话建立几个概念。

### 2.1 NumPy 与它的内部实现

NumPy 是 Python 里做数值计算的基础库，`import numpy as np` 之后用的 `np.array`、`np.asarray`、`np.zeros` 等都来自它。在这些公开 API 背后，NumPy 内部有一个叫做 `core`（核心）的子包，历史上集中存放了数组、ufunc 等最底层的实现代码。

### 2.2 Python 里的「私有」约定

在 Python 中，名字以单个下划线 `_` 开头的模块（例如 `_core`）按约定是「私有的」。私有意味着：

- 它是给项目内部使用的，**不对外承诺稳定性**，版本之间可以随意改动。
- 普通使用者**不应该**直接依赖它。

NumPy 2.0 做的一件大事，就是把原来对外可见的 `numpy.core` 改成了私有的 `numpy._core`，明确告诉用户「这是内部实现，请勿依赖」。

### 2.3 什么是「垫片（shim）」

垫片（shim）是一个软件工程里常见的比喻：它是一层**很薄的转发层**，自己几乎不干活，只负责把请求「转交」给真正干活的模块。它存在的目的通常是**兼容**——让老代码在不修改的情况下还能跑起来。

打个比方：你常去的图书馆把「计算机类」书架搬到了地下室，并改名「技术藏书区」（私有）。但为了不让老读者迷路，管理员在原位置贴了一张告示牌：「计算机类已迁移，请到地下室技术藏书区找」。这张告示牌就是垫片——它本身没有书，只负责把你指向新位置，并提醒你以后最好走公开借阅区。

`numpy.core` 就是 NumPy 留下的这样一块「告示牌」。

## 3. 本讲源码地图

本讲只涉及 `numpy/core/` 目录下两个文件，这是理解整个兼容层的最小入口。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py) | 包的入口文件，定义了 `numpy.core` 的身份、转发逻辑和可惰性加载的子模块列表 | 模块文档字符串、`from numpy import _core`、`__getattr__` |
| [`__init__.pyi`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi) | 类型存根文件，给类型检查器（如 mypy/pyright）看 | 看一眼即可，本讲不深入 |

> 说明：`.pyi` 是 Python 的「类型存根」文件，只包含类型注解，不含运行时逻辑。类型检查器读它来推断类型，而解释器运行时**完全忽略**它，只执行 `.py`。所以「理解运行时行为」只需看 `.py`。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. 从**模块文档字符串**看 `numpy.core` 的定位。
2. 看 `core` 是如何把请求**转发给 `_core`** 的。

### 4.1 从模块文档字符串看 numpy.core 的定位

#### 4.1.1 概念说明

每一个 Python 模块/包都可以在文件最开头写一段用三引号包围的字符串，叫做**模块文档字符串（module docstring）**。它有两个作用：

- 运行时挂在模块对象的 `__doc__` 属性上，可以用 `print(模块名.__doc__)` 打印出来。
- 给阅读源码的人说明「这个模块是干嘛的」。

`numpy/core/__init__.py` 的第一段就是模块文档字符串，它用最直白的方式宣告了整个目录的身份。这是本讲最重要的一段文字，因为它由 NumPy 官方亲自写明，是我们判断「`numpy.core` 是什么」的第一手证据。

#### 4.1.2 核心流程

当你写下 `import numpy.core` 并查看它的文档时，发生的事情是：

1. Python 解释器执行 `numpy/core/__init__.py`。
2. 文件最顶部的三引号字符串被读作模块文档字符串，赋值给 `numpy.core.__doc__`。
3. 你用 `print(numpy.core.__doc__)` 就能看到这段官方说明。

换句话说，文档字符串的内容，就是 NumPy 维护者想让每个打开这个文件、或查询这个模块的人第一眼看到的话。

#### 4.1.3 源码精读

来看文件最开头的文档字符串：

[`numpy/core/__init__.py` 第 1–5 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L1-L5) —— 模块文档字符串，宣告 numpy.core 的身份：

```python
"""
The `numpy.core` submodule exists solely for backward compatibility
purposes. The original `core` was renamed to `_core` and made private.
`numpy.core` will be removed in the future.
"""
```

这段话信息量很大，逐句拆解：

- **"exists solely for backward compatibility purposes"**：`numpy.core` 这个子模块存在的**唯一目的**就是向后兼容。换句话说，它自己不实现任何 NumPy 功能。
- **"The original `core` was renamed to `_core` and made private"**：原来的 `core` 被改名为 `_core`，并标记为私有。这是 NumPy 2.0 命名空间迁移的核心动作。
- **"will be removed in the future"**：`numpy.core` 在未来会被移除。这告诉我们，它只是一段**过渡期**的产物，不应该在新代码里依赖它。

紧接着文档字符串的下一行，是真正的转发动作：

[`numpy/core/__init__.py` 第 6 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L6) —— 把整个 `_core` 拿进来，作为后续转发的目标：

```python
from numpy import _core
```

这一行说明：真正的实现住在 `numpy._core` 里，`numpy.core` 只是把它「请」过来，以便转发。

#### 4.1.4 代码实践

我们来亲自把文档字符串打印出来，验证上面读到的东西。

1. **实践目标**：确认 `numpy.core` 的 `__doc__` 里确实写着「向后兼容、已重命名、将被移除」。
2. **操作步骤**：在装好 NumPy（建议 2.0 及以上版本）的环境里，新建 `show_doc.py`：

   ```python
   import numpy.core

   print(numpy.core.__doc__)
   ```

   运行 `python show_doc.py`。
3. **需要观察的现象**：终端输出一段英文说明文字，开头是 "The `numpy.core` submodule exists solely ..."。
4. **预期结果**：输出与本讲 4.1.3 引用的那段文档字符串一致。这说明你看到的 `numpy.core`，确实只是一个声明自己「仅为兼容而存在」的垫片。
5. **待本地验证**：不同 NumPy 小版本的具体措辞可能略有差异，若输出与上述不完全一致，以你本地的实际输出为准。

### 4.2 core 如何把请求转发给 _core

#### 4.2.1 概念说明

既然 `numpy.core` 自己没有实现，那么老代码里 `numpy.core.xxx` 这种用法为什么还能正常工作？答案是**转发**：当你访问 `numpy.core` 上的某个名字时，垫片会偷偷去找 `numpy._core` 上同名的对象，把它交给你，同时（在合适的时候）提醒你一句「这里废弃了」。

实现「按需、按属性」转发的机制，是 Python 3.7 引入的**模块级 `__getattr__`**（PEP 562）。它的作用可以简单理解为：

> 当你访问一个模块上**找不到**的属性时，Python 会去调用这个模块里定义的 `__getattr__(名字)` 函数，由它决定返回什么。

本讲你只需要知道「有这么一个转发函数」就够了，它的工作细节（比如为什么会「懒加载」、`__all__` 在其中起什么作用）会放在第 2 单元专门讲。

> 名词解释：**`__all__`** 是一个列表，用来声明一个模块「对外公开」的名字。在 `numpy.core/__init__.py` 里，它被特意列出一堆**子模块名**，目的是强制这些子模块在第一次被访问时才加载（即「惰性加载」），这样访问时才能打印出废弃警告。这一点本讲先记住结论，细节见第 2 单元。

#### 4.2.2 核心流程

访问 `numpy.core.<某个子模块>` 或 `numpy.core.<某个属性>` 时的大致过程：

```
numpy.core.<名字>
        │
        ▼
numpy.core 自己有这个名字吗？（通常没有实现，只有垫片骨架）
        │ 否
        ▼
调用 numpy.core.__getattr__(名字)
        │
        ▼
__getattr__ 从 numpy._core 取同名对象：getattr(_core, 名字)
        │
        ▼
记录/抛出废弃警告（这里包级 __getattr__ 会提示「numpy.core 弃用」）
        │
        ▼
把从 _core 取到的对象返回给调用者
```

注意：对于 `numpy.core.numeric.asarray` 这种「子模块里的属性」，转发是分两层完成的——先由包级 `__getattr__` 转发到 `numpy._core.numeric` 这个子模块，再由该子模块自己的垫片（例如 `numpy/core/numeric.py`）转发到 `numpy._core.numeric.asarray`。本讲关注**包级**这第一层。

#### 4.2.3 源码精读

先看包级 `__getattr__`：

[`numpy/core/__init__.py` 第 30–33 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) —— 包级 `__getattr__`：从 `numpy._core` 取同名属性并返回：

```python
def __getattr__(attr_name):
    attr = getattr(_core, attr_name)
    _raise_warning(attr_name)
    return attr
```

逐行理解：

- `attr = getattr(_core, attr_name)`：从真正的实现包 `numpy._core` 上取出叫 `attr_name` 的对象（注意 `_core` 就是第 6 行 `from numpy import _core` 引入的）。如果 `_core` 上也没有，这里会直接抛出 `AttributeError`。
- `_raise_warning(attr_name)`：调用一个工具函数，抛出「`numpy.core` 已弃用」的警告。这个函数定义在 `_utils.py` 里，本讲先看一眼它的样子。
- `return attr`：把从 `_core` 取到的对象返回给调用者。

再来看 `_raise_warning` 长什么样（注意它末尾把警告类别设成了 `DeprecationWarning`，并且消息里反复提到 `numpy._core`）：

[`numpy/core/_utils.py` 第 4–21 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) —— 统一的弃用警告生成函数：

```python
def _raise_warning(attr: str, submodule: str | None = None) -> None:
    new_module = "numpy._core"
    old_module = "numpy.core"
    if submodule is not None:
        new_module = f"{new_module}.{submodule}"
        old_module = f"{old_module}.{submodule}"
    warnings.warn(
        f"{old_module} is deprecated and has been renamed to {new_module}. "
        ...
        f"use {new_module}.{attr}.",
        DeprecationWarning,
        stacklevel=3
    )
```

从函数名和内容就能看出它的职责：拼出一段「`numpy.core` 已弃用，已改名为 `numpy._core`，请改用公开 API」的提示，并以 `DeprecationWarning`（弃用警告）的形式发出。它就是垫片「告示牌」上那段话的生成器。

最后看一眼 `__all__`：

[`numpy/core/__init__.py` 第 25–28 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L25-L28) —— 声明需要惰性加载的子模块列表：

```python
__all__ = ["arrayprint", "defchararray", "_dtype_ctypes", "_dtype",  # noqa: F822
           "einsumfunc", "fromnumeric", "function_base", "getlimits",
           "_internal", "multiarray", "_multiarray_umath", "numeric",
           "numerictypes", "overrides", "records", "shape_base", "umath"]
```

注意上面的注释 `# force lazy-loading of submodules to ensure a warning is printed`（见 [第 23 行](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L23)）：维护者明确表示，把这些子模块列在 `__all__` 里，是为了**强制它们惰性加载**，从而保证每次访问都能打印一条废弃警告。这就是为什么 `numpy.core` 不会在 `import` 时一股脑把所有子模块全加载进来——细节留到第 2 单元。

#### 4.2.4 代码实践

我们来验证「垫片转发的对象，其实来自 `numpy._core`」。

1. **实践目标**：证明 `numpy.core.numeric.asarray` 背后真正的实现属于 `numpy._core`。
2. **操作步骤**：新建 `check_module.py`：

   ```python
   import numpy.core.numeric as core_numeric

   obj = core_numeric.asarray
   print("asarray 的 __module__ 是：", getattr(obj, "__module__", "<无 __module__>"))
   ```

   运行 `python check_module.py`（你会看到一条 DeprecationWarning，这是正常的，下一讲会专门讲它）。
3. **需要观察的现象**：输出的 `__module__` 字符串里**包含 `numpy._core`**（例如形如 `numpy._core.multiarray` 或 `numpy._core.numeric`），而不是 `numpy.core`。
4. **预期结果**：这说明你虽然写的是 `numpy.core.numeric.asarray`，但拿到的函数对象本身是 `numpy._core` 体系里定义的——垫片只是把它递到你手上。
5. **待本地验证**：`asarray` 是 C 实现的内置函数，不同版本里它 `__module__` 的精确取值可能有差异；只要其中包含 `numpy._core`，就达到了本实践的目的。若你本地输出不含 `numpy._core`，请核对所装 NumPy 是否为 2.x。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `numpy.core` 的 `__init__.py` 里要写 `from numpy import _core`？删掉它会怎样？

> **参考答案**：因为包级 `__getattr__` 里的 `getattr(_core, attr_name)` 需要用到 `_core` 这个名字。如果删掉这行导入，`_core` 就是未定义的，访问任何 `numpy.core.<属性>` 时都会在 `__getattr__` 内抛出 `NameError`，转发链立刻断掉。

**练习 2**：根据 4.1.3 的文档字符串，`numpy.core` 未来会被移除。那么新写的代码应该直接用 `numpy.core` 吗？应该用什么？

> **参考答案**：不应该。文档字符串和 `_raise_warning` 都明确建议：大多数真实用途其实是要访问 NumPy 的公开 API，应直接用公开 API（例如 `numpy.asarray`、`numpy.zeros`）；只有确实需要内部属性时才用 `numpy._core`，但要承担「内部实现可能随时变动」的风险。

## 5. 综合实践

把本讲两块内容串起来，完成下面这个综合脚本。它同时验证「文档字符串说自己是垫片」和「转发的对象来自 `_core`」两件事。

1. **实践目标**：用一个脚本同时读取垫片的自述文档，并验证转发对象的真实来源。
2. **操作步骤**：新建 `u1_l1_overview.py`：

   ```python
   import warnings

   # 任务一：打印垫片的官方自述
   import numpy.core
   print("=== numpy.core 的自述 ===")
   print(numpy.core.__doc__)

   # 任务二：取一个属性，看它真正来自哪里
   # 访问子模块属性会触发 DeprecationWarning，这里把它显示出来观察
   with warnings.catch_warnings(record=True) as caught:
       warnings.simplefilter("always")
       obj = numpy.core.numeric.asarray

       print("\n=== asarray 的来源 ===")
       print("__module__ =", getattr(obj, "__module__", "<无 __module__>"))

       print("\n=== 触发的警告 ===")
       for w in caught:
           print(f"[{w.category.__name__}] {w.message}")
   ```

   运行 `python u1_l1_overview.py`。
3. **需要观察的现象**：
   - 第一段输出是「`numpy.core` exists solely for backward compatibility ...」之类的自述。
   - 第二段输出的 `__module__` 里包含 `numpy._core`。
   - 第三段输出至少一条 `DeprecationWarning`，消息里提到 `numpy._core`。
4. **预期结果**：三段输出共同证明——`numpy.core` 自己声明是兼容垫片，它返回的对象来自 `numpy._core`，并且每次访问都会发出弃用警告。
5. **待本地验证**：警告的精确措辞、`__module__` 的精确取值依 NumPy 版本而定，以本地实际输出为准；只要方向（含 `numpy._core`、属 `DeprecationWarning`）一致即可。

## 6. 本讲小结

- `numpy.core` 目录**不包含任何实现**，它只是一块「向后兼容垫片」，唯一目的是让老代码继续能跑。
- NumPy 2.0 把原来的 `core` 改名为私有的 `_core`（`numpy._core`），真正的实现都搬到了那里。
- [`__init__.py` 的文档字符串](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L1-L5)是判断其身份的第一手证据：明说「仅为兼容存在」「将被移除」。
- 转发靠包级 [`__getattr__`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33)：用 `getattr(_core, attr_name)` 从 `numpy._core` 取对象，再调用 `_raise_warning` 提示弃用。
- 访问垫片返回的对象，其真实来源是 `numpy._core`（可通过 `__module__` 验证）。

## 7. 下一步学习建议

本讲只建立了「`numpy.core` 是垫片」的全局认知，还没有深入它的内部结构和工作机制。建议接下来：

- 学习 **u1-l2（目录结构与文件分类）**：把 `numpy/core` 下约 30 个文件按「包入口 / 工具 / 纯转发垫片 / 类型存根」分类，建立完整心智地图。
- 学习 **u1-l3（运行 numpy.core：触发并捕获 DeprecationWarning）**：动手捕获并断言那条弃用警告，理解「警告只提示、不阻断」的行为。
- 之后再进入第 2 单元，深入 `__getattr__`、惰性加载、`_raise_warning` 与 `stacklevel` 等核心机制。

> 阅读提示：后续讲义会频繁引用 `__init__.py`、`_utils.py` 和各个子模块垫片（如 `numeric.py`），本讲建立的「转发到 `_core`」的心智模型是理解它们的基础。
