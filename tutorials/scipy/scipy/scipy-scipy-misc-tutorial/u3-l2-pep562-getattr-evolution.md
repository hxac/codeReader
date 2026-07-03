# 访问控制演进：模块级 `__getattr__` 与 PEP 562

> 阶段：advanced · 依赖：u2-l1（三个桩文件的实现：`warnings.warn` 与 `stacklevel`）、u1-l3（历史职能与退役原因）

## 1. 本讲目标

前面几讲我们看到的是 `scipy.misc` 的「现状」：一个只剩 `warnings.warn` 的墓碑桩文件。但 `scipy.misc` 并不是一开始就这样——它**曾经**用 Python 的 **PEP 562（模块级 `__getattr__` / `__dir__`）** 实现过一套相当精巧的「按名字分流」的弃用访问控制。学完本讲，你应当能够：

- 说清楚 **PEP 562** 给「模块」加上 `__getattr__` / `__dir__` 的触发时机与典型用途；
- 逐行读懂**旧版** `scipy/misc/__init__.py` 里用 `dataset_methods` 给不同名字分发不同弃用消息的逻辑；
- 评价「懒弃用访问控制」最终被简化为「导入即警告」的**原因与得失**，并能识别其中残留的脚手架代码。

## 2. 前置知识

本讲承接 u2-l1（你已经吃透了 `warnings.warn` 的三个参数与 `stacklevel` 的栈帧归因）和 u1-l3（你知道 `scipy.misc` 历史上是「杂物箱」，内容在 PR #21864 中被删空）。再补两个基础概念：

- **属性查找的「未命中兜底」**：对普通对象，写 `obj.attr` 时，Python 先在 `obj` 及其类型的字典里找 `attr`；**找不到**时，如果类型上定义了 `__getattr__`，解释器就转去调用 `type(obj).__getattr__(obj, 'attr')` 作为「兜底」。换句话说，`__getattr__` 只在**正常查找失败**时才触发，是一个「拦截点」。
- **模块（module）曾经没有这个拦截点**：在 Python 3.7 之前，模块对象的属性查找**没有**「未命中兜底」机制——`import m; m.不存在` 会直接抛 `AttributeError`，你无法在模块这一层做任何拦截。PEP 562 正是为了补上这个缺口。

> 一句话：本讲讲的是「`scipy.misc` 如何利用 PEP 562，在用户访问某个**具体名字**（如 `face`、`derivative`）的瞬间，按名字类别给出针对性弃用提示」，以及「为什么这套机制后来被整个砍掉」。

## 3. 本讲源码地图

本讲会用到**当前**与**历史**两份源码。历史版本经 `git show 43fc97efa8^:<path>` 还原（`43fc97efa8` 是「删空内容」的 PR #21864，`^` 取它**之前**那个仍保留 `__getattr__` 的版本）。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) | **当前**包入口桩文件 | 退化为「导入即警告」、无 `__getattr__` |
| [scipy/misc/\_\_init\_\_.py:L43-L60 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/__init__.py#L43-L60) | **历史**包入口，含模块级 `__getattr__`（历史链接，文件在该提交仍存在） | 「按名字分流」的懒弃用逻辑 |
| [scipy/misc/\_\_init\_\_.py:L36-L40 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/__init__.py#L36-L40) | 同上文件 | `dataset_methods` 清单与 `__dir__` |
| [scipy/misc/\_common.py:L11-L12 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/_common.py#L11-L12) | **历史** `_common`，提供五个真实函数与 `__all__`（历史链接） | 解释为何旧 `__getattr__` 的分流分支实为脚手架残留 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1)** PEP 562 的原理；**(4.2)** 旧 `scipy.misc` 如何用它做「按名字分流」的懒弃用（含一段批判性阅读）；**(4.3)** 当前桩文件为何把这套机制整个砍掉。

### 4.1 PEP 562：让模块也能在「属性未命中」时兜底

#### 4.1.1 概念说明

PEP 562（Python 3.7 起实现）给模块对象补上了与「普通对象」对等的两个钩子：

- **`__getattr__(name)`**：当 `module.NAME` 的正常查找（在模块的 `__dict__` 里找 `NAME`）**失败**时，解释器转而调用模块顶层定义的这个函数，传入被访问的名字字符串 `name`。
- **`__dir__()`**：当对模块调用 `dir(module)` 时，解释器改用这个函数的返回值来补充 `dir()` 的结果。

注意这两个函数**必须定义在模块顶层**（与函数体同级），且名字正好是 `__getattr__` / `__dir__`——解释器是按「模块属性」的特殊名字去识别它们的，定义在类或函数里都不算数。

这套机制常见用途有三类：

1. **惰性导入（lazy import）**：把昂贵的依赖推迟到真正被访问时才 `import`，加快 `import` 主包的速度。
2. **弃用提示（deprecation hooks）**：在用户访问某个已下线名字时，给出「该用什么替代」的指引——`scipy.misc` 旧版正是此用法。
3. **虚拟属性 / 子模块**：动态合成一些并不真实存在于文件系统里的名字。

核心直觉：**`__getattr__` 是「属性未命中」这一事件的处理器。** 它让你有机会在「抛 `AttributeError`」之前插一脚。

#### 4.1.2 核心流程

把「访问模块属性」的全过程用伪代码表示：

```
访问 module.NAME:
  1. 在 module.__dict__ 中查找 NAME
  2. 命中  -> 返回该值（__getattr__ 完全不参与）
  3. 未命中 -> 若模块顶层定义了 __getattr__:
                调用 __getattr__(NAME) -> 返回其结果（或它抛出的异常）
  4.        否则 -> 抛 AttributeError

dir(module):
  基础结果 = 模块 __dict__ 的键等
  若定义了 __dir__() -> 用其返回值覆盖/补充基础结果
```

最关键的一条是**第 2 步优先**：只要 `NAME` 已经在模块的 `__dict__` 里，`__getattr__` 就**永远不会**被触发。这条规则在 4.2 的批判性阅读里会变得很重要。

#### 4.1.3 源码精读

PEP 562 本身只是 Python 语言特性，没有「源码」可贴。下面是一个**最小可运行模型**（示例代码，非 SciPy 源码），用十几行演示触发时机：

```python
# 示例代码：demo/__init__.py —— 演示 PEP 562，非 scipy 源码
_REAL = {"known_a": 1, "known_b": 2}   # 模拟「真实存在于 __dict__ 的名字」

def __getattr__(name):
    # 只有当访问的名字不在 __dict__ 时，才会走到这里
    print(f"[__getattr__] 有人访问了未命中的名字: {name}")
    if name in _REAL:
        return _REAL[name]             # 动态补发一个值
    raise AttributeError(f"module 'demo' has no attribute {name!r}")

def __dir__():
    return sorted(set(_REAL))          # 让 dir(demo) 显示我们愿意承认的名字
```

> 注意：`_REAL` 里的值**并没有**真的写进 `demo.__dict__`（它只是模块里的一个普通字典变量），所以 `demo.known_a` 其实是「未命中」→ 走 `__getattr__` → 动态返回。这正是「虚拟属性」的玩法。如果你在文件里直接写 `known_a = 1`（顶层赋值），它就会进 `__dict__`，`__getattr__` 反而不会被调用——这是初学者最容易踩的坑。

真实项目里，旧版 `scipy.misc` 就是把这套机制用在了「弃用分流」上，详见 4.2。

#### 4.1.4 代码实践

**目标**：亲手验证「`__dict__` 命中优先」与「未命中才触发 `__getattr__`」这两条规则。

**操作步骤**（示例代码，不依赖 SciPy）：

1. 把上面的 `demo/__init__.py` 放到一个可导入的位置（例如当前目录下建 `demo/` 子目录）。
2. 再在文件里**加一行顶层赋值** `known_a = 999`，让 `known_a` 真正进入 `__dict__`。
3. 写脚本：

```python
import warnings, demo
print("已知名字 known_a =", demo.known_a)   # 预期：不打印 [__getattr__]，直接 999
try:
    demo.no_such_thing
except AttributeError as e:
    print("未知名字 ->", e)                  # 预期：打印 [__getattr__] 行 + AttributeError
print("dir(demo) 含 known_a?", "known_a" in dir(demo))   # 预期：True（__dir__ 返回）
```

**需要观察的现象**：

- 访问 `demo.known_a` 时**不会**打印 `[__getattr__]` 那行——因为它已在 `__dict__`，未命中兜底不触发。
- 访问 `demo.no_such_thing` 时**会**打印 `[__getattr__]`，随后抛 `AttributeError`。
- `dir(demo)` 受 `__dir__()` 影响。

**预期结果**：如上。若把第 2 步的顶层赋值删掉，`demo.known_a` 会改走 `__getattr__` 并打印那行、返回 `1`——这就是「是否进 `__dict__`」带来的差别。**待本地验证**具体打印内容。

#### 4.1.5 小练习与答案

**Q1**：如果一个模块**同时**在顶层写了 `foo = 1` 和一个会返回 `2` 的 `__getattr__`，那么 `module.foo` 等于多少？`__getattr__` 会被调用吗？
**答**：等于 `1`，`__getattr__` 不会被调用。因为 `foo` 已在模块 `__dict__` 中，正常查找命中，根本轮不到「未命中兜底」。

**Q2**：`__dir__()` 需要接受参数吗？它的返回值有什么要求？
**答**：不接受参数，签名就是 `__dir__()`。它应返回一个可迭代对象（通常是字符串列表），`dir(module)` 会用它来呈现模块的属性清单。

---

### 4.2 旧 `scipy.misc` 的「按名字分流」懒弃用

#### 4.2.1 概念说明

旧版 `scipy.misc` 想达到的体验是：**不要在 `import scipy.misc` 时就刷一屏警告，而是在用户真正伸手去拿某个具体名字（`face`、`derivative`……）时，才针对那个名字给出最贴切的弃用提示。** 这种「按需触发」的策略叫**懒弃用（lazy deprecation）**，好处是：

- 只警告「真正用到」的人，不打扰只是顺路 `import` 的人；
- 可以**按名字类别**给不同的迁移建议——数据集类名字告诉你「去 `scipy.datasets`」，其它名字告诉你「将在 v1.12.0 移除」。

实现这套策略的工具，正是 4.1 的模块级 `__getattr__`：把「按名字分流 + 发警告」的逻辑写进 `__getattr__(name)`，让它在用户访问名字的瞬间执行。

#### 4.2.2 核心流程

旧版 `__getattr__` 的三分支逻辑（伪代码）：

```
访问 scipy.misc.NAME：
  1. 先在 scipy.misc.__dict__ 找 NAME（见 4.1.2 第 2 步）
  2. 未命中 -> __getattr__(NAME):
     a. 若 NAME 不在 __all__      -> 抛 AttributeError("...has no attribute NAME")
     b. 若 NAME in dataset_methods -> msg = 「请改从 scipy.datasets 导入」
     c. 否则（其它已知名字）        -> msg = 「将在 SciPy v1.12.0 移除」
     d. warnings.warn(msg, DeprecationWarning, stacklevel=2)
     e. return getattr(NAME)       # 见下方「批判性阅读」
```

三个分支对应三类名字：**未知名字**（直接报错）、**数据集名字**（指向新家）、**其它弃用名字**（告知移除版本）。`stacklevel=2` 的作用承接 u2-l1——把警告归因到「用户写 `scipy.misc.NAME` 的那一行」，而非 `__getattr__` 内部。

#### 4.2.3 源码精读

先看清单与 `__dir__`。旧版在 [scipy/misc/\_\_init\_\_.py:L36-L40 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/__init__.py#L36-L40) 定义了 `dataset_methods` 与 `__dir__`：

```python
dataset_methods = ['ascent', 'face', 'electrocardiogram']


def __dir__():
    return __all__
```

- `dataset_methods` 是「数据集类名字」的白名单，决定哪个分支给「重定向到 `scipy.datasets`」的消息。
- `__dir__()` 返回 `__all__`，让 `dir(scipy.misc)` 只列出官方承认的名字（`__all__` 来自 `_common`，见下）。

再看核心的 `__getattr__`，[scipy/misc/\_\_init\_\_.py:L43-L60 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/__init__.py#L43-L60)：

```python
def __getattr__(name):
    if name not in __all__:
        raise AttributeError(
            "scipy.misc is deprecated and has no attribute "
            f"{name}.")

    if name in dataset_methods:
        msg = ("The module `scipy.misc` is deprecated and will be "
               "completely removed in SciPy v2.0.0. "
               f"All dataset methods including {name}, must be imported "
               "directly from the new `scipy.datasets` module.")
    else:
        msg = (f"The method `{name}` from the `scipy.misc` namespace is"
               " deprecated, and will be removed in SciPy v1.12.0.")

    warnings.warn(msg, category=DeprecationWarning, stacklevel=2)

    return getattr(name)
```

逐段说明：

- **未知名字**（第 44–47 行）：名字不在 `__all__`，直接抛 `AttributeError`，并在消息里点明「`scipy.misc` 已弃用」。这是「正确报错 + 顺带提醒」的写法。
- **数据集名字**（第 49–53 行）：名字在 `dataset_methods` 里，消息不仅说弃用，还**指明新家** `scipy.datasets`——这是最有价值的迁移指引。
- **其它弃用名字**（第 54–56 行）：给出移除版本 `v1.12.0`（注意这和数据集消息里的 `v2.0.0` 不同，对应不同名字的不同时间线，承接 u2-l2 的「版本驱动时间线」）。
- **发警告**（第 58 行）：`stacklevel=2` 让警告落在用户访问名字的那一行。
- **返回值**（第 60 行）：`return getattr(name)`——见下方批判性阅读。

#### 批判性阅读：这段分流逻辑在当时其实是「脚手架残留」

把 4.1 的「`__dict__` 命中优先」规则套进来，会发现一个微妙的事实。旧 `__init__.py` 顶部有（[同文件:L27-L34 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/__init__.py#L27-L34)）：

```python
from ._common import *
from . import _common
...
__all__ = _common.__all__
```

而当时的 [scipy/misc/\_common.py:L11-L12 @ 43fc97efa8^](https://github.com/scipy/scipy/blob/8f0bc5f3558c2d25907715430a3837f938d7b3d6/scipy/misc/_common.py#L11-L12) 还**真实定义**着全部五个函数：

```python
__all__ = ['central_diff_weights', 'derivative', 'ascent', 'face',
           'electrocardiogram']
```

`from ._common import *` 会把这五个名字**全部绑进 `scipy.misc.__dict__`**。于是：

- 对 `face` / `derivative` 等**已知名字**，正常查找在 `__dict__` 就命中了——**根本不会进入 `__getattr__`**。
- `__getattr__` 实际**只会**因为「未知名字」而被触发，也就是只走到第 44–47 行的 `raise AttributeError`。

也就是说，**第 49–56 行那两段精心写就的「分流消息」在 `43fc97efa8^` 这个版本里是不可达的死代码**——它们是更早阶段（名字已从命名空间移走、但 `__all__` 仍保留，从而 `__getattr__` 会命中）留下的脚手架，后来名字又通过 `_common` 回到了 `__dict__`，分流逻辑却没人删。

第 60 行 `return getattr(name)` 同样印证了这一点：builtin `getattr` **至少需要两个参数**（`getattr(object, name)`），只传一个 `name` 会抛 `TypeError`。这行若真被执行必然崩溃——它之所以「没事」，正是因为它不可达。这反过来揭示了 `__getattr__` 在该版本的**真实职责只剩一个副作用**：对未知名字抛一个「带弃用提示」的 `AttributeError`，而**不是**返回什么可用对象。

> 教学意义：读弃用桩代码时，别只看「它写了什么」，还要看「这些代码在当前状态下是否可达」。`scipy.misc` 这段 `__getattr__` 就是一个典型例子——逻辑很漂亮，但已被前面的 `from ._common import *` 架空。

#### 4.2.4 代码实践

**目标**：用 `git show` 取回旧版 `__init__.py`，对照源码标注三分支各自针对哪类名字，并验证「批判性阅读」的结论。

**操作步骤**：

1. 取回旧版到沙盒（**切勿覆盖源码**，重定向到 `/tmp`）：

```bash
git show 43fc97efa8^:scipy/misc/__init__.py > /tmp/old_misc_init.py
git show 43fc97efa8^:scipy/misc/_common.py   > /tmp/old_misc_common.py
```

2. 打开 `/tmp/old_misc_init.py`，定位 `dataset_methods`、两个 `msg = ...`、`warnings.warn(...)`、`return getattr(name)` 各自的行号。
3. 对下面三个访问，**纯靠读源码**推断「会命中哪个分支、消息是什么、`__getattr__` 是否会被触发」：

| 访问 | 是否进入 `__getattr__`？ | 命中分支 | 实际消息/结果 |
| --- | --- | --- | --- |
| `scipy.misc.face` |  |  |  |
| `scipy.misc.derivative` |  |  |  |
| `scipy.misc.no_such` |  |  |  |

**需要观察的现象 / 预期结果**（结合 4.2.3 的批判性阅读填写）：

- `face` / `derivative`：因 `from ._common import *` 已在 `__dict__`，**不进入** `__getattr__`，分流消息**不会**出现。（访问 `face` 本身静默；真正**调用** `face()` 时，由 `_common` 里 `_deprecated` 装饰器另发警告，详见 u2-l1/u2-l2。）
- `no_such`：进入 `__getattr__`，命中「不在 `__all__`」分支，抛 `AttributeError("scipy.misc is deprecated and has no attribute no_such.")`。

> 因为本机大概率装的是**当前**版 scipy（已无这些名字），旧版只能做**源码层分析**，不能直接 `import` 复现——这部分请标注「源码分析 / 待本地验证」。要真机复现需 `git checkout 43fc97efa8^` 后构建该历史版本，成本较高。

#### 4.2.5 小练习与答案

**Q1**：`dataset_methods` 分支的消息和「其它」分支的消息，最大的区别是什么？为什么要有这个区别？
**答**：`dataset_methods` 分支**额外指明了替代方案**（「import directly from the new `scipy.datasets` module」），而「其它」分支只说「将在 v1.12.0 移除」。区别源于两类名字的去向不同：数据集有了新家，值得告诉用户搬去哪；`derivative`/`central_diff_weights` 没有内置替代（见 u1-l3），只能告知移除时间。

**Q2**：既然分流逻辑在 `43fc97efa8^` 已不可达，为什么没人删？
**答**：典型的「脚手架滞留」。这段代码在更早的过渡阶段（名字移出、`__all__` 保留）是真正生效的；后来 `_common` 重新提供名字、`from ._common import *` 把它们塞回 `__dict__`，分流分支被架空，但因为不影响「未知名字报错」这条主路径，也没有测试覆盖「访问已知名字时 `__getattr__` 是否触发」，于是直到整个模块被删空都留在原地。这也是弃用期代码容易积灰的一个真实案例。

---

### 4.3 演进：当前桩文件为何直接砍掉 `__getattr__`

#### 4.3.1 概念说明

PR #21864（提交 `43fc97efa8`，「DEP: scipy.misc: remove all but modules」）把 `scipy.misc` 的**内容删空**：`_common` 删除、数据集早已搬去 `scipy.datasets`、`derivative` 类工具彻底移除。一旦「模块里没有任何名字可被懒分发」，整套 PEP 562 脚手架就**失去了存在理由**——`__getattr__` 再也分发不出任何真实对象。于是当前版本干脆把它简化成一个**导入即警告**的「墓碑桩文件」：只要 `import scipy.misc`，立刻、统一地警告一次，告诉所有人「这个模块要在 2.0.0 删了」。

这是一次从「**懒汉式**（按名字、访问时才警告）」到「**饿汉式**（导入时无条件警告）」的策略切换。

#### 4.3.2 核心流程

当前版本（[scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6)）的全部行为：

```
import scipy.misc:
  执行顶层 warnings.warn("...removed in 2.0.0", DeprecationWarning, stacklevel=2)
  没有 __getattr__、没有 __all__、模块 __dict__ 基本为空

访问 scipy.misc.<任何名字>:
  __dict__ 未命中，且无 __getattr__ -> 直接 AttributeError
```

#### 4.3.3 源码精读

当前的 [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) 只剩 6 行（承接 u2-l1 已精读过参数语义）：

```python
import warnings
warnings.warn(
    "scipy.misc is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

与旧版（4.2）逐项对比：

| 维度 | 旧版 `43fc97efa8^` | 当前 HEAD（`de190e7fde`） |
| --- | --- | --- |
| 警告触发时机 | **懒**：访问具体名字时（且仅未知名字真正触发） | **饿**：`import` 即触发 |
| 模块级 `__getattr__` | 有，三分流 | **无** |
| `__dir__` / `__all__` | 有 | **无** |
| 消息 | 按名字类别，多条不同 | 统一一句 |
| 模块内真实对象 | 借 `_common` 仍有 5 个函数 | **无任何对象** |
| 代码行数 | ~67 行 | 6 行 |

#### 4.3.4 代码实践

**目标**：对比「旧版懒触发」与「当前饿触发」在导入阶段的差异，体会策略切换。

**操作步骤**：

1. **当前版**（本机即可）观察「导入即警告」：

```bash
python -W always -c "import scipy.misc; print('导入完成')"
# 预期：先打印 DeprecationWarning，再打印「导入完成」
```

2. **当前版**访问任意名字，确认已是空壳：

```bash
python -c "import scipy.misc as m; print(hasattr(m, 'face'))"
# 预期：False（模块里没有任何名字）
```

3. **旧版**只能做源码分析：打开 `/tmp/old_misc_init.py`（4.2.4 已取回），确认其顶层**没有** `warnings.warn`——警告只藏在 `__getattr__` 内部。因此推论：在该历史版本里，`import scipy.misc` **本身不会**产生弃用警告，只有访问未知名字时才报错、访问已知名字时由各自 `_deprecated` 装饰器在**调用时**警告。

**需要观察的现象 / 预期结果**：

- 当前版：第 1 步导入即出警告；第 2 步 `hasattr(m, 'face')` 为 `False`。
- 旧版（源码分析）：导入阶段安静；`scipy.misc.face` 静默返回函数对象，`face()` 调用时才警告。

> 第 3 步涉及历史版本，标注「源码分析 / 待本地验证」。第 1、2 步可在装有当前 scipy 的本机直接验证。

#### 4.3.5 小练习与答案

**Q1**：为什么当前版本不再需要 `dataset_methods` 这类分流清单？
**答**：因为模块里**已经没有任何名字**可供分发——数据集早搬去 `scipy.datasets`，其它工具被删。`__getattr__` 无对象可返回，分流清单自然失去意义，连同 `__getattr__`/`__dir__`/`__all__` 一起被删干净。

**Q2**：饿汉式（导入即警告）比懒汉式「更吵」，SciPy 为什么仍接受？
**答**：两个原因。(1) **内容已删，懒分发无物可发**——保留懒策略只会让用户在「访问得到 `AttributeError`」时才后知后觉，体验更差。(2) **模块临终**：它将在 2.0.0 被彻底移除，让所有还在 `import scipy.misc` 的人**尽早、无差别**感知，比「按名字悄悄提示」更安全，也更利于 CI 用 `-W error::DeprecationWarning` 兜底（见 u1-l2）。

---

## 5. 综合实践

把 4.1～4.3 串成一个完整任务：**在沙盒里复刻旧 `scipy.misc` 的「按名字分流 `__getattr__`」，再做一个对照用的「墓碑桩文件」，最后写一段对比说明。**

1. 用 `git show 43fc97efa8^:scipy/misc/__init__.py` 重读旧版，确认三分支结构（4.2）。
2. 在沙盒建一个独立包 `legacy_misc/`，其 `__init__.py`（**示例代码**）实现「按名字分流」：

```python
# legacy_misc/__init__.py —— 示例代码：沙盒复现旧 scipy.misc 的 __getattr__
import warnings

# 假装这两个名字还「官方存在」
__all__ = ['ascent', 'derivative']
dataset_methods = ['ascent']          # 数据集类 -> 重定向消息

def __dir__():
    return __all__

def __getattr__(name):
    if name not in __all__:           # 未知名字：报错 + 提醒
        raise AttributeError(f"legacy_misc is deprecated and has no attribute {name!r}")
    if name in dataset_methods:       # 数据集名字：指明新家
        msg = (f"legacy_misc.{name} is deprecated; import it from `mylib.datasets` instead.")
    else:                             # 其它名字：告知移除版本
        msg = (f"legacy_misc.{name} is deprecated and will be removed in mylib 2.0.")
    warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
    return None                       # 旧版 return getattr(name) 是死代码；这里返回 None 即可演示「副作用才是目的」
```

3. 写测试 `test_legacy.py`，用 `warnings.catch_warnings(record=True)` 验证：

```python
import warnings, legacy_misc

# (a) 仅 import 不应产生警告（懒策略）
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    import importlib; importlib.reload(legacy_misc)
    assert len(w) == 0, "导入阶段不应警告（懒策略）"

# (b) 访问数据集名字 -> 重定向消息
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    legacy_misc.ascent
    assert len(w) == 1 and "datasets" in str(w[0].message)

# (c) 访问其它已知名字 -> 移除版本消息
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    legacy_misc.derivative
    assert len(w) == 1 and "2.0" in str(w[0].message)

# (d) 未知名字 -> AttributeError
try:
    legacy_misc.no_such
    assert False
except AttributeError:
    pass
print("全部断言通过")
```

4. 再建一个对照包 `tombstone/`，`__init__.py` 完全照抄当前的 [scipy/misc/\_\_init\_\_.py:L1-L6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6)（消息换成你自己的），验证：`import tombstone` **立刻**出一条警告，且 `tombstone` 里没有任何名字。
5. 写约 200 字对比：从「懒 vs 饿」「按名字分流 vs 统一一句」「`__getattr__` 有无」「真实对象有无」四个角度，说明两种实现的差异，以及为什么删空内容后「墓碑式」更合理。

**需要观察的现象 / 预期结果**：`legacy_misc` 访问特定名字才警告、且消息按类别不同；`tombstone` 一导入就警告、无名字可访问。如果本机不方便建包运行，可把第 3 步断言改写成「预期 vs 实际」表格人工推演，并标注「待本地验证」。

## 6. 本讲小结

- **PEP 562** 让模块也能定义顶层 `__getattr__(name)` / `__dir__()`：当模块属性在 `__dict__` **未命中**时，解释器转去调用 `__getattr__`，从而能在「抛 `AttributeError`」前插一脚。
- 旧 `scipy.misc` 用它做**按名字分流的懒弃用**：未知名字报错、数据集名字指向 `scipy.datasets`、其它名字告知移除版本，配 `stacklevel=2` 归因到用户访问行。
- **批判性阅读**：在 `43fc97efa8^` 版本，`from ._common import *` 已把全部名字塞进 `__dict__`，导致分流分支**不可达**，是更早阶段的脚手架残留；`return getattr(name)` 因 `getattr` 缺参数本会抛 `TypeError`，进一步印证 `__getattr__` 的真实职责是「副作用（报错/警告）」而非返回值。
- PR #21864 删空内容后，懒分发**无物可发**，当前版本退化为「导入即警告」的 6 行墓碑桩文件，`__getattr__`/`__dir__`/`__all__` 全部移除。
- 演进的**得**：极简、零脚手架、让所有人尽早无差别感知；**失**：失去按名字的精确迁移指引（但这在「内容已删」的前提下本就无法兑现）。

## 7. 下一步学习建议

- 下一讲 **u3-l3（综合实践：把旧代码从 `scipy.misc` 迁移出去）** 会把本讲的认知落到实际迁移：数据集走 `scipy.datasets`，`derivative` 改手写差分或外部库，并用 `python -W error` 在 CI 兜底。
- 想看 SciPy **当前仍在用**的「软弃用 + 重定向」机制，可精读 [scipy/_lib/deprecation.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py) 里的 `_sub_module_deprecation`（承接 u2-l2）——它用 `stacklevel=3` 实现了「旧入口仍可用、但访问时重定向到新位置并警告」，可视为本讲 `__getattr__` 分流思路的「官方升级版」。
- 建议阅读 **PEP 562** 原文（*Module level `__getattr__`/`__dir__`*），把本讲的最小模型与语言规范对齐，理解解释器为何只认「模块顶层」的这两个特殊名字。
