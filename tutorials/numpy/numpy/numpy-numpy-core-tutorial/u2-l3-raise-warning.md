# `_raise_warning`：统一弃用信息的生成与 stacklevel

## 1. 本讲目标

学完本讲，你应当能够：

- 逐行读懂 `numpy/core/_utils.py` 里的 `_raise_warning`，说出它如何拼装出那条长长的弃用提示文本。
- 解释 `submodule` 这个**可选参数**如何让同一个函数同时服务「包级」和「子模块级」两种调用场景。
- 说清楚 `warnings.warn` 的 `stacklevel` 参数到底在做什么，并推导出为什么 numpy 恰好用 `stacklevel=3`。
- 仿照 `_raise_warning`，亲手写出一个可复用的通用弃用函数 `deprecate(old_name, new_name, submodule=None)`，并能在「stacklevel 设错 / 设对」时正确预测警告会指向哪一行代码。

本讲只解决一个问题：**垫片在判定「这个废弃名字仍然存在」之后，是怎么把那条 `DeprecationWarning` 造出来、又怎么保证它指向用户代码而不是 numpy 自己的内部？** 主角只有一个文件——[`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L1-L21)。

## 2. 前置知识

本讲建立在你已经学过以下两讲的基础上：

- [u1-l3 运行 numpy.core：触发并捕获 DeprecationWarning](u1-l3-run-and-observe-warning.md)——那里讲过 `warnings.catch_warnings(record=True)`、`WarningMessage` 的 `.category`/`.message`/`.filename`/`.lineno`，以及「警告只提示、不阻断」。
- [u2-l1 模块级 `__getattr__`（PEP 562）与惰性转发](u2-l1-module-getattr.md)——那里讲过垫片的 `__getattr__` 在属性查找失败时被回调。

本讲只需要再补三个 Python 基础概念：

1. **`warnings.warn` 的完整签名**。
   `warnings.warn(message, category=UserWarning, stacklevel=1)`。
   - `message`：警告文本（字符串）。
   - `category`：警告类别，必须是 `Warning` 的子类；numpy 这里固定用 `DeprecationWarning`。
   - `stacklevel`：一个正整数，决定这条警告**算在谁的头上**（显示成哪个文件的哪一行）。本讲的核心就是它。

2. **Python 的「相邻字符串字面量隐式拼接」**。
   写成下面这样、中间只有空格（甚至换行）的多段字符串字面量，Python 会在**编译期**把它们拼成一个字符串，不需要 `+`：
   ```python
   s = ("a" "b" "c")   # 等价于 "abc"
   ```
   这条规则对 f-string 也成立，所以「普通字面量」和「f-string」可以交错拼接。`_raise_warning` 的长消息正是这么写的——既好读，运行时又只是一个字符串。

3. **调用栈（call stack）与栈帧（frame）**。
   函数 A 调用函数 B、B 再调用 C 时，解释器会把每次调用「压」成一帧，叠成一座栈。`warnings.warn` 想把警告归因到某一层，靠的就是「从我自己这帧往上数几层」。本讲会用一张表把这条链数清楚。

> 一句话：`_raise_warning` = 拼一段固定文本 + 选对警告类别 + 用 `stacklevel` 把账算到用户头上。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `_utils.py` | 工具（全目录唯一不做转发的 `.py`） | **本讲主角**：`_raise_warning` 的全部实现 |
| `__init__.py` | 包入口 | 以「不传 `submodule`」调用 `_raise_warning`，证明一个函数能服务包级场景 |
| `numeric.py` | 纯转发垫片 | 以 `submodule="numeric"` 调用，体现调用链的第 2 帧 |
| `multiarray.py` | 特殊垫片（含 eager 绑定） | 以 `submodule="multiarray"` 调用，对照参数差异 |
| `umath.py` | 纯转发垫片 | 以 `submodule="umath"` 调用，又一个子模块参数范例 |

铺垫一句：[u1-l2 目录结构与文件分类](u1-l2-directory-map.md) 已经确认 `_utils.py` 是全目录**唯一**「不做转发、也没有对应 `.pyi`」的 `.py`。它的全部职责，就是给所有垫片提供一个统一的「报警函数」。本讲就把它读透。

## 4. 核心概念与源码讲解

### 4.1 `_raise_warning`：把「报警」抽成一个函数

#### 4.1.1 概念说明

回忆 [u2-l2 委派模式](u2-l2-delegation-patterns.md) 里的共同骨架：每个垫片的 `__getattr__` 在判定「属性废弃但仍然存在」之后，都要做同一件事——**打印一条弃用警告，然后正常返回对象**。

这件「打印弃用警告」的事，本来可以直接写成 `warnings.warn(...)`。但 numpy 没有让 17 个垫片各自写一遍，而是把它抽成了 `_utils._raise_warning` 这一个函数。原因有三：

1. **文本必须全局一致**。弃用提示是一段固定的长文字（解释「`numpy.core` 已改名为 `numpy._core`、建议用公开 API」）。如果每个垫片各写一份，迟早会出现「这里多个句号、那里少半句」的文本漂移。抽成一个函数 = 单一信息源（single source of truth）。
2. **类别和 stacklevel 也要全局一致**。警告类别必须是 `DeprecationWarning`，`stacklevel` 必须是 `3`。集中在一处，就不会有人改错。
3. **可复用、可演进**。将来如果 numpy 想统一调整弃用措辞（比如改成 `FutureWarning`、或加上迁移指南链接），只需改 `_utils.py` 一个文件。

> 关键直觉：`_raise_warning` 不是一个「转发」函数，它是一个「**纯副作用**」函数——它不返回有用的值（返回 `None`），唯一的目的是把一条警告送进 Python 的警告系统。

#### 4.1.2 核心流程

`_raise_warning` 的签名（[L4](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4)）是：

```python
def _raise_warning(attr: str, submodule: str | None = None) -> None:
```

它做三步：

```text
1. 用两个局部变量拼出 new_module / old_module：
     new_module = "numpy._core"
     old_module = "numpy.core"
   如果传了 submodule，就分别补上 ".{submodule}"。

2. 拼一条长消息：开头用 old/new_module，结尾用 "{new_module}.{attr}"，
   中间是一大段固定解释文字。

3. warnings.warn(消息, DeprecationWarning, stacklevel=3)
   ——把警告归因到用户代码（详见 4.3）。
```

注意第 3 步：`warnings.warn` **只是把警告送进警告系统**，函数本身随后正常返回 `None`。报警 ≠ 抛异常——这一点在 [u1-l3](u1-l3-run-and-observe-warning.md) 已经强调过，这里再次确认。

#### 4.1.3 源码精读

`_utils.py` 全文只有 21 行，先整体看一遍：

[_utils.py:1-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L1-L21) —— 整个文件就是 `import warnings` + 一个 `_raise_warning` 函数。

把函数体拆成三段读：

- [L5-L6](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L5-L6)：先给出默认的「新/旧模块名」——`numpy._core` 与 `numpy.core`。这是包级场景的默认值。
- [L7-L9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L7-L9)：如果调用方传了 `submodule`，就把两个名字都补成 `numpy._core.{submodule}` / `numpy.core.{submodule}`。这是子模块级场景。`submodule` 的细节在 4.2 展开。
- [L10-L21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L10-L21)：`warnings.warn(...)` 调用，类别固定 `DeprecationWarning`（L19），`stacklevel=3`（L20）。`stacklevel` 的细节在 4.3 展开。

整个函数**没有 `return` 语句**，默认返回 `None`——这印证了它「纯副作用」的性质：调用它的垫片并不关心它的返回值，只关心「报警这一动作发生了」。

#### 4.1.4 代码实践

最快的体会方式，是**直接调用** `_raise_warning`，亲眼看到它产生一条警告。如果你本地装了 numpy（≥ 2.0），可以跑下面的脚本：

```python
import warnings
from numpy.core._utils import _raise_warning      # 直接拿到这个工具函数

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    ret = _raise_warning("asarray", "numeric")     # 仿照 numeric.py 的调用方式

print("返回值:", ret)                                # None —— 纯副作用
print("警告数:", len(w))
print("类别:", w[0].category.__name__)
print("消息开头:", w[0].message.args[0][:60], "...")
```

**需要观察的现象**：`_raise_warning` 正常返回 `None`，同时产生**恰好 1 条** `DeprecationWarning`；消息开头是 `numpy.core.numeric is deprecated and has been renamed to numpy._core.numeric.`。

**预期结果**：依次打印 `返回值: None`、`警告数: 1`、`类别: DeprecationWarning`、以及消息开头那半句。若你本地 numpy 版本不同或没装 numpy，标注「待本地验证」，并改用 4.2 里不带 numpy 的本地复制品来跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么把「报警」抽成 `_raise_warning` 一个函数，而不是让每个垫片各自写 `warnings.warn`？

> **答案**：为了得到「单一信息源」。弃用提示是固定长文本，类别（`DeprecationWarning`）和 `stacklevel`（3）也必须全局一致。集中在一个函数里，既避免 17 处副本之间的文本漂移，又让将来统一修改措辞只需改一个文件。

**练习 2**：`_raise_warning` 的返回值是 `None`，可它明明「报警」了。这两者矛盾吗？

> **答案**：不矛盾。`warnings.warn` 只是把一条警告送进 Python 的警告系统，**它本身不抛异常、正常返回**；外层函数 `_raise_warning` 也就跟着正常结束并返回 `None`。「报警」是副作用，不是返回值，更不是异常。这正是「警告 ≠ 异常」的体现（见 [u1-l3](u1-l3-run-and-observe-warning.md)）。

---

### 4.2 消息拼装：固定文本 + 可选的 submodule 参数

#### 4.2.1 概念说明

`_raise_warning` 的第二个职责，是把弃用消息**正确地拼出来**。这条消息结构很规整：

```text
{old_module} is deprecated and has been renamed to {new_module}.   ← 头部（变量）
The numpy._core namespace contains private NumPy internals and its
use is discouraged, ... use the public NumPy API. ...              ← 中段（固定文本）
If you would still like to access an internal attribute,
use {new_module}.{attr}.                                           ← 尾部（变量）
```

也就是说：**只有两个地方是动态的**——头部的「旧/新模块名」、尾部的「`新模块名.属性名`」。中间一大段是固定解释文字，劝用户改用公开 API。

`submodule` 这个可选参数，正是用来控制头尾的「模块名」长什么样的：

- **不传 `submodule`**（包级）：`old_module="numpy.core"`、`new_module="numpy._core"`。对应「用户访问 `numpy.core.某属性`」。
- **传 `submodule="numeric"`**（子模块级）：`old_module="numpy.core.numeric"`、`new_module="numpy._core.numeric"`。对应「用户访问 `numpy.core.numeric.某属性`」。

一个函数、一个参数，就同时覆盖了「包级」和「子模块级」两种调用场景——这是 `_raise_warning` 设计上最省心的地方。

#### 4.2.2 核心流程

消息拼装的伪代码与两种场景的对照：

```text
new_module = "numpy._core"
old_module = "numpy.core"
if submodule is not None:
    new_module = f"{new_module}.{submodule}"     # → "numpy._core.numeric"
    old_module = f"{old_module}.{submodule}"     # → "numpy.core.numeric"
msg = f"{old_module} is deprecated ... use {new_module}.{attr}."
warnings.warn(msg, DeprecationWarning, stacklevel=3)
```

| 调用方式 | `old_module` | `new_module` | 消息头部 |
| --- | --- | --- | --- |
| `_raise_warning("asarray")`（包级，不传 submodule） | `numpy.core` | `numpy._core` | `numpy.core is deprecated and has been renamed to numpy._core.` |
| `_raise_warning("asarray", "numeric")`（子模块级） | `numpy.core.numeric` | `numpy._core.numeric` | `numpy.core.numeric is deprecated and has been renamed to numpy._core.numeric.` |

注意尾部的 `{attr}` 也会随之落到正确位置：包级时是 `numpy._core.asarray`，子模块级时是 `numpy._core.numeric.asarray`。

#### 4.2.3 源码精读

模块名的拼装只在三行里完成：

[_utils.py:4-9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L9) —— `submodule is not None` 的判断决定了模块名是否要补上 `.子模块`。

消息文本本身写在 `warnings.warn(...)` 的第一个参数里：

[_utils.py:11-18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L11-L18) —— 这是一段用「相邻字面量隐式拼接」写成的长字符串：第 11、18 行是 f-string（含变量），中间 L12–L17 全是普通字符串字面量（固定文本）。Python 在编译期把它们拼成一个字符串，运行时只是一个 `str`。

那么「谁传 submodule、谁不传」？全目录只有一处不传——包入口：

[__init__.py:30-33](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) —— 包级 `__getattr__` 直接 `_raise_warning(attr_name)`，**不传 submodule**，于是消息里是 `numpy.core`（不带子模块名）。

其余子模块垫片都传了各自的模块名：

- [numeric.py:11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L11) —— `_raise_warning(attr_name, "numeric")`。
- [multiarray.py:21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L21) —— `_raise_warning(attr_name, "multiarray")`。
- [umath.py:9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L9) —— `_raise_warning(attr_name, "umath")`。

对照之下就能看出：**`submodule` 参数的取值，恰好就是「这个垫片文件名去掉 `.py`」**。这是一个人为约定——调用方负责传入正确的子模块名，`_raise_warning` 负责把它拼进消息。

#### 4.2.4 代码实践

在 REPL 里用一段**不依赖 numpy** 的本地复制品，直观对比「传 / 不传 submodule」的消息差异（这段是示例代码，仿照 `_raise_warning`）：

```python
# 示例代码：_raise_warning 的简化复制品
import warnings

def raise_warning(attr, submodule=None):
    new_module = "numpy._core"
    old_module = "numpy.core"
    if submodule is not None:
        new_module = f"{new_module}.{submodule}"
        old_module = f"{old_module}.{submodule}"
    warnings.warn(
        f"{old_module} is deprecated and has been renamed to {new_module}. "
        f"use {new_module}.{attr}.",
        DeprecationWarning,
        stacklevel=2,   # 这里先用 2，4.3 再细讲；不影响观察消息文本
    )

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    raise_warning("asarray")                # 包级
with warnings.catch_warnings(record=True) as w2:
    warnings.simplefilter("always")
    raise_warning("asarray", "numeric")     # 子模块级

print(w[0].message.args[0])
print("---")
print(w2[0].message.args[0])
```

**需要观察的现象**：两次调用的消息**只在模块名**上有差别——包级是 `numpy.core ... numpy._core.asarray`，子模块级是 `numpy.core.numeric ... numpy._core.numeric.asarray`。

**预期结果**：第一段以 `numpy.core is deprecated and has been renamed to numpy._core.` 开头、以 `use numpy._core.asarray.` 结尾；第二段相应位置变成 `numpy.core.numeric` 与 `numpy._core.numeric.asarray`。

#### 4.2.5 小练习与答案

**练习 1**：如果调用 `_raise_warning("asarray")` 时**不传** `submodule`，消息里的 `old_module` 会是什么？

> **答案**：`numpy.core`（包级）。因为 `submodule is None`，不会执行 L8–L9 的补名逻辑，`old_module` 保持 L6 的初值 `"numpy.core"`。这正是包入口 `__init__.py` 的用法。

**练习 2**：消息由多段字符串字面量拼成，其中只有两段是 f-string，中间那些固定文本为什么不也写成 f-string？

> **答案**：因为它们不含任何变量，写成普通字面量更清晰，也避免无谓的 f-string 格式化开销。Python 的「相邻字面量隐式拼接」让多段字符串（含 f-string 与普通字面量）可以自然地跨行写成一个字符串，既好读、运行时又只是一个 `str`。

**练习 3**：能不能把 `submodule` 的默认值从 `None` 改成空字符串 `""`，然后用 `f"{old_module}.{submodule}"` 直接拼，省掉 `if submodule is not None` 这个分支？

> **答案**：能跑，但不好。包级时会拼出 `numpy.core.`（末尾多一个点）这样丑陋且语义不清的结果。用 `None` 显式区分「包级 vs 子模块级」，再在 `is not None` 时才补名，是更安全、可读性更好的写法。numpy 选的就是这种写法。

---

### 4.3 `warnings.warn` 的 stacklevel：警告归因到真正的调用者

#### 4.3.1 概念说明

`stacklevel` 是本讲最容易踩坑、也最值得讲透的点。一句话：**它决定这条警告在报出来时，显示成「哪个文件的哪一行」**。

为什么要关心这个？因为用户看到一条 `DeprecationWarning` 时，第一反应是「我的哪行代码触发了它？」如果警告显示成 `_utils.py:20`（numpy 内部），用户完全无法定位自己的代码，这条警告就几乎没用。numpy 想要的是：**警告显示成用户自己写 `numpy.core.numeric.asarray` 的那一行**。

`stacklevel` 就是一个「从 `warnings.warn` 这一帧往上数几层」的计数器：

- `stacklevel=1`（默认）：算在 `warnings.warn` **被调用的那一行**上——也就是 `_raise_warning` 内部，对用户毫无意义。
- `stacklevel=2`：往上数一层，算在「调用 `_raise_warning` 的那一行」上——也就是垫片的 `__getattr__`，仍是 numpy 内部。
- `stacklevel=3`：再往上数一层，算在「调用 `__getattr__` 的那一行」上——**正是用户代码**。

用一行公式表达这个计数关系（从 `warnings.warn` 所在帧起算，往上数到目标帧）：

\[
\text{stacklevel} = (\text{目标帧在 warn 帧之上的层数}) + 1
\]

用户代码在 `warn` 帧之上恰好 2 层，于是 \(2 + 1 = 3\)。

#### 4.3.2 核心流程

把「用户访问 `numpy.core.numeric.asarray`」时的整条调用栈画出来（从下往上读，每一层就是一个 Python 栈帧）：

```text
栈顶（最先返回）
        ↑
        │  warnings.warn(..., stacklevel=3)   ← warn 所在帧
        │     _raise_warning("asarray","numeric")        ← stacklevel=1 指向这里（_utils.py）
        │       numeric.__getattr__("asarray")           ← stacklevel=2 指向这里（numeric.py，numpy 内部）
        │         用户代码: numpy.core.numeric.asarray   ← stacklevel=3 指向这里（用户代码 ✅）
栈底（最初调用）
```

对照成表：

| `stacklevel` | 指向的帧 | 警告显示的文件 | 对用户是否有用 |
| --- | --- | --- | --- |
| 1（默认） | `warnings.warn` 所在帧 | `_utils.py` | ❌ numpy 内部 |
| 2 | 调用 `_raise_warning` 的帧 | `numeric.py`（垫片） | ❌ 仍是 numpy 内部 |
| **3** | 调用 `__getattr__` 的帧 | **用户代码** | ✅ 正是我们要的 |

**一个精妙之处**：无论是「包级访问」（经 `__init__.__getattr__` 转发）还是「子模块级访问」（经 `numeric.__getattr__` 转发），调用链都是同一个形状——

```text
用户代码  →  某个垫片的 __getattr__  →  _raise_warning  →  warnings.warn
 (层 3)         (层 2)                   (层 1 = warn 帧)
```

两条路径在「用户代码」与「`warnings.warn`」之间**都恰好隔着 2 个 Python 帧**，所以一个 `stacklevel=3` 就能同时服务所有垫片，不需要每个垫片各传一个不同的值。

#### 4.3.3 源码精读

`stacklevel=3` 写在 `warnings.warn` 调用的最后一个参数：

[_utils.py:19-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L19-L21) —— `DeprecationWarning`（类别，L19）与 `stacklevel=3`（L20）。

要验证「3」这个数字是对的，就把两条真实调用链各数一遍帧：

**链 A：子模块级**——用户写 `numpy.core.numeric.asarray`：

1. 用户代码（属性访问触发 `__getattr__`）→
2. [numeric.py:11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L11) 的 `_raise_warning(attr_name, "numeric")` →
3. [_utils.py:10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L10) 的 `warnings.warn(..., stacklevel=3)`。

`stacklevel=3` 从第 3 帧往上数 3 层 = 用户代码。✅

**链 B：包级**——用户写 `numpy.core.some_attr`：

1. 用户代码（属性访问触发包级 `__getattr__`）→
2. [__init__.py:32](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L32) 的 `_raise_warning(attr_name)` →
3. [_utils.py:10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L10) 的 `warnings.warn(..., stacklevel=3)`。

同样是 3 帧，`stacklevel=3` 同样指向用户代码。✅

这就是为什么 numpy 敢于把 `stacklevel=3` **硬编码**进 `_raise_warning`：只要所有调用方都遵守「垫片 `__getattr__` 直接调 `_raise_warning`、中间不额外包函数」这个约定，帧数就恒为 3。

#### 4.3.4 代码实践

用一段**不依赖 numpy** 的最小实验，亲眼看到 `stacklevel` 如何改变警告的归因位置（示例代码）：

```python
# 示例代码：观察 stacklevel 对警告归因的影响
import warnings

def raise_warning(attr, stacklevel):
    # 仿 _raise_warning，stacklevel 由参数传入以便对比
    warnings.warn(f"deprecated: use numpy._core.{attr}", DeprecationWarning,
                  stacklevel=stacklevel)

def shim_getattr(attr, stacklevel):     # 仿 numeric.__getattr__，是「层 2」
    raise_warning(attr, stacklevel)

def caller(attr, stacklevel):           # 仿用户代码，是「层 3」
    shim_getattr(attr, stacklevel)

for lvl in (1, 2, 3):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        caller("asarray", lvl)
    print(f"stacklevel={lvl} -> 归因到 {w[0].filename} : {w[0].lineno}")
```

**需要观察的现象**：`stacklevel=1` 归因到 `raise_warning` 所在文件、`=2` 归因到 `shim_getattr` 所在文件、`=3` 归因到 `caller` 所在文件——也就是「越大的 stacklevel，归因位置越靠近最初的调用者」。

**预期结果**：三行输出的文件名会**依次**从「定义 `raise_warning` 的文件」变到「定义 `caller` 的文件」。具体行号取决于你把脚本写在哪一行，但**文件名**的变化规律稳定可观察。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `_utils.py` 里的 `stacklevel=3` 改成 `2`，用户看到的警告会指向哪里？

> **答案**：会指向垫片的 `__getattr__`（比如 `numeric.py` 或 `__init__.py`），仍是 numpy 内部代码。用户无法据此定位自己写 `numpy.core.numeric.asarray` 的那一行，警告的实用性大打折扣。这就是为什么必须用 `3`。

**练习 2**：为什么包级（`__init__.__getattr__`）和子模块级（`numeric.__getattr__`）两条不同路径，能用**同一个** `stacklevel=3`？

> **答案**：因为两条路径的调用链形状完全一样——都是「用户代码 → 某个 `__getattr__` → `_raise_warning` → `warnings.warn`」，中间隔着恰好 2 个 Python 帧。帧数相同，所需 `stacklevel` 就相同（都是 3）。这正是把 `stacklevel` 硬编码进 `_raise_warning` 得以成立的前提。

**练习 3**：如果有人在 `_raise_warning` 外面再包一层，比如 `def my_warn(attr): _raise_warning(attr, "numeric")`，并在垫片里改成调用 `my_warn`，原来的 `stacklevel=3` 还对吗？

> **答案**：不对。多了一层 `my_warn` 这个 Python 帧，用户代码现在在「`warn` 帧之上 3 层」，需要 `stacklevel=4`。这揭示了 `stacklevel` 的**脆弱性**：调用链层数一变，`stacklevel` 就得同步调整。所以 numpy 严格遵守「垫片 `__getattr__` 直接调 `_raise_warning`」这个约定，不轻易加中间层。

---

## 5. 综合实践

把本讲三块知识（统一弃用函数、`submodule` 参数、`stacklevel`）串起来，亲手写一个可复用的 `deprecate(old_name, new_name, submodule=None)`，并用它搭一个最小转发垫片。重点环节是：**先把 `stacklevel` 故意设错，观察警告指向了转发模块；再把它调对，对比归因位置。**

**实践目标**：仿照 [`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L1-L21) 实现一个通用弃用函数，并在转发模块里调用它，验证「报警 + 正常返回」与「警告归因到调用方」两件事。

**操作步骤**：

1. 在一个空目录里建一个最小包 `shim_demo/`：

   ```
   shim_demo/
     __init__.py        # 空文件，让它成为包
     _new.py            # 「真模块」（仿 numpy._core.*）
     _deprecate.py      # 通用弃用函数（仿 _utils._raise_warning）
     core.py            # 转发垫片（仿 numeric.py）
     run.py             # 调用方 + 验证脚本
   ```

2. `_new.py`（真模块，放几个属性）：

   ```python
   # 这是「真模块」的替身
   def asarray(*a, **k):
       return "real asarray"
   PI = 3.14
   ```

3. `_deprecate.py`（仿 [_utils.py:4-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21)，`stacklevel` 暴露成参数以便对比）：

   ```python
   import warnings

   def deprecate(old_name, new_name, submodule=None, stacklevel=3):
       old_module = old_name
       new_module = new_name
       if submodule is not None:
           old_module = f"{old_name}.{submodule}"
           new_module = f"{new_name}.{submodule}"
       warnings.warn(
           f"{old_module} is deprecated and has been renamed to {new_module}. "
           f"use {new_module}.<attr>.",
           DeprecationWarning,
           stacklevel=stacklevel,
       )
   ```

4. `core.py`（转发垫片，仿 [numeric.py:1-13](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L13)）。注意：调用链是「用户 → `core.__getattr__` → `deprecate` → `warn`」，共 3 帧，所以正确的 `stacklevel` 是 **3**：

   ```python
   def __getattr__(attr_name):
       from ._new import _new                       # 惰性导入真模块
       from ._deprecate import deprecate
       sentinel = object()
       ret = getattr(_new, attr_name, sentinel)     # 仿 numeric 的 sentinel 写法
       if ret is sentinel:
           raise AttributeError(
               f"module 'shim_demo.core' has no attribute {attr_name}")
       deprecate("myold.core", "myold._core", submodule="core",
                 stacklevel=3)                       # 先写正确的 3，第 6 步再改成 2 对比
       return ret
   ```

5. `run.py`（调用方 + 验证）：

   ```python
   import warnings
   from . import core

   def probe(stacklevel):
       # 临时把 core 用的 stacklevel 改掉，便于对比（仅作演示）
       with warnings.catch_warnings(record=True) as w:
           warnings.simplefilter("always")
           obj = core.asarray        # 这一行就是「用户代码」——我们希望警告指向这里
       print(f"stacklevel={stacklevel}: 返回 {obj!r}, "
               f"归因到 {w[0].filename}（末尾是 run.py 吗? {w[0].filename.endswith('run.py')}）")

   probe(3)
   ```

6. **第一轮：stacklevel 设错**。把 `core.py` 里 `deprecate(...)` 的 `stacklevel` 改成 `2`，运行 `python -m shim_demo.run`。
   **预期**：归因文件的末尾**不是** `run.py`，而是 `core.py`（转发模块）——警告指向了垫片，而非调用方。

7. **第二轮：stacklevel 调对**。改回 `stacklevel=3`，再运行。
   **预期**：归因文件末尾**是** `run.py`，并且警告指向 `obj = core.asarray` 那一行。

**需要观察的现象**：
- 无论 `stacklevel` 是 2 还是 3，`core.asarray` 都**正常返回** `"real asarray"`——报警不影响功能（印证「警告 ≠ 异常」）。
- 唯一变化的是 `w[0].filename`：`stacklevel=2` 指向 `core.py`（转发模块），`stacklevel=3` 指向 `run.py`（调用方）。

**预期结果**：
- `stacklevel=2`：`归因到 .../core.py（末尾是 run.py 吗? False）`。
- `stacklevel=3`：`归因到 .../run.py（末尾是 run.py 吗? True）`，且 `w[0].lineno` 对应 `obj = core.asarray` 那一行。

**解释（写进你的实验记录）**：`stacklevel` 从 `warnings.warn` 所在帧起算。`deprecate` 是 warn 帧（层 1），`core.__getattr__` 是层 2，`run.py` 的 `probe` 是层 3。所以 `stacklevel=2` 归因到 `core.py`（转发模块），`stacklevel=3` 才归因到 `run.py`（调用方）。这正是 numpy 在 [_utils.py:20](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L20) 选 `stacklevel=3` 的原因——它的调用链同样是「用户 → 垫片 `__getattr__` → `_raise_warning` → `warn`」三层。若本地环境跑不出上述现象，标注「待本地验证」并手算各 case 的栈帧数。

## 6. 本讲小结

- `_raise_warning` 是全目录唯一「不做转发」的 `.py`——它是一个**纯副作用**函数：拼一条弃用消息、以 `DeprecationWarning` 类别送进警告系统，然后返回 `None`。把它抽成单一函数，是为了让 17 个垫片的弃用文本、类别、`stacklevel` 全局一致（单一信息源）。
- 消息只有两处是动态的：头部的「旧/新模块名」、尾部的「`新模块名.属性名`」；中间是一大段固定解释文字，用「相邻字面量隐式拼接」跨行写成。
- 可选参数 `submodule` 让一个函数同时服务两种场景：**不传** → 包级（`numpy.core`，见 `__init__.py`）；**传 `"numeric"`/`"umath"`/...** → 子模块级（`numpy.core.numeric` 等）。其取值约定就是「垫片文件名去掉 `.py`」。
- `warnings.warn` 的 `stacklevel` 决定警告归因到哪一帧：`1` = warn 所在帧（`_utils.py`）、`2` = 垫片 `__getattr__`、`3` = **用户代码**。numpy 选 `3`，是为了让用户能看到是自己的哪一行触发了弃用。
- 之所以一个硬编码的 `3` 能通吃所有垫片，是因为包级与子模块级两条调用链形状相同——「用户 → `__getattr__` → `_raise_warning` → `warn`」恒为 3 帧。这也意味着 `stacklevel` 是**脆弱**的：调用链一旦多包一层，就必须同步加 1。

## 7. 下一步学习建议

- 接着读 [u2-l4 包入口 `__init__.py`：`__all__`、`__getattr__` 与 `_ufunc_reconstruct`](u2-l4-package-init.md)，看包级 `__all__` 如何强制惰性加载、包级 `__getattr__` 如何与本讲的 `_raise_warning`（不传 submodule）配合，以及 `_ufunc_reconstruct` 为旧 pickle 保留的原因。
- 第二单元结束后进入第三单元的兼容性工程：[u3-l1 Pickle 向后兼容](u3-l1-pickle-compat.md) 会解释「为什么有些属性等不起惰性加载和警告、必须在顶部 eager 绑定」——那是与本讲「惰性 + 报警」正好相反的另一面。
- 延伸阅读：在 CPython 文档里对照 [`warnings.warn`](https://docs.python.org/3/library/warnings.html#the-warnings-module) 的 `stacklevel` 说明，用本讲的 3 帧调用链手算一遍，巩固「`stacklevel` = 目标帧到 warn 帧的层数 + 1」这个结论。
