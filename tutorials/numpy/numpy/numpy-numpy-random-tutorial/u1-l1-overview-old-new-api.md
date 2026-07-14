# 项目定位与新旧两套随机数 API

> 本讲是 `numpy.random` 学习手册的第一篇。你不需要事先读过任何源码，我们将从「`numpy.random` 到底是什么」讲起，建立起后续所有讲义都需要的大局观。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话说清 `numpy.random` 在 NumPy 中的定位与职责。
2. 区分两套随机数 API：推荐的 **新 API（`Generator` / `default_rng`）** 与遗留的 **旧 API（`RandomState` 与全局函数 `rand`/`randn`/`seed` 等）**。
3. 知道 `default_rng()` 是官方推荐的统一入口，并理解它为什么比全局函数更好。
4. 能对照真实源码，解释新 API 的导入路径与旧 API 的单例（singleton）机制。

## 2. 前置知识

在开始之前，你只需要具备以下基础：

- **会写最基本的 Python**：能 `import`、能调用函数、能看懂 `class`。
- **知道 NumPy 数组是什么**：`import numpy as np` 后，`np.array([1,2,3])` 这类概念。
- **听说过「伪随机数」**：计算机产生的「随机数」其实是由一个确定的算法、从一个初始值（种子 seed）出发算出来的序列。只要种子相同，序列就完全相同——这正是「可复现性」的基础。

几个本讲会用到的术语，先用大白话解释：

| 术语 | 大白话解释 |
| --- | --- |
| 伪随机数生成器（PRNG） | 一个确定性算法，输入一个种子，输出一串看起来随机的数。 |
| 种子（seed） | 启动生成器的初始值；种子固定，输出序列就固定。 |
| 流（stream） | 生成器持续产出的那一串数。 |
| 单例（singleton） | 整个程序里只有一个全局共享的实例。 |

本讲涉及的数学很简单，只需要知道均匀分布浮点数的取值范围是半开区间 \([0, 1)\)（即包含 0、不包含 1）即可。

## 3. 本讲源码地图

本讲只看 `numpy/random/` 顶层的「门面」文件，它们决定了外部世界能看到什么。后续讲义才会深入 `.pyx`（Cython 源码）与 `.c`（C 实现）内部。

| 文件 | 作用 | 本讲用到它做什么 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py) | `numpy.random` 包的入口；决定对外暴露哪些名字。 | 看新 API、旧 API 各自是怎么被导入并挂到 `np.random` 上的。 |
| [`__init__.pyi`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.pyi) | 类型存根（type stub）；给静态检查工具看的「目录索引」。 | 用最精简的形式确认对外公开的符号清单。 |
| [`_generator.pyx`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx) | 新 API 的 Cython 实现，定义 `Generator` 类与 `default_rng` 函数。 | 精读 `default_rng` 的派发逻辑。 |
| [`mtrand.pyx`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx) | 旧 API 的 Cython 实现，定义 `RandomState` 类与全局函数。 | 看全局函数如何绑定到一个共享单例 `_rand`。 |

> 提示：`.pyx` 是 [Cython](https://cython.readthedocs.io/) 源码，看起来很像 Python，但能编译成 C 以获得高速度。本讲你只需把它当作「带类型标注的 Python」来读，细节留到后续讲义。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`default_rng` 入口** —— 推荐的统一构造器。
2. **`Generator` 与 `BitGenerator`** —— 新 API 的两层架构。
3. **`RandomState` 与全局函数** —— 旧 API 的遗留兼容层。

---

### 4.1 default_rng 入口

#### 4.1.1 概念说明

`default_rng()` 是 NumPy 官方推荐的、创建随机数生成器的**统一入口**。它的名字里带 "default"，意味着：

- 你不需要纠结该选哪个底层算法——它会自动帮你选好默认的 `BitGenerator`（当前是 `PCG64`）。
- 你不需要关心内部用的是哪个类——它返回一个 `Generator` 实例，你直接调用它的方法即可。

一句话总结：**「想要随机数？先 `rng = np.random.default_rng()`，再调 `rng.xxx()`。」**

这一点在包文档的最开头就写明了，[`__init__.py` 的模块文档字符串第 6 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L6) 直接写道：

```python
Use ``default_rng()`` to create a `Generator` and call its methods.
```

#### 4.1.2 核心流程

`default_rng(seed)` 内部其实是一个「按 `seed` 的类型派发」的小型状态机。它的判定顺序是：

```
default_rng(seed)
   │
   ├─ seed 是 BitGenerator？ ───────► Generator(seed)        # 直接包装，复用传入的比特源
   │
   ├─ seed 是 Generator？   ───────► seed                   # 原样返回（幂等）
   │
   ├─ seed 是 RandomState？ ───────► Generator(seed._bit_generator)  # 把旧实例「升级」成新 API
   │
   └─ 其他（None / int / 序列 / SeedSequence）
                                    ───────► Generator(PCG64(seed))  # 默认路径：新建 PCG64 + Generator
```

关键结论：

- 当你什么都不传，或传一个整数/整数序列时，走的是最后一条默认路径——**新建一个 `PCG64` 作为 `BitGenerator`，再用 `Generator` 把它包起来**。
- 这个函数**不维护任何全局共享实例**（这点和旧 API 截然不同，见 4.3）。

#### 4.1.3 源码精读

`default_rng` 定义在 [`_generator.pyx:4991`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4991)，函数签名非常简洁：

```python
def default_rng(seed=None):
```

它的 `seed` 参数文档（[`:4996`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4996)）说明可以接受 `None / int / array_like[ints] / SeedSequence / BitGenerator / Generator / RandomState` 七种类型。

真正实现派发逻辑的，是函数末尾短短十几行（[`_generator.pyx:5071-5083`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5071-L5083)）：

```python
    if _check_bit_generator(seed):
        # We were passed a BitGenerator, so just wrap it up.
        return Generator(seed)
    elif isinstance(seed, Generator):
        # Pass through a Generator.
        return seed
    elif isinstance(seed, np.random.RandomState):
        gen = np.random.Generator(seed._bit_generator)
        return gen

    # Otherwise we need to instantiate a new BitGenerator and Generator as
    # normal.
    return Generator(PCG64(seed))
```

这四条分支精确对应 4.1.2 的状态机。最后一行 `return Generator(PCG64(seed))` 就是「默认路径」，它把 `seed` 交给 `PCG64`，再用 `Generator` 包装——**这就是「default」二字的全部含义**。

> 注意 `_check_bit_generator(seed)` 这个判断放最前：它把「用户已经传进来一个现成的 `BitGenerator`」这种情况优先处理掉，避免后面用 `PCG64(seed)` 把它重新覆盖。这也意味着 `default_rng` 不会浪费你手动创建的比特源。

`default_rng` 最终通过 [`_generator.pyx:5086`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5086) 的 `default_rng.__module__ = "numpy.random"` 把自己的 `__module__` 改写为 `numpy.random`，这样对外它看起来就像直接定义在 `numpy.random` 命名空间里。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `default_rng` 的「默认路径」返回的是一个 `Generator`，且其底层 `BitGenerator` 是 `PCG64`。
2. **操作步骤**：在 Python 解释器里运行下面的脚本（示例代码）：

   ```python
   # 示例代码
   import numpy as np

   rng = np.random.default_rng(42)
   print("rng       =", rng)
   print("类型      =", type(rng))
   print("底层 BG   =", rng.bit_generator)
   print("5 个浮点数 =", rng.random(5))
   ```
3. **需要观察的现象**：
   - `print(rng)` 会显示 `Generator(PCG64)`。
   - `type(rng)` 是 `numpy.random._generator.Generator`。
   - `rng.bit_generator` 是 `PCG64` 实例。
4. **预期结果**：输出第一行应为 `Generator(PCG64)`，这与 `default_rng` 文档示例（[`_generator.pyx:5028-5029`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5028-L5029)）一致；`random(5)` 的具体数值「待本地验证」（见下方练习，你会看到它与旧 API 完全不同）。

#### 4.1.5 小练习与答案

**练习 1**：`default_rng()` 不传任何参数时，`seed` 是什么？随机数还能复现吗？

> **参考答案**：`seed=None`。此时会从操作系统拉取「新鲜的、不可预测的熵」（见 [`_generator.pyx:4997-4998`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4997-L4998) 的说明），因此每次运行结果都不同，**不能复现**。想要复现，就必须显式传一个固定的种子。

**练习 2**：如果调用 `default_rng(rng)`（把一个已有的 `Generator` 再传进去），会发生什么？

> **参考答案**：走第二条分支 `isinstance(seed, Generator)`，原样返回同一个 `Generator`（[`_generator.pyx:5074-5076`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5074-L5076)）。也就是说 `default_rng` 对 `Generator` 是**幂等**的，不会新建实例。

---

### 4.2 Generator 与 BitGenerator

#### 4.2.1 概念说明

新 API 背后是一个清晰的**两层架构**：

- **BitGenerator（比特生成器）**：只干一件事——高速产出「原始的随机比特流」。它不关心你要什么分布，只负责吐 32/64 位的随机整数。`numpy.random` 提供了 `MT19937`、`PCG64`、`PCG64DXSM`、`Philox`、`SFC64` 五种可选。
- **Generator（生成器）**：拿到比特流后，**把它转换成各种概率分布**（均匀、正态、指数……），并对外暴露一组 NumPy 风格的 API（`random()`、`standard_normal()`、`integers()` 等）。

可以类比成一个水管系统：

```
    BitGenerator                Generator
  ┌──────────────┐   比特流   ┌──────────────────┐
  │ 产出原始比特  │ ─────────► │ 转成各种分布的数  │ ──► 用户
  └──────────────┘            └──────────────────┘
   (PCG64 等)                  (random/normal/...)
```

这种分层的好处是**关注点分离**：想换一个更快的比特源（比如 `SFC64`）时，上层所有分布方法都不用改。

`Generator` 在源码里被定义成一个「容器」（container）——见 [`_generator.pyx:142`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L142) 的类定义，其文档字符串第一句就是：

```python
cdef class Generator:
    """Generator(bit_generator)\n--

    Container for the BitGenerators.
```

#### 4.2.2 核心流程

一个 `Generator` 对象的生命周期：

```
1. 选择一个 BitGenerator（如 PCG64），传入种子
2. Generator(bit_generator) 持有该 BitGenerator
3. 调用 rng.random() / rng.standard_normal() 等方法
       └─► 方法内部经由 bit_generator 的 C 指针，调用 C 层采样函数
       └─► 返回 NumPy 数组（或单个标量）
```

`default_rng` 帮你把第 1、2 步合二为一：`Generator(PCG64(seed))`。

#### 4.2.3 源码精读

新 API 的所有公开符号，都集中在 [`__init__.py` 的导入区（第 180-191 行）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L180-L191)：

```python
from . import _bounded_integers, _common, _pickle
from ._generator import Generator, default_rng
from ._mt19937 import MT19937
from ._pcg64 import PCG64, PCG64DXSM
from ._philox import Philox
from ._sfc64 import SFC64
from .bit_generator import BitGenerator, SeedSequence
from .mtrand import *

__all__ += ['Generator', 'RandomState', 'SeedSequence', 'MT19937',
            'Philox', 'PCG64', 'PCG64DXSM', 'SFC64', 'default_rng',
            'BitGenerator']
```

逐行解读：

- [第 181 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L181)：导入新 API 的主角 `Generator` 与入口 `default_rng`。
- [第 182-185 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L182-L185)：导入五种 `BitGenerator`。
- [第 186 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L186)：导入 `BitGenerator` 基类与 `SeedSequence`（种子序列，第三单元细讲）。
- [第 189-191 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L189-L191)：把上面这些名字追加进 `__all__`，使它们成为 `np.random` 的「官方公开成员」。

> 注意 `Generator` 的文档里有一句 **「No Compatibility Guarantee」**（[`:159`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L159)）：新 API **不承诺**跨版本的位级别一致性——只要算法更好，比特流就可能变。这与旧 API（4.3 节）的「冻结」保证正好相反，是两者最本质的设计差异之一。

#### 4.2.4 代码实践

1. **实践目标**：验证 `Generator` 是一个可换「内核」的容器——同一个 `Generator`，换不同 `BitGenerator` 会得到不同的比特流。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from numpy.random import Generator, PCG64, MT19937

   print("PCG64 :", Generator(PCG64(42)).random(3))
   print("MT1993:", Generator(MT19937(42)).random(3))
   ```
3. **需要观察的现象**：两行输出的 3 个浮点数**完全不同**。
4. **预期结果**：两行都是长度为 3、取值在 \([0, 1)\) 的数组，但数值互不相同。这正说明 `BitGenerator` 决定了「比特源」，`Generator` 只负责把它们包装成同样的分布 API。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Generator(PCG64())` 和 `np.random.default_rng()` 产生的随机数会一样吗？

> **参考答案**：**不一样**，因为两者都没有传固定种子，都从操作系统获取了不同的熵。但若都传同一个种子，如 `Generator(PCG64(7))` 与 `default_rng(7)`，则**完全一样**——因为 `default_rng(7)` 走的就是 `Generator(PCG64(7))` 这条默认路径（见 4.1.3）。

**练习 2**：为什么要把「产出比特」和「转换分布」拆成两层？

> **参考答案**：为了**关注点分离**与**可替换性**。比特层的算法（PCG64/Philox/SFC64…）可以单独优化和替换，而不影响分布层的 API；分布层也可以针对不同比特源共用同一套采样代码。这种解耦让「换一个更快的生成器」变成一行代码的事。

---

### 4.3 RandomState 与全局函数

#### 4.3.1 概念说明

旧 API 是 NumPy 1.17 之前唯一的随机数接口，至今仍被保留，用于**向后兼容**。它有两层含义：

1. **`RandomState` 类**：基于 `MT19937`（Mersenne Twister，梅森旋转）算法的遗留生成器。官方在它的文档里直接建议你「改用 `Generator`」（见 [`mtrand.pyx:125-127`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L125-L127)）：

   > Container for the slow Mersenne Twister pseudo-random number generator. Consider using a different BitGenerator with the Generator container instead.

2. **模块级全局函数**：你在很多老代码里见过的 `np.random.rand()`、`np.random.randn()`、`np.random.seed()`、`np.random.randint()` 等等。它们其实**并不是独立的生成器**，而全部绑定到一个**全局共享的单例**上。

`__init__.py` 的文档把这些全局函数分成三类（[`__init__.py:39-62`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L39-L62)）：「工具函数」（如 `shuffle`/`choice`）、「在新 API 中已移除的兼容函数」（如 `rand`/`randn`/`seed`/`randint`）和「单变量/多变量/标准分布函数」（如 `normal`/`binomial`/`multivariate_normal`）。

#### 4.3.2 核心流程

旧 API 的全局函数之所以「全局」，关键在于这一行：

```
_rand = RandomState()        # 模块加载时，创建一个全局共享的 RandomState
rand  = _rand.rand           # 把它的方法「拍平」成模块级函数
randn = _rand.randn
seed  = ...                  # seed() 重置这个共享 _rand
```

于是你在任何地方调用 `np.random.rand(5)`，本质上都是在调用**同一个** `_rand` 实例的 `rand` 方法。这带来一个隐患：**任何一处调用 `np.random.seed()` 或消费随机数，都会影响全局状态**，让程序难以复现、难以并行。

#### 4.3.3 源码精读

`RandomState` 类定义在 [`mtrand.pyx:121`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L121)：

```python
cdef class RandomState:
    """RandomState(seed=None)\n--

    Container for the slow Mersenne Twister pseudo-random number generator.
```

注意它文档里的 **「Compatibility Guarantee」**（[`:137-145`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L137-L145)）：旧 API **承诺**固定种子 + 固定调用序列 = 永远相同的输出（除修正 bug 外）。因此它被「冻结」，只做必要维护——这正是它仍被保留的原因：科学计算里大量旧结果依赖这套确定序列。

全局单例与函数绑定，集中在 [`mtrand.pyx:4764-4812`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L4764-L4812)。摘录关键几行：

```python
_rand = RandomState()         # 4764：模块级唯一单例

beta = _rand.beta             # 4766 起：把方法拍平成模块级函数
...
rand = _rand.rand             # 4793
randint = _rand.randint       # 4794
randn = _rand.randn           # 4795
random = _rand.random         # 4796
...
```

而 `seed()` 函数（[`mtrand.pyx:4814`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L4814)）的作用就是「重置这个单例」：

```python
def seed(seed=None):
    """seed(seed=None)

    Reseed the singleton RandomState instance.

    Notes
    -----
    This is a convenience, legacy function ... Best practice
    is to use a dedicated ``Generator`` instance rather than
    the random variate generation methods exposed directly in
    the random module.
    ...
```

注意官方在这里又一次明确建议：**最佳实践是用一个专属的 `Generator` 实例，而不是这些模块级方法。**

最后，[`__init__.py:187`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L187) 的一行 `from .mtrand import *` 把这些全局函数连同 `RandomState` 一起倒进了 `np.random` 命名空间——这就是你能直接写 `np.random.rand` 的原因。类型存根 [`__init__.pyi:7-61`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.pyi#L7-L61) 显式列出了从 `mtrand` 导入的 `RandomState` 及全部函数名，可以当作一份精确的「旧 API 清单」来查。

#### 4.3.4 代码实践

1. **实践目标**：感受「全局单例」带来的隐式共享状态——验证 `np.random.rand` 与 `np.random.random` 操作的是同一个内部状态。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import numpy as np

   np.random.seed(0)
   a = np.random.rand(2)          # 消费 2 个数
   b = np.random.random_sample(2) # 再消费 2 个数
   print("a =", a)
   print("b =", b)

   np.random.seed(0)              # 重置同一个单例
   c = np.random.rand(4)          # 一次性消费 4 个数
   print("c =", c)
   ```
3. **需要观察的现象**：`c` 的前两个数应等于 `a`，后两个数应等于 `b`。
4. **预期结果**：因为 `seed(0)` 把那个全局 `_rand` 重置回同一起点，`a+b` 拼起来应当与 `c` 完全一致。具体数值「待本地验证」。这恰好证明了：所有旧的全局函数共享同一个状态机。

#### 4.3.5 小练习与答案

**练习 1**：为什么说在多线程或大型程序里使用 `np.random.seed()` + 全局函数是「坏味道」？

> **参考答案**：因为存在一个**全局可变单例** `_rand`（[`mtrand.pyx:4764`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L4764)）。任何线程任何位置调用 `seed()` 或消费随机数，都会修改这个共享状态，导致：①结果难以复现；②多线程间互相干扰；③不同模块之间隐式耦合。新 API 用「每个任务一个 `Generator` 实例」避免了这一切。

**练习 2**：`RandomState(42).random(3)` 和 `np.random.default_rng(42).random(3)` 的输出会一样吗？为什么？

> **参考答案**：**不一样**。前者用 `MT19937`，后者默认用 `PCG64`（见 4.1）。即使种子都是 42，两个完全不同的算法产生的比特流自然不同。**「种子相同 ⇒ 输出相同」只在同一个算法/同一套 API 内部成立。**

**练习 3**：既然新 API 更好，为什么 `RandomState` 和全局函数不直接删掉？

> **参考答案**：因为旧 API 给出了**跨版本兼容保证**（[`mtrand.pyx:137-145`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L137-L145)），大量历史科学代码、已发表论文里的数值结果都依赖这套确定序列。删掉会破坏可复现性。所以官方选择「冻结旧 API、主推新 API」的双轨策略。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个对比任务（这正是本讲规格里指定的实践）。

**任务**：分别用新 API 和旧 API 各生成 5 个 \([0, 1)\) 的浮点数，对比它们的方法名、输出，并写一句话总结新 API 的推荐理由。

**参考脚本**（示例代码）：

```python
# 示例代码
import numpy as np

# ---- 新 API ----
rng = np.random.default_rng(42)
new_out = rng.random(5)

# ---- 旧 API ----
rs = np.random.RandomState(42)
old_out = rs.random_sample(5)

print("新 API (Generator)  方法名: random        ->", new_out)
print("旧 API (RandomState)方法名: random_sample ->", old_out)
print("两者相同？", np.allclose(new_out, old_out))

# 顺带感受全局单例的「隐式状态」
np.random.seed(42)
legacy_global = np.random.random_sample(5)
print("全局函数 random_sample       ->", legacy_global)
print("全局函数与 RandomState(42) 相同？", np.allclose(legacy_global, old_out))
```

**观察与思考清单**：

1. `new_out` 与 `old_out` **数值不同**——因为新 API 用 `PCG64`、旧 API 用 `MT19937`（对应 4.2 与 4.3）。
2. 新 API 的方法叫 `random()`，旧 API 对应 `random_sample()`（旧 API 里 `random` 是 `random_sample` 的别名）——**命名也不统一**。
3. `legacy_global` 应当与 `old_out` **相同**（具体数值「待本地验证」），因为 `np.random.seed(42)` 把全局单例 `_rand` 重置成与 `RandomState(42)` 相同的起点。

**用一句话写下你的总结**，参考口径：

> 新 API（`default_rng`/`Generator`）推荐使用，因为它基于更好的默认算法 `PCG64`、不依赖全局共享状态（每个实例独立、可并行可复现）、命名统一；旧 API（`RandomState`/全局函数）仅作为向后兼容保留。

## 6. 本讲小结

- `numpy.random` 是 NumPy 的随机数子系统，官方推荐入口是 **`default_rng()`**，它返回一个 **`Generator`**（[`__init__.py:6`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L6)）。
- 新 API 采用 **两层架构**：`BitGenerator` 产出比特流，`Generator` 转换成各种分布（[`_generator.pyx:142`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L142)）。
- `default_rng` 内部是按 `seed` 类型派发的小状态机，默认路径是 `Generator(PCG64(seed))`（[`_generator.pyx:5071-5083`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5071-L5083)）。
- 旧 API 的 `RandomState` 基于 `MT19937`，且**所有全局函数（`rand`/`randn`/`seed`/…）都绑定到一个模块级单例 `_rand`**（[`mtrand.pyx:4764-4812`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/mtrand.pyx#L4764-L4812)）。
- 两者最关键差异：旧 API **保证**跨版本位级别一致（被冻结）；新 API **不保证**（可随算法改进而变化）。
- 因此「同种子 ⇒ 同输出」**只在同一套 API 内**成立——`default_rng(42)` 与 `RandomState(42)` 的结果完全不同。

## 7. 下一步学习建议

本讲建立了「新旧两套 API」的大局观，接下来建议：

- **`u1-l2` 目录结构、构建系统与模块地图**：深入 `meson.build`，看 `.pyx`/`.pxd`/`.c` 三层源码如何编译成 `npyrandom` 静态库与各个扩展模块。这是理解后续所有源码阅读的基础。
- **`u1-l3` 从 default_rng 起步：运行第一个程序**：把本讲的 `default_rng` 真正跑起来，亲手感受种子与可复现性。
- 想提前了解两层架构细节的读者，可以先扫一眼 [`_generator.pyx:142`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L142)（`Generator` 类）与第二单元的 `bitgen_t` 结构。
