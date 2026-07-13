# 类型存根策略：废弃模块的 .pyi 怎么写

## 1. 本讲目标

本讲只回答一个问题：**当一个动态模块被废弃、改用转发垫片后，它的类型存根（`.pyi`）该怎么写？**

学完后你应该能够：

1. 说清楚 `.pyi` 与 `.py` 在「是否会触发弃用警告」上的根本区别。
2. 识别 `numpy/core` 下三类 `.pyi` 写法：完整再导出、省略再导出（附 NOTE）、近空/空。
3. 用「可验证性」这条原则，判断一个废弃模块该用哪一类存根。
4. 理解 `from ... import *` 之后为什么还要补一行 `from ... import __all__ as __all__`。
5. 看懂包入口 `__init__.pyi` 对「没有存根的子模块」用 `ModuleType` 兜底的做法。

## 2. 前置知识

阅读本讲前，请确保你已经掌握以下内容（前序讲义已建立）：

- **u1-l2 / u1-l3**：`numpy/core` 是一个向后兼容垫片目录；运行时访问它的属性会经由模块级 `__getattr__` 触发 `DeprecationWarning`。
- **u2-l4**：包入口 `__init__.py` 用 `__all__` 只声明子模块名、顶部一个都不 import，强制每次访问都走 `__getattr__`；而对应的 `__init__.pyi` 走的是「相反路线」——全量 import 子模块。

本讲会把 `.pyi` 这条「相反路线」讲透。需要你事先了解的两个常识：

- **类型存根（stub，`.pyi`）**：一种只给类型检查器（mypy / pyright 等）阅读的「类型说明书」，**Python 解释器在运行时根本不会执行它**。一个模块若同时存在 `.py` 和 `.pyi`，解释器用 `.py`、类型检查器用 `.pyi`。
- **`from module import *` 的规则**：当 `module` 定义了 `__all__`，`import *` 只导入 `__all__` 里列出的名字；而**所有以 `_` 开头的名字（包括 `__all__` 本身）默认不会被 `import *` 带入**，除非它们被显式列进 `__all__`。

> 关键直觉：`.pyi` 是「静态图纸」，`.py` 是「运行时机器」。弃用警告是机器运转时的副作用，图纸不会发出任何声响。

## 3. 本讲源码地图

本讲涉及的文件全部在 `numpy/core/` 下，共 5 个 `.pyi`：

| 文件 | 行数 | 角色 | 对应运行时 `.py` |
| --- | --- | --- | --- |
| `numeric.pyi` | 4 行 | 完整再导出型 | `numeric.py`（纯转发垫片） |
| `umath.pyi` | 4 行 | 完整再导出型 | `umath.py`（纯转发垫片） |
| `overrides.pyi` | 8 行注释 | 省略再导出型（附 NOTE） | `overrides.py`（纯转发垫片） |
| `_internal.pyi` | 1 行注释 | 近空型 | `_internal.py`（特殊垫片） |
| `__init__.pyi` | 46 行 | 包入口存根 | `__init__.py`（包入口） |

辅助佐证的「上游真模块」（在 `numpy/_core/` 下，不在本讲目录，仅用于验证存根策略的依据）：

- `numpy/_core/numeric.py`：定义了 `__all__`（约第 73 行）。
- `numpy/_core/umath.py`：定义了 `__all__`（约第 44 行）。
- `numpy/_core/overrides.py`：**没有** `__all__`，也**没有** `__dir__`。
- `numpy/_core/_internal.py`：**没有** `__all__`，也**没有** `__dir__`。

## 4. 核心概念与源码讲解

### 4.1 静态与运行时的分离：为什么存根不会报警

#### 4.1.1 概念说明

在前几讲里我们反复强调：访问 `numpy.core.numeric.asarray` 时，运行时会走 `numeric.py` 的模块级 `__getattr__`，进而调用 `_utils._raise_warning` 抛出 `DeprecationWarning`。

但类型检查器（mypy、pyright）**根本不执行 Python 代码**——它们只读文本。当它们分析 `import numpy.core.numeric` 时，由于 `numpy/core/numeric.pyi` 存在，类型检查器会**只看 `.pyi`、完全忽略 `.py`**。而 `.pyi` 里没有任何 `warnings.warn`，自然也不会「报警」。

这就引出本讲最核心的认知：

> **弃用警告是运行时行为，由 `.py` 的 `__getattr__` 产生；类型存根是静态信息，与警告完全解耦。** 一个模块即使被彻底废弃，它的 `.pyi` 依然可以是一份「干净、完整、毫无弃用气息」的类型说明书。

那么问题来了：既然 `.pyi` 不负责报警，它唯一的工作就是「把名字和类型告诉类型检查器」。废弃模块的名字清单从哪里来？这就是后面三种策略的分水岭。

#### 4.1.2 核心流程

类型检查器处理一个带存根的废弃模块，分三步：

1. **定位存根**：看到 `numpy/core/numeric.py` 同目录有 `numeric.pyi`，决定「用存根」。
2. **解析存根内容**：读取 `.pyi` 顶层声明的名字（`import *`、显式 `import`、变量标注等）。
3. **对外暴露这些名字**：用户代码 `from numpy.core.numeric import asarray` 是否合法、`asarray` 是什么类型，全部以 `.pyi` 为准。

注意第 2 步：存根里**写了什么，类型检查器就只知道什么**；存根里没写的名字，在严格模式下会被判为「未知属性」。

#### 4.1.3 源码精读

我们先看一份「干净到毫无弃用气息」的存根。整个 `numeric.pyi` 只有 4 行：

[numeric.pyi:L1-L4](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi#L1-L4) —— 第 1 行只是注释 `# deprecated module`，第 3、4 行用 `import *` 把上游 `numpy._core.numeric` 的全部公开名字搬过来。**没有任何 `DeprecationWarning`、没有任何 `__getattr__`。**

对照运行时的 [numeric.py:L1-L12](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12)：`.py` 里只有 `__getattr__`，靠转发 + `_raise_warning` 工作；`.pyi` 里则是 `import *`，靠静态再导出工作。两者**完全独立**，连长得都不像。

#### 4.1.4 代码实践

实践目标：亲眼确认「存根不执行」。

1. 新建目录 `stub_demo/`，放入两个文件：
   - `stub_demo/m.py`：写一行会在 import 时打印的代码，例如 `print("m.py 被运行了")` 并定义 `x = 1`。
   - `stub_demo/m.pyi`：只写 `x: int`（不写 print）。
2. 在 `stub_demo` 外面运行两段对比：
   - `python -c "import stub_demo.m"` —— 解释器用 `.py`，会看到打印。
   - `pyright` 或 `mypy` 分析引用 `stub_demo.m` 的代码 —— 只读 `.pyi`，**不会**出现「m.py 被运行了」。
3. 需要观察的现象：运行时走 `.py`、有副作用；静态检查走 `.pyi`、无副作用。
4. 预期结果：两条路径互不干扰，证明存根与运行时分离。若本地没装 pyright/mypy，此步标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `numeric.pyi` 删掉，类型检查器还能知道 `numpy.core.numeric.asarray` 的类型吗？
  - **答案**：能，但来源变了——类型检查器会退回去读 `numeric.py`，而 `.py` 里只有 `__getattr__`，`asarray` 并未显式出现，于是 `asarray` 会被推断为 `Any`（或严格模式下报「未知属性」）。存根存在的意义之一，就是给类型检查器一份确定的名字清单。

- **练习 2**：为什么 `.pyi` 里不写 `warnings.warn(...)` 来「让类型检查器也知道这个模块废弃了」？
  - **答案**：因为 `warnings.warn` 是运行时函数调用，类型检查器不会「执行」它，写了也表达不了「废弃」的语义。要表达废弃，得用专门的 `@deprecated` 装饰器（见 4.3 节），而不是 `warn`。

---

### 4.2 完整再导出型：numeric.pyi 与 umath.pyi

#### 4.2.1 概念说明

当上游真模块（`numpy/_core/numeric.py`）**定义了 `__all__`**，垫片存根就可以用最省事的写法：`from numpy._core.numeric import *`。这一行让类型检查器顺着 import 路径，把上游存根里 `__all__` 列出的名字全部「搬」过来——无需手写任何签名。我们把这种写法叫**完整再导出型**。

它的前提是「可验证」：类型检查器能沿着 `import *` 找到一个**确定的、闭合的**名字清单（即 `__all__`），于是每个名字的类型都能被解析到。

#### 4.2.2 核心流程

`numeric.pyi` 的工作流程，从类型检查器视角看是两步：

1. 执行 `from numpy._core.numeric import *`：因上游 `numpy/_core/numeric.py` 有 `__all__`（且 `_core` 自身也有存根体系），类型检查器把 `__all__` 中每个名字的签名搬进当前模块。
2. 执行 `from numpy._core.numeric import __all__ as __all__`：把上游的 `__all__` 列表本身也搬过来，作为当前存根的 `__all__`。

为什么要单独再搬一次 `__all__`？因为 `__all__` 以 `_` 开头，按 `import *` 的规则**不会被自动带入**（即使它就在模块命名空间里）。若不补这一行，`numeric.pyi` 自己就没有 `__all__`，下游若再 `from numpy.core.numeric import *`，名字清单就会缺失或漂移。

#### 4.2.3 源码精读

[numeric.pyi:L1-L4](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi#L1-L4) 完整内容如下：

```python
# deprecated module

from numpy._core.numeric import *
from numpy._core.numeric import __all__ as __all__
```

- 第 1 行 `# deprecated module`：给人看的注释，对类型检查器无任何作用。
- 第 3 行：搬入全部公开名字（依赖上游 `__all__`）。
- 第 4 行：补搬 `__all__` 本身，保证「再导出」是闭合、可传递的。

`umath.pyi` 的写法逐字相同，只是换成 umath：

[umath.pyi:L1-L4](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.pyi#L1-L4) —— 同样两行 `import`，来源指向 `numpy._core.umath`。

> 佐证「可验证」前提：上游 `numpy/_core/numeric.py` 在第 73 行附近、`numpy/_core/umath.py` 在第 44 行附近都定义了 `__all__`，所以 `import *` 有确定的清单可搬。

#### 4.2.4 代码实践

实践目标：体会「`import *` 不会带入 `__all__`」，理解第 4 行为何不可省。

1. 建一个 `pkg/` 包：
   - `pkg/real.py`：定义 `__all__ = ["a", "b"]`，并定义 `a = 1`、`b = 2`。
   - `pkg/shim.pyi`：先只写一行 `from pkg.real import *`（**不要**写第二行）。
2. 在包外写一段调用代码 `from pkg.shim import a`，用 pyright/mypy 检查，记录 `a` 的类型（应能解析为 `int`）。
3. 再写 `from pkg.shim import *` 后引用 `__all__`，或在 stub 里检查 `shim.__all__`——会发现 `shim` 没有自己的 `__all__`。
4. 给 `shim.pyi` 补上 `from pkg.real import __all__ as __all__`，重新检查，确认 `shim.__all__` 现在可用。
5. 需要观察的现象：缺第二行时，`shim` 没有 `__all__`；补上后才有。
6. 预期结果：复现 numpy 第 4 行的作用。若本地无类型检查器，标注「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：`numeric.pyi` 第 1 行的 `# deprecated module` 如果删掉，对类型检查结果有影响吗？
  - **答案**：没有。它是注释，类型检查器忽略它。它只对阅读源码的人有提示作用。

- **练习 2**：如果上游 `numpy/_core/numeric.py` 突然删除了 `__all__`，`numeric.pyi` 的 `import *` 还能正常再导出吗？
  - **答案**：行为会变。没有 `__all__` 时，`import *` 会改为「导入所有不以 `_` 开头的名字」，清单不再闭合、可能漂移。这正是 4.3 节 `overrides.pyi` 不敢用 `import *` 的原因。

---

### 4.3 省略再导出型：overrides.pyi 的 NOTE 与「可验证」原则

#### 4.3.1 概念说明

`overrides.pyi` 是三种写法里最有教学价值的一种——它**主动选择不写任何再导出**，只用一段 NOTE 注释解释「为什么不写」。

原因在于一条贯穿全讲的**可验证性原则**：类型存根承诺的每个名字，类型检查器都应该能解析到确定类型。而 `numpy/_core/overrides.py` 既**没有 `__all__`，也没有 `__dir__`**——它运行时虽然通过 `__getattr__` 动态转发任意名字，但这种「动态」对类型检查器是不可见的。若硬要在 `overrides.pyi` 里写 `import *`，要么搬不到闭合清单，要么需要**逐字复制**上游全部签名。

再加上 `overrides` 本就是「废弃 + 非公开 API」，维护者权衡后选择：**留一段 NOTE，什么都不再导出**。

#### 4.3.2 核心流程

维护者在为一个废弃动态模块写存根时，决策树如下：

1. 上游有 `__all__` 或 `__dir__` 吗？
   - 有 → 走 4.2 的「完整再导出」（`import *`）。
   - 没有 → 进入第 2 步。
2. 这个模块是公开 API 吗？
   - 是 → 即便要逐字复制签名也得写，否则用户代码类型会变 `Any`。
   - 否（废弃 + 非公开）→ 走「省略再导出」，只留 NOTE 说明理由。

`overrides` 落在第 2 步的「否」分支，于是得到一份只有注释的存根。

NOTE 里还点出了另一个关键点：要真正在存根里表达「废弃」，需要给每个函数套上 `@deprecated` 装饰器（`typing_extensions.deprecated` / PEP 702）。而装饰器**不能套在 `import *` 搬来的名字上**——必须逐个手写签名再装饰。对非公开的废弃模块，这份复制成本不值得付出，于是干脆省略。

#### 4.3.3 源码精读

[overrides.pyi:L1-L8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/overrides.pyi#L1-L8) 整个文件就是一段注释：

```python
# NOTE: At runtime, this submodule dynamically re-exports any `numpy._core.overrides`
# member, and issues a `DeprecationWarning` when accessed. But since there is no
# `__dir__` or `__all__` present, these annotations would be unverifiable. Because
# this module is also deprecated in favor of `numpy._core`, and therefore not part of
# the public API, we omit the "re-exports", which in practice would require literal
# duplication of the stubs in order for the `@deprecated` decorator to be understood
# by type-checkers.
```

逐句拆解：

- 第 1-2 句：运行时它会动态再导出 `numpy._core.overrides` 的任意成员并报警——这是「事实」。
- 第 2-3 句（`But since ... unverifiable`）：**因为没有 `__dir__` 或 `__all__`，这些再导出对类型检查器是「不可验证」的**——这是「省略」的核心理由。
- 第 3-5 句（`Because ... not part of the public API`）：又因它已废弃、非公开，所以省略是可接受的——这是「省略」的合理性。
- 第 5-7 句（`which in practice ... by type-checkers`）：真要在存根里标废弃，得逐字复制签名以便套 `@deprecated` 装饰器——这是「省略」的成本考量。

对照运行时 [overrides.py:L1-L10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/overrides.py#L1-L10)：`.py` 用 `None` 写法（u2-l2 讲过）动态转发任意名字；正是这种「任意名字」让存根无法闭合，逼出了「省略」策略。

#### 4.3.4 代码实践

实践目标：复现「不可验证」，体会 NOTE 描述的困境。

1. 建包 `ov/`：
   - `ov/real.py`：定义几个函数（如 `def f(): ...`），**故意不写** `__all__`。
   - `ov/shim.pyi`：尝试写 `from ov.real import *`。用 pyright/mypy 检查后，观察 `shim.f` 是否被识别、以及是否有「未解析导入」之类的提示。
2. 把 `ov/real.py` 加上 `__all__ = ["f"]`，重新检查，对比识别效果。
3. 再尝试给 `shim.pyi` 里的 `f` 加 `@deprecated` 装饰（需先 `from typing_extensions import deprecated`），观察：装饰 `import *` 搬来的名字是否可行（多数检查器要求你显式写出 `def f() -> None: ...` 才能装饰）。
4. 需要观察的现象：无 `__all__` 时再导出不可靠；装饰 `import *` 的名字需要逐字复制签名。
5. 预期结果：亲手验证 NOTE 的两个论点（不可验证、需逐字复制）。若本地无类型检查器，标注「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：`overrides.pyi` 里全是注释，等于这个模块对类型检查器「不存在任何名字」。用户写 `from numpy.core.overrides import array_function_dispatch` 会被类型检查器怎么处理？
  - **答案**：在严格模式下，类型检查器会报「`overrides` 没有属性 `array_function_dispatch`」（或视配置降级为 `Any`）。因为存根只声明了注释、没有任何名字。维护者接受这个代价，因为它是非公开的废弃模块。

- **练习 2**：为什么维护者不干脆给 `numpy/_core/overrides.py` 补一个 `__all__`，这样 `overrides.pyi` 不就能用 `import *` 了吗？
  - **答案**：那会改变上游真模块的公开面，牵连 `numpy._core.overrides` 自身的行为，代价远大于「一个废弃垫片的存根省略」。存根策略应顺应上游现状，而不是反过来要求上游改造。

---

### 4.4 近空存根：_internal.pyi 与「最小标注」

#### 4.4.1 概念说明

`_internal.pyi` 比注释型更极端：它只有一行 `# deprecated module`，连 NOTE 都没有。这种「近空存根」用于**私有、内部、废弃**的模块——它们既不是公开 API，也不值得花力气维护类型信息，但仍然需要一个 `.pyi` 文件存在，以免类型检查器报「找不到存根」。

注意 `_internal` 在运行时其实是个**特殊垫片**（u3-l1 讲过）：它 eager 绑定了 `_reconstruct` 和 `_dtype_from_pep3118`，是为旧 pickle 兼容服务的。但这些细节**没有**进入存根——因为存根面向的是「类型」，而 pickle 重建是「运行时行为」，类型检查器无需关心。

#### 4.4.2 核心流程

近空存根的策略：

1. 保留一个 `.pyi` 文件，内容仅一行注释，声明「这是废弃模块」。
2. 不声明任何名字、不 import 任何东西。
3. 效果：类型检查器认得这个模块存在，但对其内部名字一无所知（一律 `Any` 或严格模式下未知）。

这是「最小标注」：用最低成本满足「有存根」这一形式要求，不投入任何维护。

#### 4.4.3 源码精读

[_internal.pyi:L1-L1](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.pyi#L1-L1) —— 全部内容就是：

```python
# deprecated module
```

对照运行时 [_internal.py:L1-L27](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py#L1-L27)：`.py` 里既有 `_reconstruct`、`_dtype_from_pep3118` 的 eager 绑定，又有 `__getattr__` 转发；`.pyi` 里却什么都没有。这再次印证 4.1 的结论：**存根与运行时是完全不同的两份文件，服务于两个不同的消费者**。

同类的还有 `_dtype.pyi`、`_dtype_ctypes.pyi`——它们是 **0 字节空文件**，连注释都没有，是「近空」的极致形态（本讲目录里确有这两个空文件，可作为佐证）。

#### 4.4.4 代码实践

实践目标：对比「近空存根」与「无存根」在类型检查器眼中的差异。

1. 建包 `priv/`：
   - `priv/real.py`：定义 `def helper() -> int: return 1`。
   - 准备两个垫片场景：
     - A：`priv/internal.pyi` 只写 `# deprecated module`（近空）。
     - B：把 `priv/internal.pyi` 删掉（无存根），但 `priv/internal.py` 用 `__getattr__` 转发 `real`。
2. 写调用代码 `from priv import internal` 后用 `internal.helper()`，分别在 A、B 下用 pyright/mypy 检查。
3. 需要观察的现象：
   - A（近空存根）：检查器认得 `internal` 是模块，但 `helper` 未知（`Any` 或报错）。
   - B（无存根）：检查器可能完全读不懂 `__getattr__` 转发，行为更不可预测。
4. 预期结果：近空存根至少保证了「模块存在」这一信息。若本地无类型检查器，标注「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：`_internal.py` 运行时有 `_reconstruct`，但 `_internal.pyi` 里没写。类型检查器知道 `numpy.core._internal._reconstruct` 吗？
  - **答案**：不知道（在严格模式下报未知，或降级为 `Any`）。`_reconstruct` 是为 pickle 重建准备的运行时名字，不属于需要静态类型保证的公开面，故未进存根。

- **练习 2**：`_internal.pyi`（1 行注释）和 `_dtype.pyi`（0 字节）在效果上有差别吗？
  - **答案**：对类型检查器而言几乎无差别——都没有声明任何名字。注释只对人有意义。两者都属于「近空/空」策略。

---

### 4.5 包入口存根：__init__.pyi 与「没有存根的子模块」兜底

#### 4.5.1 概念说明

`__init__.pyi` 是整个目录存根的「总入口」。回顾 u2-l4：运行时的 `__init__.py` 用 `__all__` 只声明子模块名、**顶部一个都不 import**，目的是逼用户每次访问都走 `__getattr__`、从而打警告。

存根走了**完全相反**的路线：它**显式 import 全部子模块**（`from . import (...)`）。原因还是 4.1 的逻辑——存根不负责报警，它只负责给类型检查器准确的子模块类型。显式 import 能让每个子模块名都绑定到它自己的 `.pyi`（如 `numeric` 绑到 `numeric.pyi`），类型最精确。

但目录里有一个「例外」子模块 `_multiarray_umath`：它在**上游 `numpy/_core/` 也没有存根**，无法 `import *` 再导出。存根对此用一个变量标注 `_multiarray_umath: ModuleType` 兜底。

#### 4.5.2 核心流程

`__init__.pyi` 的三段式：

1. 显式 `from . import (...)` 把 15 个有存根的子模块搬进来，每个都获得精确类型。
2. 声明 `__all__` 列表（含 `_multiarray_umath` 等），供 `import *` 使用。
3. 对没有存根的 `_multiarray_umath`，写 `_multiarray_umath: ModuleType`——告诉检查器「这是一个模块对象」，其属性访问一律放行（返回 `Any`）。

第 3 步是关键兜底：`ModuleType` 是 `types.ModuleType` 的标注，任何 `x._multiarray_umath.anything` 都不会报错，代价是失去精确类型。这是「没有更好信息时的最低保证」。

#### 4.5.3 源码精读

[__init__.pyi:L1-L46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L1-L46) 分三部分：

```python
# deprecated module
from types import ModuleType
from . import (            # 第 5-22 行：显式搬入全部有存根的子模块
    _dtype, _dtype_ctypes, _internal, arrayprint, defchararray,
    einsumfunc, fromnumeric, function_base, getlimits, multiarray,
    numeric, numerictypes, overrides, records, shape_base, umath,
)
__all__ = [ "_dtype", ... "_multiarray_umath", ... "umath" ]   # 第 24-42 行
# `numpy._core._multiarray_umath` has no stubs, so there's nothing to re-export
_multiarray_umath: ModuleType                                 # 第 44-45 行
```

- 第 1 行 `# deprecated module`：注释，标记整个包废弃。
- 第 5-22 行：与运行时 `__init__.py`「一个都不 import」相反，这里「全量 import」。
- 第 24-42 行 `__all__`：列出的子模块名与运行时 `__init__.py` 的 `__all__` 保持一致（含 `_multiarray_umath`）。
- 第 44-45 行：注释点明 `_multiarray_umath` 上游无存根，故用 `ModuleType` 兜底。

这一兜底与 u3-l2 讲的 `_multiarray_umath` 运行时身份呼应：它在运行时是处理 ABI 冲突的特殊垫片（对 `_ARRAY_API` 抛 `ImportError`），类型层面则退化为「一个普通模块对象」。

#### 4.5.4 代码实践

实践目标：复现「无存根子模块」的 `ModuleType` 兜底。

1. 建包 `mypkg/`：
   - `mypkg/__init__.pyi`：`from . import good`，再写 `bad: ModuleType`（用 `from types import ModuleType`）。
   - `mypkg/good.pyi`：`def f() -> int: ...`（有存根）。
   - `mypkg/bad.py`：随便定义些东西，但**不要**给 `bad.pyi`（无存根）。
2. 写调用代码：`from mypkg import good, bad`；`good.f()` 与 `bad.whatever()` 各自检查。
3. 需要观察的现象：`good.f()` 有精确类型（`int`）；`bad.whatever()` 不报错但返回 `Any`（因 `bad` 被标注为 `ModuleType`）。
4. 预期结果：复现 numpy 对 `_multiarray_umath` 的处理思路。若本地无类型检查器，标注「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `__init__.pyi` 用显式 `from . import (...)`，而不是像运行时那样靠 `__getattr__`？
  - **答案**：因为存根的目标是「给类型检查器精确类型」。显式 import 让每个子模块名绑定到各自的 `.pyi`，类型最准；而 `__getattr__` 在存根里无法表达「返回什么具体类型」，会退化成 `Any`，丢失精度。

- **练习 2**：如果把第 45 行 `_multiarray_umath: ModuleType` 删掉，会怎样？
  - **答案**：`__all__` 里仍列着 `_multiarray_umath`，但类型检查器找不到这个名字的声明，会报「未知导出」。`ModuleType` 标注正是为了让 `__all__` 与实际声明一致。

---

## 5. 综合实践

把本讲三类策略串起来。场景：你维护一个库，把公开模块 `mathlib.calc` 重命名成了私有的 `mathlib._calc`，需要为旧的 `mathlib.calc` 路径写兼容垫片与存根。

任务步骤：

1. **建真模块** `mathlib/_calc.py`：定义若干函数，并写 `__all__ = ["add", "pi"]`、`def add(a: int, b: int) -> int: ...`、`pi = 3`。同时再建一个**没有** `__all__` 的内部模块 `mathlib/_dyn.py`（随便定义一两个函数）。

2. **建运行时垫片**（仿 numpy.core）：
   - `mathlib/calc.py`：用模块级 `__getattr__` 转发到 `_calc`，并调一个自写的 `_raise_warning`（仿 u2-l3）。
   - `mathlib/dyn.py`：同样转发到 `_dyn`。

3. **为 `calc` 写两版存根并对比**（这是本讲核心实践任务）：
   - **A 版（完整再导出）**：`mathlib/calc.pyi` 写
     ```python
     from mathlib._calc import *
     from mathlib._calc import __all__ as __all__
     ```
   - **B 版（注释省略）**：`mathlib/calc.pyi` 只写一行 `# deprecated module`。
   - 写一段调用代码 `from mathlib.calc import add; add(1, 2)`，分别在 A、B 下用 pyright 或 mypy 检查，**记录差异**：A 版下 `add` 有精确签名（参数、返回值类型已知）；B 版下 `add` 退化为 `Any` 或被报「未知属性」。

4. **解释原因**：用本讲 4.2、4.3 的可验证性原则说明——A 版因 `_calc` 有 `__all__` 而可验证，故能完整再导出；B 版主动放弃再导出，类型信息随之丢失。

5. **延伸**：给 `mathlib/dyn.pyi` 选择正确策略——因 `_dyn` 无 `__all__`，应仿 `overrides.pyi` 写 NOTE 省略，或仿 `_internal.pyi` 写近空注释。说明你的选择依据。

6. 若本地没有 pyright/mypy，第 3、4 步标注「待本地验证」，但仍要写出你**预期**的类型检查器表现，并附理由。

完成本实践后，你应当能独立判断任何一个废弃动态模块该用哪一类存根。

## 6. 本讲小结

- `.pyi` 与 `.py` 服务于两个不同消费者：解释器跑 `.py`（产生弃用警告），类型检查器读 `.pyi`（只看类型）。**存根本身永远不会触发 `DeprecationWarning`**。
- `numpy/core` 的存根分三类，由「可验证性」决定：
  - **完整再导出**（`numeric.pyi`、`umath.pyi`）：上游有 `__all__`，用 `import *` 搬名字，再补 `import __all__ as __all__` 保证 `__all__` 闭合可传递。
  - **省略再导出 + NOTE**（`overrides.pyi`）：上游无 `__all__`/`__dir__`，再导出「不可验证」，且模块废弃非公开，故留 NOTE 说明省略理由（含「逐字复制签名才能套 `@deprecated`」的成本考量）。
  - **近空/空**（`_internal.pyi` 一行注释；`_dtype.pyi` 0 字节）：私有内部模块，最小标注，仅保证「存根存在」。
- 包入口 `__init__.pyi` 走与运行时相反的「全量 import」路线以保类型精度；对上游无存根的 `_multiarray_umath` 用 `_multiarray_umath: ModuleType` 兜底。
- 选择策略的本质，是在「类型精度」「维护成本」「模块是否公开」三者间权衡；废弃 + 非公开的模块，维护者更倾向省略或近空。

## 7. 下一步学习建议

- **下一讲 u3-l4（综合实践）**：把本讲的存根策略与前面所有机制（`__getattr__` 转发、`_raise_warning`、pickle eager 绑定、ABI 守卫）整合，亲手设计一个完整的生产级兼容垫片包，其中就包括配套的 `.pyi`。本讲是那篇综合实践的「类型层」铺垫。
- **延伸阅读**：
  - PEP 561（为包分发类型信息的规范，解释 `.pyi` 与 `py.typed` 的关系）。
  - PEP 702（`@deprecated` 装饰器，即 `overrides.pyi` NOTE 中提到的「让类型检查器理解废弃」的标准手段）。
  - 对比阅读 `numpy/_core/` 下的 `.pyi`（真模块的存根），看「非废弃」模块的存根如何写完整签名，与本讲的「再导出」策略互为参照。
