# 模块导出与公共 API 体系

## 1. 本讲目标

通过上一篇（u1-l2）你已经知道 `scipy/fftpack/` 目录里有哪些文件、谁是核心实现、谁是弃用垫片。本讲聚焦其中最薄、却最关键的一个文件——包入口 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py)。它本身几乎不含任何变换算法，却决定了「外界能用哪些名字、这些名字从哪来」。

学完本讲，你应当能够：

1. 说清楚 Python 的 `__all__` 是什么、它如何控制 `from module import *` 与「公共 API」。
2. 解释 `from ._basic import *` 这种**聚合导入**是如何把四个私有子模块的名字拼装成 `scipy.fftpack` 的统一门面。
3. 把 fftpack 的公共 API 准确地归入**五大功能分组**（FFT、实变换、伪微分、辅助函数、卷积），并明白为什么「卷积」一组与另外四组地位不同。
4. 用 `__module__` 属性亲手验证 `fftshift`、`fftfreq` 等其实来自 `numpy`，理解什么叫**再导出（re-export）**。

## 2. 前置知识

本讲不涉及任何傅里叶数学，只讲 Python 的「模块导出机制」。需要你先具备以下几个朴素概念：

- **包（package）与子模块（submodule）**：`scipy.fftpack` 是一个包，对应磁盘上的 `scipy/fftpack/` 目录；目录里的 `__init__.py` 是包的入口；目录里其他 `.py` 文件（如 `_basic.py`）是它的子模块，写作 `scipy.fftpack._basic`。
- **导入即赋值**：执行 `from numpy.fft import fftshift` 后，当前模块的命名空间里就多了一个名字 `fftshift`，它指向 `numpy.fft` 里那个函数对象本身——不是副本，是同一个对象。
- **`dir(obj)`**：返回对象（含模块）身上所有可见属性名的列表。
- **`from module import *`**：星号导入。它到底导入哪些名字，由模块的 `__all__` 决定；没有 `__all__` 时才退化为「所有不以 `_` 开头的名字」。
- **下划线约定**：以 `_` 开头的名字（如 `_basic`、`_helper`）是「私有」的，按惯例不对外暴露。fftpack 正是用这套约定把实现藏在 `_xxx.py` 里。

> 承接 u1-l1 的结论：`fft`、`dct` 等公共函数**不**定义在 `__init__.py` 里，而是定义在 `_basic.py` 等私有子模块，再被「搬运」到包顶层。本讲要讲清楚的就是这套「搬运」机制。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它做什么 |
|------|------|----------------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py) | 包入口，定义公共 API 门面 | 读 `__all__` 与四条聚合导入，理解导出全貌 |
| [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py) | 私有子模块：复/实数 FFT | 看它的 `__all__` 如何成为聚合导入的「清单」 |
| [`_realtransforms.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_realtransforms.py) | 私有子模块：DCT/DST | 同上 |
| [`_pseudo_diffs.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_pseudo_diffs.py) | 私有子模块：伪微分算子 | 同上，并顺带看它如何 `from . import convolve` |
| [`_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py) | 私有子模块：辅助函数 | 关键证据：`fftshift` 等来自 `numpy.fft` |
| [`basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/basic.py) | 弃用垫片（shim） | 对照理解「公共 API 不经过这里」 |

## 4. 核心概念与源码讲解

### 4.1 `__all__` 列表：模块导出的「白名单」

#### 4.1.1 概念说明

Python 模块可以定义一个名为 `__all__` 的**字符串列表**，用来显式声明「本模块对外公开的名字」。它有两层作用：

1. **控制 `from module import *`**：星号导入只会引入 `__all__` 里列出的名字，其余一律不引入。没有 `__all__` 时，才退化为「导入所有不以 `_` 开头的名字」。
2. **作为文档/契约**：它是「官方公共 API」的权威清单。IDE 自动补全、文档生成工具（如 Sphinx 的 `autosummary`）、以及人类读者，都把 `__all__` 当作「这个模块到底提供什么」的标准答案。

可以把 `__all__` 想象成一家餐厅的**菜单**：后厨（实现）可以有很多东西，但只有印在菜单上的才会端给顾客。不在菜单上的，即便存在，也不保证稳定、也不算公开承诺。

#### 4.1.2 核心流程

当用户写下 `from scipy.fftpack import *` 时，Python 解释器的处理过程是：

```text
1. 导入 scipy.fftpack（执行 __init__.py）
2. 读取 __init__.py 里的 __all__ 列表
3. 对列表中的每一个名字 name：
       在 scipy.fftpack 的命名空间里查找 getattr(scipy.fftpack, name)
       若找不到 → 直接抛 AttributeError
       若找到   → 把它绑定到调用者的命名空间
```

这里有一个重要推论：**`__all__` 里写的名字，必须在模块命名空间里真实存在**。`__all__` 只是「点名」，点到的名字必须有人应答（即确实被导入/定义过），否则星号导入会报错。这正是为什么 `__init__.py` 在写完 `__all__` 之后，紧接着就要用聚合导入把所有名字「搬」进命名空间。

#### 4.1.3 源码精读

fftpack 包的 `__all__` 定义在入口文件的中段，共 31 个名字：

```python
__all__ = ['fft','ifft','fftn','ifftn','rfft','irfft',
           'fft2','ifft2',
           'diff',
           'tilbert','itilbert','hilbert','ihilbert',
           'sc_diff','cs_diff','cc_diff','ss_diff',
           'shift',
           'fftfreq', 'rfftfreq',
           'fftshift', 'ifftshift',
           'next_fast_len',
           'dct', 'idct', 'dst', 'idst', 'dctn', 'idctn', 'dstn', 'idstn'
           ]
```

这 31 个名字就是 fftpack 对世界承诺的**全部公共 API**。来源：[`__init__.py:L81-L91`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L81-L91)。

观察两点：

- 列表里**没有**任何 `_` 开头的名字（如 `_basic`、`_helper`）——私有实现被刻意排除在菜单之外。
- 列表里也**没有** `basic`、`helper`、`convolve` 这类子模块名——稍后会看到，`convolve` 是以「子模块」身份而非「顶层函数」身份提供的，所以不在 `__all__`。

#### 4.1.4 代码实践

**目标**：亲手验证 `__all__` 就是公共 API 的白名单，并理解它与 `dir()` 的差别。

**操作步骤**：

1. 打开一个 Python 终端，导入 fftpack；
2. 打印 `fftpack.__all__` 并计数；
3. 用 `dir(fftpack)` 对比，看看「菜单」和「后厨」的差别。

```python
import scipy.fftpack as fp

# 菜单：官方公共 API
print("公共名称数:", len(fp.__all__))
print(fp.__all__)

# 后厨：dir() 会把子模块、__all__ 以外的东西也列出来
all_attrs = [n for n in dir(fp) if not n.startswith('_')]
print("dir() 中不以 _ 开头的名字数:", len(all_attrs))

# 找出「后厨有、菜单没有」的名字
extra = set(all_attrs) - set(fp.__all__)
print("菜单外的可见名字:", sorted(extra))
```

**需要观察的现象**：
- `__all__` 长度应为 31。
- `dir()` 多出来的名字里，应能看到 `basic`、`helper`、`pseudo_diffs`、`realtransforms`（弃用垫片子模块，见 u1-l1）、`convolve`（卷积子模块）、`test`（PytestTester 实例）等。这些都不在 `__all__` 里。

**预期结果**：`__all__` 恰好 31 项；`extra` 集合非空，印证「`dir()` 看到的 ≠ 公共 API」。具体的 `extra` 内容**待本地验证**（取决于 SciPy 版本，但上述几类名字一定出现）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fftpack.__all__` 末尾故意加一个根本不存在的名字 `'not_a_real_thing'`，`from scipy.fftpack import *` 还能成功吗？为什么？

> **答案**：不能成功，会抛 `AttributeError`。因为 `import *` 会逐个对 `__all__` 里的名字执行属性查找，点到的名字必须真实存在于模块命名空间。

**练习 2**：`__all__` 和 `dir(module)` 哪个更能代表「作者想让你用的 API」？

> **答案**：`__all__`。`dir()` 只反映「当前命名空间里有什么」，包括私有子模块、测试入口、甚至意外泄漏的名字；`__all__` 则是作者显式声明、并承诺维护的公共契约。

---

### 4.2 聚合导入：四个私有子模块拼装出公共 API

#### 4.2.1 概念说明

4.1 节留下一个问题：`__all__` 点了 31 个名字的名，可这些名字在 `__init__.py` 里**一个都没定义**。它们从哪来？答案就是紧随其后的四行**聚合导入**（aggregation import）：

```python
from ._basic import *
from ._pseudo_diffs import *
from ._helper import *
from ._realtransforms import *
```

这是一种典型的**门面模式（Facade）**实现：把内部按主题拆成的多个私有子模块（`_basic`、`_pseudo_diffs`、`_helper`、`_realtransforms`），统一「搬运」到包顶层，对外只暴露一个干净的 `scipy.fftpack` 入口。用户无需知道 `fft` 住在 `_basic.py`、`dct` 住在 `_realtransforms.py`，只要 `from scipy.fftpack import fft, dct` 即可。

#### 4.2.2 核心流程

`from ._basic import *` 这一行到底做了什么？关键是「`*`」的语义由**被导入模块自己的 `__all__`** 决定：

```text
from ._basic import *
   ├─ 1. 先导入子模块 _basic（执行 _basic.py）
   ├─ 2. 读取 _basic.__all__
   └─ 3. 把 _basic.__all__ 里列出的每个名字，
          从 _basic 的命名空间绑定到 __init__ 的命名空间
```

所以，每个私有子模块都**自带一份自己的 `__all__`**，这份清单决定了「它愿意向包顶层贡献哪些名字」。四条聚合导入各取所需，拼成完整的 31 项公共 API。

#### 4.2.3 源码精读

先看 `__init__.py` 的四条聚合导入：[`__init__.py:L93-L96`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L93-L96)。

再看四个私有子模块各自的 `__all__`（即它们各自贡献的「清单」）：

- **`_basic.py`** 贡献 8 个 FFT 函数：[`_basic.py:L5-L6`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L5-L6)
  ```python
  __all__ = ['fft','ifft','fftn','ifftn','rfft','irfft',
             'fft2','ifft2']
  ```
- **`_pseudo_diffs.py`** 贡献 10 个伪微分算子：[`_pseudo_diffs.py:L6-L9`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_pseudo_diffs.py#L6-L9)
  ```python
  __all__ = ['diff',
             'tilbert','itilbert','hilbert','ihilbert',
             'cs_diff','cc_diff','sc_diff','ss_diff',
             'shift']
  ```
- **`_helper.py`** 贡献 5 个辅助函数：[`_helper.py:L8`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L8)
  ```python
  __all__ = ['fftshift', 'ifftshift', 'fftfreq', 'rfftfreq', 'next_fast_len']
  ```
- **`_realtransforms.py`** 贡献 8 个 DCT/DST 函数：[`_realtransforms.py:L5`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_realtransforms.py#L5)
  ```python
  __all__ = ['dct', 'idct', 'dst', 'idst', 'dctn', 'idctn', 'dstn', 'idstn']
  ```

做个加法：\(8 + 10 + 5 + 8 = 31\)，正好等于包级 `__all__` 的 31 项。这不是巧合，而是「包级 `__all__`」与「各子模块 `__all__` 之和」必须严格同步的体现——它们描述的是同一份公共 API。

> **小贴士**：注意四条聚合导入的顺序（`_basic` → `_pseudo_diffs` → `_helper` → `_realtransforms`）并不会改变最终结果，因为四个子模块的 `__all__` 互不重叠。如果将来有两个子模块重名，后导入的会覆盖前者，所以「清单互斥」是这套机制隐含的纪律。

#### 4.2.4 代码实践

**目标**：验证 4 个子模块的 `__all__` 之和，确实等于包级 `__all__`。

**操作步骤**：

```python
import scipy.fftpack as fp
import scipy.fftpack._basic as _b
import scipy.fftpack._pseudo_diffs as _p
import scipy.fftpack._helper as _h
import scipy.fftpack._realtransforms as _r

union = set(_b.__all__) | set(_p.__all__) | set(_h.__all__) | set(_r.__all__)
print("四个子模块 __all__ 的并集大小:", len(union))
print("包级 __all__ 大小:", len(fp.__all__))
print("两者完全一致:", union == set(fp.__all__))
```

**需要观察的现象**：并集大小应为 31，且与包级 `__all__` 集合完全相等。

**预期结果**：`两者完全一致: True`。（若想更严谨，可额外验证两两子模块 `__all__` 交集为空。）

#### 4.2.5 小练习与答案

**练习 1**：为什么聚合导入写的是 `from ._basic import *`，而不是逐个 `from ._basic import fft, ifft, ...`？

> **答案**：用 `*` 配合子模块自己的 `__all__`，可以让「公共 API 清单」只维护在**一处**（即 `_basic.__all__`）；新增函数时只需改子模块的 `__all__` 并同步包级 `__all__`，无需在导入语句里逐个增删，减少维护成本和不一致风险。

**练习 2**：如果 `_basic.py` 删掉了自己的 `__all__`，`from ._basic import *` 还会导入 `fft` 吗？

> **答案**：仍会导入 `fft`，但行为会变「脏」。没有 `__all__` 时，`import *` 退化为导入所有不以 `_` 开头的名字，于是 `_basic.py` 里 `from scipy.fft import _duccfft` 带进来的 `np`、`_duccfft` 之外的公共名字都可能被误带入包顶层。这就是为什么每个子模块都要显式声明 `__all__`——给星号导入设一道护栏。

---

### 4.3 五大功能分组的归属映射

#### 4.3.1 概念说明

fftpack 的公共 API 在文档（`__init__.py` 顶部的 docstring）里被组织成**五大功能分组**：

1. **快速傅里叶变换（FFT）**：`fft`、`ifft`、`fft2`、`ifft2`、`fftn`、`ifftn`、`rfft`、`irfft`
2. **实变换（DCT/DST）**：`dct`、`idct`、`dctn`、`idctn`、`dst`、`idst`、`dstn`、`idstn`
3. **伪微分算子**：`diff`、`tilbert`、`itilbert`、`hilbert`、`ihilbert`、`cs_diff`、`sc_diff`、`ss_diff`、`cc_diff`、`shift`
4. **辅助函数**：`fftshift`、`ifftshift`、`fftfreq`、`rfftfreq`、`next_fast_len`
5. **卷积**：`convolve`、`convolve_z`、`init_convolution_kernel`、`destroy_convolve_cache`

前四组都是包顶层的「扁平名字」，出现在 `__all__` 里；而**第五组（卷积）地位特殊**——它们住在 `scipy.fftpack.convolve` 这个**子模块**里，必须通过 `scipy.fftpack.convolve.convolve` 这样带模块前缀的方式访问，且**不在** `__all__` 中。这正是为什么 `dir(scipy.fftpack)` 能看到 `convolve`（一个模块对象），却看不到 `convolve_z`（它要进到子模块里才看得到）。

#### 4.3.2 核心流程

下面这张映射表把「分组 ←→ 子模块 ← → 是否在 `__all__`」三者的关系一次说清：

```text
┌──────────────┬─────────────────────────┬────────────┬───────────┐
│   功能分组    │      所属子模块          │ 聚合导入?   │ 在 __all__?│
├──────────────┼─────────────────────────┼────────────┼───────────┤
│ FFT          │ _basic.py               │  是 (import*)│ 是        │
│ 实变换 DCT/DST│ _realtransforms.py      │  是 (import*)│ 是        │
│ 伪微分算子    │ _pseudo_diffs.py        │  是 (import*)│ 是        │
│ 辅助函数      │ _helper.py              │  是 (import*)│ 是        │
│ 卷积         │ convolve.pyx (子模块)    │  否          │ 否        │
└──────────────┴─────────────────────────┴────────────┴───────────┘
```

卷积分组之所以走「子模块」路线，原因之一是它的底层 [`convolve.pyx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/convolve.pyx) 是一个由 Cython 编译而来的二进制扩展模块（详见 u1-l2），自带独立的缓存与内核初始化接口（如 `init_convolution_kernel` / `destroy_convolve_cache`），把它们收敛在 `scipy.fftpack.convolve` 这个命名空间下更内聚。文档里也用了一条 `.. module:: scipy.fftpack.convolve` 指令来明确这一点。

#### 4.3.3 源码精读

文档 docstring 用 `autosummary` 把这五组分别列出。FFT 组与实变换组：[`__init__.py:L13-L31`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L13-L31)；伪微分算子组：[`__init__.py:L36-L48`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L36-L48)；辅助函数组：[`__init__.py:L53-L60`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L53-L60)。

卷积组则单独成节，并用 `.. module::` 指令声明它属于子模块命名空间：[`__init__.py:L65-L77`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L65-L77)：

```python
Convolutions (:mod:`scipy.fftpack.convolve`)
============================================

.. module:: scipy.fftpack.convolve

.. autosummary::
   :toctree: generated/

   convolve
   convolve_z
   init_convolution_kernel
   destroy_convolve_cache
```

一个旁证：`_pseudo_diffs.py` 在实现伪微分算子时，确实通过 `from . import convolve` 把卷积子模块作为依赖引入——说明卷积是被当作「子模块」被其他代码消费的：[`_pseudo_diffs.py:L14`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_pseudo_diffs.py#L14)。

#### 4.3.4 代码实践

**目标**：写一个脚本，把 fftpack 的公共 API 自动归入五大分组，并凸显卷积组的「子模块」特殊性。

**操作步骤**：

```python
import scipy.fftpack as fp

# 前四组：扁平名字，按子模块 __all__ 归类
groups = {
    "FFT":           fp._basic.__all__,
    "实变换 DCT/DST":  fp._realtransforms.__all__,
    "伪微分算子":      fp._pseudo_diffs.__all__,
    "辅助函数":        fp._helper.__all__,
}
for name, members in groups.items():
    print(f"[{name}] ({len(members)}): {members}")

# 第五组：卷积——它在子模块里，不在包 __all__
convolve_public = [n for n in dir(fp.convolve) if not n.startswith('_')]
print(f"[卷积] 子模块 scipy.fftpack.convolve: {convolve_public}")

# 验证：卷积函数不在包级 __all__
print("convolve 在包 __all__ 里吗:", 'convolve' in fp.__all__)
```

**需要观察的现象**：前四组的名字都应在 `fp.__all__` 中；卷积组的四个名字在 `fp.convolve` 命名空间下可见，但 `'convolve' in fp.__all__` 为 `False`。

**预期结果**：四组扁平名字合计 31；`convolve` 不在 `__all__`。卷积子模块里的确切公开名字（如是否含内部辅助）**待本地验证**，但上述四个文档函数一定存在。

#### 4.3.5 小练习与答案

**练习**：用户想调用卷积 `convolve`，下面哪种写法合法？为什么？
- (a) `from scipy.fftpack import convolve`
- (b) `from scipy.fftpack.convolve import convolve`
- (c) `scipy.fftpack.convolve.convolve(...)`

> **答案**：(b) 和 (c) 合法。(a) 也能「跑通」，但拿到的 `convolve` 是**子模块对象本身**（`scipy.fftpack.convolve`），不是卷积函数——因为子模块名恰好也叫 `convolve`，造成了同名遮蔽。这正是把卷积做成子模块带来的一个易混淆点：要调用卷积函数，得用 (b) 或 (c) 这类「再深入一层」的写法。

---

### 4.4 numpy 的再导出：`__module__` 揭示真实出处

#### 4.4.1 概念说明

文档里有这样一句提醒：`fftshift`、`ifftshift`、`fftfreq` 其实是 **numpy 的函数**，fftpack 只是「转手」提供。这在 Python 里叫**再导出（re-export）**：模块 A 用 `from B import x` 把 B 的对象 `x` 引入自己的命名空间，于是用户看起来好像 `x` 是 A 提供的，但对象本身仍是 B 的那个，连身份都没变。

怎样「识破」再导出？靠函数对象自带的 `__module__` 属性——它记录了**这个函数最初是在哪个模块里被 `def` 出来的**，无论之后被搬运到多少个命名空间，`__module__` 都不会撒谎。所以 `fftpack.fftshift.__module__` 会指向 `numpy`，而不是 `scipy.fftpack._helper`。

#### 4.4.2 核心流程

```text
numpy.fft.fftshift  (def 在 numpy 内部)
        │
        │  from numpy.fft import fftshift   ← 再导出
        ▼
scipy.fftpack._helper.fftshift   (命名空间里多了一个名字，对象不变)
        │
        │  from ._helper import *           ← 聚合导入（见 4.2）
        ▼
scipy.fftpack.fftshift           (顶层门面又多一个名字)
        │
        │  obj.__module__  永远指向定义处
        ▼
   'numpy.fft...'   ← 真实出处
```

关键点：再导出只是「起别名」，不复制、不重定义，因此 `__module__` 始终回溯到最初的定义模块。

#### 4.4.3 源码精读

再导出的「源头」就在 `_helper.py` 的第 4 行——这一行直接从 numpy 把三个函数搬进来：[`_helper.py:L4`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L4)：

```python
from numpy.fft import fftshift, ifftshift, fftfreq
```

随后 `_helper.py` 把这三个「搬来的」名字连同自己定义的 `rfftfreq`、`next_fast_len` 一起写进 `__all__`（[`_helper.py:L8`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L8)），再经 4.2 节的聚合导入出现在包顶层。

与之对照，`rfftfreq`（[`_helper.py:L11`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L11)）和 `next_fast_len`（[`_helper.py:L54`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L54)）是 fftpack **自己 `def` 的**，它们的 `__module__` 会是 `scipy.fftpack._helper`。

文档里也有明确提醒，建议优先从 numpy 导入这三个函数：[`__init__.py:L62-L63`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L62-L63)。

#### 4.4.4 代码实践

**目标**：用 `__module__` 亲手验证三个辅助函数来自 numpy，另两个来自 fftpack 自身。

**操作步骤**：

```python
import scipy.fftpack as fp
import numpy as np

for name in ['fftshift', 'ifftshift', 'fftfreq', 'rfftfreq', 'next_fast_len']:
    obj = getattr(fp, name)
    origin = "numpy" if obj.__module__.startswith("numpy") else "scipy"
    print(f"{name:14s} __module__ = {obj.__module__:30s} -> 来自 {origin}")

# 进一步：fftpack.fftshift 和 numpy.fft.fftshift 是不是同一个对象？
print("同一个对象:", fp.fftshift is np.fft.fftshift)
```

**需要观察的现象**：
- `fftshift`、`ifftshift`、`fftfreq` 的 `__module__` 以 `numpy` 开头；
- `rfftfreq`、`next_fast_len` 的 `__module__` 为 `scipy.fftpack._helper`；
- `fp.fftshift is np.fft.fftshift` 应为 `True`——证明是同一个对象，而非副本。

**预期结果**：如上。`__module__` 的完整字符串（例如究竟是 `numpy.fft.helper` 还是 `numpy.fft._helper`）随 numpy 版本变化，**待本地验证**，但前缀必然是 `numpy`。

#### 4.4.5 小练习与答案

**练习 1**：既然 `fftpack.fftshift is np.fft.fftshift` 为 `True`，那「fftpack 的 fftshift」和「numpy 的 fftshift」到底有没有区别？

> **答案**：运行时没有任何区别——它们是同一个函数对象。区别只在「出处与建议」：文档建议新代码直接从 numpy 导入，fftpack 提供它纯粹是为了向后兼容、方便老用户在一个命名空间下拿全所有工具。

**练习 2**：如果未来 numpy 升级，把 `numpy.fft.fftshift` 的实现改了，fftpack 这边会自动跟着变吗？需要 fftpack 发版吗？

> **答案**：会自动跟着变，且 fftpack **不需要**为此发版。因为 fftpack 只是在 import 时绑定了一个引用，对象本身仍是 numpy 的；numpy 升级后 `__module__` 指向的函数就是新版实现。这正是再导出「不复制、只引用」的特性带来的副作用——也是为什么文档劝你直接用源头（numpy）。

---

## 5. 综合实践

**任务：写一个「fftpack 公共 API 探针」工具。**

把本讲四个知识点串起来，写一个脚本，给定 `scipy.fftpack`，自动产出一份「公共 API 报告」，要求包含：

1. 公共名称总数（来自 `__all__`）；
2. 每个名字属于五大分组中的哪一组（用各子模块的 `__all__` 做归属判定）；
3. 哪些名字是 numpy 再导出的（用 `__module__` 判定，前缀为 `numpy` 即是）；
4. 卷积分组的四个函数（深入 `scipy.fftpack.convolve` 子模块）。

参考框架（你需要补全分组归类与再导出检测的逻辑）：

```python
import scipy.fftpack as fp

# 1. 子模块 → 分组名的映射
submod_to_group = {
    fp._basic:          "FFT",
    fp._realtransforms: "实变换 DCT/DST",
    fp._pseudo_diffs:   "伪微分算子",
    fp._helper:         "辅助函数",
}

# 2. 建一张「名字 -> 所属分组」的索引
name2group = {}
for sub, group in submod_to_group.items():
    for n in sub.__all__:
        name2group[n] = group

# 3. 遍历包级 __all__，归类并检测再导出
print(f"公共 API 总数: {len(fp.__all__)}\n")
for n in fp.__all__:
    obj = getattr(fp, n)
    grp = name2group.get(n, "未知")
    reexport = "(numpy 再导出)" if obj.__module__.startswith("numpy") else ""
    print(f"  {n:12s} [{grp}] {reexport}")

# 4. 单独列出卷积子模块
print("\n卷积子模块 scipy.fftpack.convolve:")
for n in ['convolve', 'convolve_z', 'init_convolution_kernel', 'destroy_convolve_cache']:
    print(f"  {n}")
```

**验收标准**：
- 31 个公共名字全部被正确归类到前四组之一，无「未知」；
- `fftshift`、`ifftshift`、`fftfreq` 三个被正确标记为 numpy 再导出；
- 卷积四个名字被单独列出，且你能在报告里指出它们**不在** `fp.__all__`。

**进阶（可选）**：把报告扩展为 Markdown 表格输出到屏幕，并在表头说明「分组」和「真实定义模块」两列，便于直接粘贴进学习笔记。

## 6. 本讲小结

- **`__all__` 是公共 API 的白名单**：它既是 `from module import *` 的取名单，也是作者对外承诺的契约；fftpack 的 `__all__` 共 31 项，且不含任何 `_` 开头的私有名字。
- **聚合导入实现门面模式**：四条 `from ._xxx import *` 把 `_basic`/`_pseudo_diffs`/`_helper`/`_realtransforms` 四个私有子模块的名字搬运到包顶层，拼出统一门面；每个子模块各自的 `__all__` 决定它贡献哪些名字。
- **数字自洽**：四个子模块 `__all__` 之和 \(8+10+5+8=31\)，恰好等于包级 `__all__`，两者必须同步维护。
- **五大分组地位不同**：FFT、实变换、伪微分、辅助函数四组是包顶层扁平名字（在 `__all__` 内）；卷积一组是独立子模块 `scipy.fftpack.convolve`，**不在** `__all__`，需带模块前缀访问。
- **`__module__` 能识破再导出**：`fftshift`/`ifftshift`/`fftfreq` 经 `_helper.py` 从 `numpy.fft` 转手而来，`__module__` 永远指向 numpy；它们与 `numpy.fft` 中的对象是同一个（`is` 为真）。
- **菜单 ≠ 后厨**：`dir(fftpack)` 还会看到 `basic`/`helper`/`convolve`/`test` 等「菜单外」的名字，它们不属于 `__all__` 承诺的公共 API。

## 7. 下一步学习建议

本讲只讲了「API 长什么样、从哪来」，还没碰任何变换的实际行为。建议按以下顺序继续：

1. **u1-l4 快速上手：一维复数 FFT**：从 `_basic.py` 的 `fft`/`ifft` 入手，亲手做一次变换—逆变换，理解 `n`、`axis`、`overwrite_x` 等参数与「标准打包顺序」。这是理解后续所有变换的基础。
2. **单元 2 核心变换族深入**：在会用 `fft` 之后，进入多维 FFT、`rfft` 实数打包、DCT/DST 等具体变换族。
3. **延展阅读**：等学完卷积相关内容（单元 3 伪微分算子），可以回头对照本讲的「卷积子模块」结论，体会为何 `convolve` 要做成独立命名空间——它的内核初始化与缓存（`init_convolution_kernel`/`destroy_convolve_cache`）正是下一篇章的重点。

> 阅读源码时，可随时回到 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py) 把它当作「目录页」：想找某类函数，先看它属于哪个分组、来自哪个子模块，再钻进对应文件读实现。
