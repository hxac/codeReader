# 公共 API 与目录结构：public 壳与 private 实现

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `numpy.typing` 对外只暴露哪几个名字，以及它们由谁定义。
- 解释为什么 NumPy 要把类型子系统拆成「公共壳 `numpy.typing`」与「私有实现 `numpy._typing`」两层。
- 读懂 `numpy._typing/__init__.py` 里「大量 `import X as X` 聚合」在写什么、为什么这么写。
- 会用 `__all__`、`dir()`、模块级 `__getattr__`/`__dir__` 来观察并画出「公共 API ↔ 私有实现」的边界。

## 2. 前置知识

本讲承接 [u1-l1（numpy.typing 是什么）](u1-l1-what-is-numpy-typing.md)。你已经知道：

- **静态类型检查**在程序运行前进行（mypy / pyright），其结论可能与**运行时**行为不一致；
- `numpy.typing` 是 NumPy 自 1.20 引入的类型子系统，采用「公共壳 + 私有实现」的分层。

本讲需要补充两个 Python 基础概念：

1. **`__all__` 与「公共 API 契约」**
   `__all__` 是模块顶部的一个列表。它有两层作用：一是声明「这个模块的公共名字就是这些」；二是决定 `from module import *` 时会导入哪些名字。不在 `__all__` 里的名字，原则上被视为模块的**内部细节**，使用者不应依赖。

2. **再导出（re-export）与 `import X as X`**
   一个模块把别处定义的名字「搬」到自己名下供外部使用，叫再导出。PEP 484 规定：写 `from m import X` 时，类型检查器默认认为 `X` 只是本模块**自用**的导入；而写成 `from m import X as X`（名字相同），则是**显式告诉**类型检查器「我要把 `X` 重新导出给别人」，于是 `from 当前模块 import X` 才会被认为是合法的公共导入。

3. **public / private 命名约定**
   Python 没有真正的「私有」，但有约定：名字以单下划线 `_` 开头（如 `_typing`、`_SupportsArray`）表示「内部使用，别从外部依赖」。本讲要看的，正是带下划线的私有包如何支撑不带下划线的公共模块。

## 3. 本讲源码地图

本讲只盯住「分层」这件事，涉及的真实文件如下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `numpy/typing/__init__.py` | **公共壳（运行时）** | 一行 import + `__all__` + 收窄 `dir()`/属性访问的模块级 `__getattr__`/`__dir__`。 |
| `numpy/typing/__init__.pyi` | **公共壳（类型检查）** | 类型检查器实际读取的桩文件，只有 import 与 `__all__`。 |
| `numpy/_typing/__init__.py` | **私有聚合中枢** | 用 8 个 `from ._子模块 import ... as ...` 块，把上百个内部别名汇聚到一个包出口。 |
| `numpy/_typing/_nbit_base.py` | 私有实现示例 | 真正定义 `NBitBase` 类的地方，可用来验证「私有实现被公共壳重新包装」。 |

> 后续讲义（u2、u3、u4）会逐个深入 `_typing/` 下的 `_array_like.py`、`_dtype_like.py`、`_nbit_base.py` 等；本讲只关心它们如何被「聚合 + 转发」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：`__all__`（公共 API 契约）、`numpy.typing`（公共壳）、`numpy._typing`（私有聚合）。

### 4.1 `__all__`：公共 API 的契约

#### 4.1.1 概念说明

一个库可以定义成百上千个名字，但只希望用户依赖其中一小部分。`__all__` 就是用来圈定这「一小部分」的清单。对 `numpy.typing` 而言，这张清单只有四个名字：

- `ArrayLike`：一切「可以转成数组」的对象；
- `DTypeLike`：一切「可以转成 dtype」的对象；
- `NDArray`：带形状/元素类型参数的数组别名；
- `NBitBase`：表达数值精度的辅助类型（自 2.3 起弃用）。

理解 `__all__` 是理解整个分层的第一把钥匙：公共壳之所以「薄」，正是因为它对外只认这四个名字。

#### 4.1.2 核心流程

`__all__` 在三处发挥作用：

1. **约束 `from numpy.typing import *`**：只导入清单里的四个名字，其余全部屏蔽。
2. **作为文档**：读源码的人一眼就能看出公共面有多大。
3. **被模块级 `__dir__()` 复用**（见 4.2）：`dir(numpy.typing)` 展示的内容也以它为基础。

一句话：`__all__` = 公共 API 的「白名单」。

#### 4.1.3 源码精读

公共壳在运行时声明了这张清单：

[\_\_init\_\_.py:177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L177) — 在 `numpy/typing/__init__.py` 中用列表显式声明四个公共名字。

桩文件里也有一份完全相同的声明：

[\_\_init\_\_.pyi:8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.pyi#L8) — 类型检查器读取的 `.pyi` 中 `__all__` 与运行时一致，保证「检查时」和「运行时」对公共面的认知相同。

注意：四个名字在 `numpy/typing/__init__.py` 中**并不在本地定义**，而是从私有包导入（见 4.2、4.3）。`__all__` 只负责「点名」，不负责「制造」。

#### 4.1.4 代码实践

**目标**：确认 `__all__` 确实是公共 API 的白名单，并约束 `import *`。

```python
# 示例代码
import numpy.typing as npt

# 1. 查看公共契约
print("public:", npt.__all__)
# 期望: ['ArrayLike', 'DTypeLike', 'NBitBase', 'NDArray']

# 2. 在一个新命名空间里做 import *，看实际进来了什么
ns = {}
exec("from numpy.typing import *", ns)
got = sorted(k for k in ns if not k.startswith("_"))
print("import * 带进来的:", got)
# 期望: 恰好是 __all__ 里的那四个（顺序可能不同）
```

**需要观察的现象**：`import *` 带进来的非下划线名字，是否**恰好**等于 `__all__`。

**预期结果**：是的——即便 `numpy.typing` 模块内部还有 `test`（PytestTester）等其它名字，`import *` 也只会引入这四个。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `__all__`，`from numpy.typing import *` 的行为会怎样变化？
**答案**：没有 `__all__` 时，`import *` 会退化为「导入所有不以 `_` 开头的名字」，于是像 `test` 这样的名字也会被带进来——公共面就会「漏出」内部细节。`__all__` 正是用来防止这一点。

**练习 2**：若想新增一个公共别名 `Foo`，至少要改 `numpy/typing/__init__.py` 的哪两处？
**答案**：① 在 `from numpy._typing import ...` 那一行加入 `Foo`；② 把字符串 `"Foo"` 追加进 `__all__`。（当然，`Foo` 的真正定义要落在 `numpy._typing` 的某个私有模块里。）

---

### 4.2 `numpy.typing`：极薄的公共壳

#### 4.2.1 概念说明

「壳（shell）」的意思是：这个模块自己几乎不写任何类型逻辑，只做三件事——

1. 从私有包 `numpy._typing` 把四个名字**搬**过来；
2. 用 `__all__` 声明公共面；
3. 用模块级 `__getattr__` / `__dir__`（PEP 562）**收窄**对外暴露的内容，顺带为弃用的 `NBitBase` 安排一条警告通道。

这样做的好处是：内部实现可以随便重构（拆分、重命名私有模块），只要公共壳继续转发出那四个名字，使用者的代码就不会被破坏。

#### 4.2.2 核心流程

```
用户代码:  import numpy.typing as npt
   │
   ├── 类型检查器 → 读取 numpy/typing/__init__.pyi（桩，只有 import + __all__）
   │
   └── 运行时    → 执行 numpy/typing/__init__.py：
            ① from numpy._typing import 四个名字   （第 175 行）
            ② __all__ = [...]                      （第 177 行）
            ③ 定义 __dir__ / __getattr__            （第 184–204 行，PEP 562）
            ④ 追加文档字符串                         （第 207–211 行）
            ⑤ 挂上 test = PytestTester(...)         （第 213–216 行）
```

注意「双轨」：类型检查器看 `.pyi`，运行时跑 `.py`。两者 `__all__` 相同，但 `.pyi` 里**没有** `__getattr__`/`__dir__` 这些运行时设施——它们对类型检查没有意义。（双轨制会在 u1-l3、u5-l1 专门讲。）

#### 4.2.3 源码精读

**① 一行 import 搬来全部公共名字**：

[\_\_init\_\_.py:175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175) — `from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray`，公共壳的全部「内容」都来自私有包。这一行就是公共与私有之间唯一的胶水。

**② 用 PEP 562 收窄对外暴露**：

[\_\_init\_\_.py:180-181](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L180-L181) — 先算出 `__DIR = __all__ + [所有 dunder 名字]`，再冻结成集合 `__DIR_SET`，作为「合法可访问名字」的白名单。

[\_\_init\_\_.py:184-185](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L184-L185) — `__dir__()` 只返回 `__DIR`，于是 `dir(npt)` 显示的内容被刻意收窄，连模块里真实存在的 `test` 都不会出现。

[\_\_init\_\_.py:187-204](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L187-L204) — `__getattr__(name)`：当访问的名字不在模块 `__dict__` 时被调用；若 `name == "NBitBase"` 则发 `DeprecationWarning`，若 `name in __DIR_SET` 则返回，否则抛 `AttributeError`。

> ⚠️ **一个容易踩的细节**：第 175 行已经把 `NBitBase` 绑定进了模块的 `__dict__`。而 PEP 562 的模块级 `__getattr__` 只在「`__dict__` 里找不到」时才作为回退被调用。因此，**直接访问 `npt.NBitBase` 通常会在 `__dict__` 命中、并不经过 `__getattr__`**，那条 `DeprecationWarning` 在直接属性访问时不一定触发（仓库里也没有任何测试断言它一定触发，只有 `# type: ignore[deprecated]` 这类「类型层面」的标记）。要确认在你机器上的真实行为，请按 4.2.4 本地验证。

**③ 文档与测试入口**：

[\_\_init\_\_.py:207-211](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L207-L211) — 把 `numpy._typing._add_docstring` 生成的文档片段拼到模块 docstring 末尾（因为 PEP 695 的 `type` 别名没法直接挂 docstring，详见 u5-l3）。

[\_\_init\_\_.py:213-216](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L213-L216) — 挂上 `test = PytestTester(__name__)`，所以能在终端跑 `numpy.typing.test()`。

**④ 对照桩文件**：

[\_\_init\_\_.pyi:1-6](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.pyi#L1-L6) — 类型检查器看到的版本只有 import 与 `__all__`，没有任何 `__getattr__`/`__dir__`，也没有 docstring 拼接逻辑。这正是「壳」在「检查时」的样子。

#### 4.2.4 代码实践

**目标**：亲手验证公共壳如何「收窄」对外暴露。

```python
# 示例代码
import numpy.typing as npt

# 1. dir(npt) 由 __dir__() 决定，被刻意收窄
print("len(dir(npt)) =", len(dir(npt)))

# 2. 真实运行时命名空间更大（注意这里能看到 __dir__ 刻意藏起来的 test）
hidden = [k for k in npt.__dict__ if not k.startswith("__") and k not in npt.__all__]
print("被壳藏起来的非下划线名字:", hidden)   # 预期能看到 'test' 等

# 3. 访问白名单之外的名字会触发 AttributeError
try:
    npt.definitely_not_a_name
except AttributeError as e:
    print("AttributeError:", e)

# 4. 访问 NBitBase 是否真的报警？—— 待本地验证
import warnings
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = npt.NBitBase
print("直接访问 NBitBase 时的 warning 数量:", len(w))
```

**需要观察的现象**：① `dir(npt)` 比真实 `__dict__` 更短；② 访问白名单外的名字抛 `AttributeError`；③ 第 4 步的 warning 数量。

**预期结果**：前两步符合描述；第 4 步请记录你机器上 `len(w)` 的真实值。结合 4.2.3 的提示思考：因为 `NBitBase` 已在 `__dict__` 中，`__getattr__` 的警告分支往往不会被走到——如果你观察到 `len(w) == 0`，原因就在这里。（**待本地验证**）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `len(dir(npt))` 会小于 `npt.__dict__` 里真正的名字数？
**答案**：模块定义了 `__dir__()`，只返回 `__DIR`（即 `__all__` 加 dunder），刻意不展示 `test`、`__DIR`、`__DIR_SET` 等内部名字。

**练习 2**：访问 `npt.definitely_missing` 时，调用链是怎样的？
**答案**：该名字不在 `__dict__` → 触发 PEP 562 的 `__getattr__("definitely_missing")` → 它不在 `__DIR_SET`、也不是 `"NBitBase"` → 抛 `AttributeError`。

---

### 4.3 `numpy._typing`：私有实现与聚合中枢

#### 4.3.1 概念说明

公共壳之所以能「薄」，是因为所有真正的类型别名都住在私有包 `numpy._typing` 里。这个包的 `__init__.py` 是一个**聚合中枢**：它本身不定义任何类型，只把分散在 `_array_like.py`、`_dtype_like.py`、`_nbit_base.py`、`_char_codes.py` 等十来个私有模块里的上百个别名，统一汇聚到一个包出口。

这样一来，公共壳只需 `from numpy._typing import ...` 一行，就能拿到全部四个公共名字；而 NumPy 内部其它子系统也可以复用同一批私有别名。

#### 4.3.2 核心流程

聚合中枢的工作模式可以概括为「分门别类、逐块再导出」：

```
numpy/_typing/__init__.py
   ├── from ._array_like    import (ArrayLike, NDArray, _SupportsArray, _ArrayLike*_co …)   # 数组类
   ├── from ._char_codes    import (_Float64Codes, _Int8Codes, …)                            # dtype 字符串编码
   ├── from ._dtype_like    import (DTypeLike, _VoidDTypeLike, _SupportsDType, …)            # dtype 类
   ├── from ._nbit          import (_NBitInt, _NBitDouble, …)                                # 平台精度
   ├── from ._nbit_base     import (NBitBase, _8Bit.._128Bit)                                # 精度层次
   ├── from ._nested_sequence import _NestedSequence                                          # 嵌套序列协议
   ├── from ._scalars       import (_IntLike_co, _FloatLike_co, …)                           # 标量协变别名
   ├── from ._shape         import (_Shape, _AnyShape, _ShapeLike)                            # 形状
   └── from ._ufunc         import (_UFunc_Nin2_Nout1, …)                                     # ufunc 类型建模
```

每一块都采用 `原名 as 原名` 的写法，这是 PEP 484 的「显式再导出」约定（见 4.3.3）。

#### 4.3.3 源码精读

[\_\_init\_\_.py:1](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../_typing/__init__.py#L1) — 模块 docstring 直白写道：「Private counterpart of `numpy.typing`」（`numpy.typing` 的私有对应物）。这行注释是整层分层的「官方说明」。

[\_\_init\_\_.py:3-26](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../_typing/__init__.py#L3-L26) — 从 `._array_like` 再导出。注意前两行就是公共名字 `ArrayLike as ArrayLike`、`NDArray as NDArray`，紧接着是一大串 `_ArrayLike*_co`、`_SupportsArray` 等**带下划线的内部别名**。这就是「公共四件套」与「上百个内部别名」同住一处的证据。

[\_\_init\_\_.py:74-92](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../_typing/__init__.py#L74-L92) — 从 `._dtype_like` 再导出，其中 `DTypeLike as DTypeLike` 是公共名，其余 `_DTypeLikeBool`、`_VoidDTypeLike`、`_SupportsDType` 等都是内部别名。

[\_\_init\_\_.py:110-118](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../_typing/__init__.py#L110-L118) — 从 `._nbit_base` 再导出，公共名 `NBitBase` 与内部精度类 `_8Bit.._128Bit` 一起被搬出。这一行还带了 `# type: ignore[deprecated]` 注释——因为 `NBitBase` 已弃用，类型检查器会提示「你正在导入一个弃用的名字」，注释用来在「聚合」这一层把它压住。

**`import X as X` 到底图什么？**

> 没加 `as`：`from ._array_like import ArrayLike` —— 类型检查器认为 `ArrayLike` 只是本模块**自用**，从外部 `from numpy._typing import ArrayLike` 可能被警告。
>
> 加了 `as`：`from ._array_like import ArrayLike as ArrayLike` —— 显式声明「我要把 `ArrayLike` 重新导出」，于是公共壳那一行 `from numpy._typing import ArrayLike` 才名正言顺。

**私有实现被公共壳「重新包装」的实证**：

[\_nbit\_base.py:7-9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/../_typing/_nbit_base.py#L7-L9) — `NBitBase` 物理上定义在私有模块 `_nbit_base.py`，却被装饰器 `@set_module("numpy.typing")` 改写了 `__module__`。所以 `NBitBase.__module__` 报告的是 `"numpy.typing"` 而不是 `"numpy._typing._nbit_base"`。这就是公共壳的「化妆术」：私有实现的产物，对外表现为公共模块的一部分。

#### 4.3.4 代码实践

**目标**：列出公共 API 与私有实现的真实边界。

```python
# 示例代码
import numpy.typing as npt
import numpy._typing as _npt   # 私有包，仅供学习窥探，生产代码请勿依赖

public = set(npt.__all__)
internal = {k for k in dir(_npt) if not k.startswith("__")}
extras = sorted(internal - public)

print("公共名字        :", sorted(public))
print("私有多出的别名数:", len(extras))
print("其中公共名(无下划线):", [e for e in extras if not e.startswith("_")])
print("内部别名(带下划线)前 10 个:", [e for e in extras if e.startswith("_")][:10])
```

**需要观察的现象**：公共只有 4 个名字；`_typing` 多出的别名远多于 4 个，且绝大多数以下划线开头（如 `_SupportsArray`、`_8Bit`、`_Float64Codes`）。

**预期结果**：`len(extras)` 是一个两位数（约近百个）。**精确数量待本地验证**（会随版本变化），但「私有面 ≫ 公共面」的结论是确定的。

> 小提示：本实践会 `import numpy._typing`，这个带下划线的包是「私有」的——用它来理解内部结构没问题，但不要在自己的代码里 `from numpy._typing import ...`，因为 NumPy 不保证它的稳定性。

#### 4.3.5 小练习与答案

**练习 1**：`from ._array_like import ArrayLike as ArrayLike` 里的 `as ArrayLike` 能去掉吗？为什么？
**答案**：去掉后，类型检查器会把 `ArrayLike` 当成「本模块自用」的导入，于是公共壳里 `from numpy._typing import ArrayLike` 可能被标记为从「非再导出」的名字导入。`as ArrayLike` 是 PEP 484 的显式再导出标记，去掉会破坏类型检查体验（但运行时通常仍能跑）。

**练习 2**：`NBitBase` 定义在 `numpy/_typing/_nbit_base.py`，为什么 `NBitBase.__module__` 却是 `"numpy.typing"`？
**答案**：类装饰器 `@set_module("numpy.typing")`（来自 `numpy._utils`）在类创建后把 `__module__` 改成了公共模块名，让私有实现对外表现为公共模块的产物——这是「公共壳」理念的延伸。

## 5. 综合实践

把本讲三个模块串起来，绘制一份「公共 ↔ 私有」边界清单。

1. 写一个脚本，输出一张三列表格：**公共名 | 真正定义它的私有模块 | 它是否被弃用**。
   - 提示：用 `npt.__all__` 取公共名；对每个名字查 `type(getattr(npt, name)).__module__` 或 `getattr(npt, name).__module__` 来定位私有模块。
   - 参考结论：`ArrayLike`/`NDArray` → `numpy._typing._array_like`；`DTypeLike` → `numpy._typing._dtype_like`；`NBitBase` → `numpy._typing._nbit_base`。
2. 在同一脚本里统计：`numpy._typing` 里**没有**进入公共 `__all__` 的内部别名共有多少个，其中以下划线开头的占比是多少。
3. 用一句话写下你的结论，形如：「公共壳 = 4 个名字 + `__all__` 白名单 + `__dir__`/`__getattr__` 收窄；私有 = 1 个聚合 `__init__.py` + N 个实现模块」。

完成后，你就拥有了一张可以随时回看的项目「类型分层地图」。

## 6. 本讲小结

- `numpy.typing` 的公共面**只有四个名字**：`ArrayLike`、`DTypeLike`、`NBitBase`、`NDArray`，由 `__all__` 圈定。
- 公共壳 `numpy/typing/__init__.py` 极薄：一行 `from numpy._typing import ...` 搬来全部名字，再用 `__all__` + 模块级 `__dir__`/`__getattr__` 收窄对外暴露。
- 真正的实现集中在**私有包** `numpy._typing`，其 `__init__.py` 是一个聚合中枢，用 8 个 `from ._子模块 import X as X` 块汇聚了上百个内部别名。
- `import X as X` 是 PEP 484 的「显式再导出」写法，让公共壳的导入名正言顺。
- `@set_module("numpy.typing")` 这类装饰器把私有实现「化妆」成公共模块的产物（如 `NBitBase.__module__`）。
- 类型检查器读 `.pyi`、运行时跑 `.py`，两者 `__all__` 一致但内容不同——这是「双轨制」的开端。

## 7. 下一步学习建议

- 下一讲 [u1-l3（PEP 561 类型分发：py.typed 与 .pyi 桩文件）](u1-l3-pep561-py-typed-stubs.md) 会接着本讲的「双轨制」往下讲：`py.typed` 标记如何让 NumPy 成为「自带类型」的包，以及 `test_isfile.py` 如何验证桩文件随包安装。
- 想直接看「四个公共名字到底怎么定义」的读者，可以跳到单元 2：先读 [u2-l1（ArrayLike）](u2-l1-arraylike.md)，对照本讲看到的 `numpy/_typing/_array_like.py`，体会「聚合出口」背后的真实类型构造。
- 建议同时打开 `numpy/_typing/__init__.py` 与本讲对照阅读，亲手数一数它再导出了多少个名字——这是建立全局直觉最快的方式。
