# 从 default_rng 起步：运行第一个程序

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `numpy.random.default_rng()` 创建一个 `Generator` 实例，并知道它默认使用 `PCG64` 作为底层比特生成器。
- 理解 `Generator` 是「持有 BitGenerator 的容器」，能说出 `default_rng` 在内部对 `seed` 做了哪几种分支派发。
- 区分 `seed=None` 与 `seed=<具体值>` 的差别，理解为什么「固定种子」能带来可复现性，以及它的边界（同版本、同调用顺序）。
- 亲手运行第一个可复现的随机数程序，并能解释「重新运行得到完全一致结果」背后的原因。

本讲是整个手册的「第一行可运行代码」，因此侧重直觉与动手，不深入算法细节（分布算法留到第 4、5 单元，种子混合留到第 3 单元）。

## 2. 前置知识

在开始前，你需要具备以下概念（前两讲已建立，这里做一句话回顾）：

- **numpy.random 子系统**：NumPy 中负责生成随机数的模块，既包含推荐的新 API，也保留了向后兼容的旧 API。
- **两层架构**：`BitGenerator` 只负责产出「原始随机比特流」（如 PCG64、MT19937），`Generator` 把比特流「翻译」成各种概率分布的样本。`default_rng()` 返回的就是一个 `Generator`。
- **新旧 API**：新 API 以 `default_rng()` / `Generator` 为核心、不维护全局状态、不保证跨版本比特流一致；旧 API 以 `RandomState` 和模块级全局函数（`np.random.rand` 等）为核心、共享一个全局单例、冻结了比特流。

如果你还不清楚上面任何一个名词，建议先读 `u1-l1` 与 `u1-l2`。本讲会直接使用这些结论。

此外，你需要能运行 Python 3 并 `import numpy as np`。可以在交互式终端（REPL）、`.py` 脚本或 Jupyter 中练习。

## 3. 本讲源码地图

本讲只涉及两个关键源码文件：

| 文件 | 作用 |
| --- | --- |
| [_generator.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx) | Cython 源码，定义 `Generator` 类与 `default_rng()` 函数。本讲核心。 |
| [__init__.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py) | `numpy.random` 包的入口，负责把 `Generator`、`default_rng` 等名字对外暴露。 |

> 提示：`.pyx` 是 Cython 源文件，在构建阶段会被编译成 C 再编译成 Python 扩展模块（见 `u1-l2`）。阅读 `.pyx` 就像阅读带类型标注的 Python，初学者可以先忽略 `cdef` 等关键字。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**default_rng 函数**、**Generator 构造**、**seed 参数**。三者环环相扣：`default_rng` 根据 `seed` 的类型决定如何构造 `Generator`，而 `Generator` 的构造又依赖 `seed` 最终产生的 BitGenerator 初始状态。

### 4.1 default_rng 函数：推荐的统一入口

#### 4.1.1 概念说明

`default_rng()` 是官方推荐的、创建随机数生成器的「一站式入口」。它做了两件让初学者省心的事：

1. **帮你选好默认 BitGenerator**：你不需要自己去 `import PCG64`，`default_rng` 默认就用 `PCG64`（一个速度快、统计质量高的现代生成器）。
2. **根据你传入的 `seed` 类型自动决定怎么做**：传入一个整数、一个 BitGenerator、一个旧的 `RandomState`……它都能正确处理。

更关键的一点（来自上一讲的全局观）：`default_rng()` **不维护任何全局实例**。每次调用都会创建一个全新的、独立的 `Generator`。这正是它与旧 API `np.random.seed()` / `np.random.rand()` 的本质区别——后者操作的是一个全局共享的单例，前者则是「每次自己拿一个新的」。

#### 4.1.2 核心流程

`default_rng(seed)` 的派发逻辑可以用下面这段伪代码概括：

```
def default_rng(seed=None):
    if seed 是一个 BitGenerator 实例:
        return Generator(seed)              # 直接包装
    elif seed 是一个 Generator 实例:
        return seed                          # 原样透传
    elif seed 是一个 RandomState 实例(旧 API):
        return Generator(seed._bit_generator)  # 强制转换为新 API
    else:
        # seed 是 None / int / 整数序列 / SeedSequence
        return Generator(PCG64(seed))        # 用默认 BitGenerator 构造
```

也就是说，对绝大多数初学者而言，走的是最后一条分支 `return Generator(PCG64(seed))`。

#### 4.1.3 源码精读

先看 `default_rng` 在 `__init__.py` 中是如何被对外暴露的。`numpy.random` 的模块文档字符串第一句就点明了用法：

[__init__.py:6] 使用 `default_rng()` 来创建 `Generator` 并调用其方法。
[使用 default_rng 的说明](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L6)

而真正的导入发生在这一行：

[__init__.py:181] 从编译后的 `_generator` 扩展模块中导入 `Generator` 和 `default_rng`。
[导入 default_rng](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L181)

```python
from ._generator import Generator, default_rng
```

所以 `np.random.default_rng` 与 `from numpy.random import default_rng` 拿到的是同一个函数。

接下来看 `default_rng` 的实现（关键部分，省略了文档字符串）：

[_generator.pyx:4991-5083] `default_rng` 函数：先处理「传入的已经是生成器」的特殊情况，最后兜底用 `PCG64(seed)` 构造默认 `Generator`。
[default_rng 派发逻辑](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5071-L5083)

```cython
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

几个要点：

- `_check_bit_generator(seed)` 用来判断 `seed` 是不是一个 `BitGenerator` 实例（比如 `PCG64()`、`MT19937()`）。如果是，直接 `Generator(seed)` 包装。
- 若传入的是一个 `Generator`，原样返回——这是一种「幂等」设计：`default_rng(default_rng(42))` 不会出错。
- 若传入旧的 `RandomState`，则取出它的 `_bit_generator` 转成新 API，方便从旧代码平滑迁移。
- 兜底分支 `Generator(PCG64(seed))` 是初学者最常走的路径：`seed` 会被原样传给 `PCG64` 的构造函数。

文档字符串里还有一句非常重要的话，直接点明了它和旧 API 的区别：

> If `seed` is not a `BitGenerator` or a `Generator`, a new `BitGenerator` is instantiated. **This function does not manage a default global instance.**
> （此函数不维护任何默认的全局实例。）

这句话解释了为什么 `default_rng()` 是「干净的」：它不会污染全局状态，每次调用相互独立。

#### 4.1.4 代码实践

**实践目标**：直观感受 `default_rng` 对不同 `seed` 类型的派发，并验证「不维护全局实例」。

**操作步骤**：

1. 在 Python 中分别创建几个 `Generator`，观察它们的 `repr`（字符串表示）。
2. 用 `is` 判断两个变量是否指向同一个对象。

```python
import numpy as np

# 1) 默认走兜底分支：Generator(PCG64(seed))
rng1 = np.random.default_rng(2024)
print(repr(rng1))          # Generator(PCG64) at 0x...

# 2) 传入一个 BitGenerator，走第一分支：直接包装
bg = np.random.PCG64(2024)
rng2 = np.random.default_rng(bg)
print(repr(rng2))          # Generator(PCG64) at 0x...

# 3) 传入一个 Generator，走第二分支：原样透传
rng3 = np.random.default_rng(rng1)
print(rng3 is rng1)        # True —— 同一个对象

# 4) 不维护全局实例：两次调用得到两个独立对象
a = np.random.default_rng(1)
b = np.random.default_rng(1)
print(a is b)              # False —— 每次都是新对象
```

**需要观察的现象**：

- `rng3 is rng1` 应为 `True`（透传）。
- `a is b` 应为 `False`（每次新建，不共享全局单例）。

**预期结果**：透传返回同一个对象；而即使传入相同整数 `1`，两次 `default_rng(1)` 也返回两个不同对象。这正是「不维护全局实例」的直接证据。

#### 4.1.5 小练习与答案

**练习 1**：`np.random.default_rng(np.random.default_rng(7))` 的返回值，与传入的对象是什么关系？
**答案**：是同一个对象（`is` 为 `True`）。因为 `default_rng` 检测到入参已经是 `Generator`，会走「透传」分支直接返回它。

**练习 2**：把一个 `np.random.RandomState(0)` 传给 `default_rng`，返回值的类型是什么？它和原 `RandomState` 还是同一个对象吗？
**答案**：返回一个 `Generator`（不是 `RandomState`）。它们不是同一个对象，但 `Generator` 复用了 `RandomState` 内部的 `_bit_generator`，从而实现「从旧 API 平滑迁移到新 API」。

---

### 4.2 Generator 构造：组合一个 BitGenerator

#### 4.2.1 概念说明

`Generator` 不是「自己产生随机数」的机器，它更像一个**外壳**：内部持有一个 `BitGenerator`，并提供一大批方法（`random`、`integers`、`standard_normal`……）把底层比特流翻译成分布样本。

你可以把两者关系想象成：

- **BitGenerator**：一台不断吐出 0/1 比特的「原材料机」。
- **Generator**：一台「加工车间」，把比特原材料加工成均匀分布、正态分布、整数等各种成品。

这种「组合（composition）」关系意味着：同一个 `Generator` 类可以搭配不同的 `BitGenerator`，换内核而不换接口。这为后面第 6 单元（MT19937 / PCG64 / Philox / SFC64）的切换打下基础。

#### 4.2.2 核心流程

`Generator.__init__` 做了三件事：

```
def __init__(self, bit_generator):
    1. 记住传入的 bit_generator（保存为 self._bit_generator）
    2. 从 bit_generator 取出 capsule（一个 PyCapsule），校验它是合法的 "BitGenerator"
    3. 从 capsule 里取出 C 层的 bitgen_t 结构（函数指针表），保存为 self._bitgen
    4. 共享 bit_generator 的锁 self.lock（用于线程安全）
```

第 2、3 步是关键：`Generator` 通过 `capsule` 这个「C 指针包装盒」拿到底层 C 结构 `bitgen_t`，之后所有分布方法都是通过这个结构去调用 C 采样函数的。`bitgen_t` 的细节会在 `u2-l1` 详讲，本讲你只需要知道「有这么一座桥」即可。

#### 4.2.3 源码精读

先看 `Generator` 类的成员声明，它体现了「组合」关系：

[_generator.pyx:190-194] `Generator` 的 Cython 成员：`_bit_generator` 保存 Python 层的 BitGenerator 对象，`_bitgen` 是从 capsule 取出的 C 层结构，`lock` 用于线程安全。
[Generator 成员声明](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L190-L194)

```cython
    cdef public object _bit_generator
    cdef bitgen_t _bitgen
    cdef binomial_t _binomial
    cdef object lock
```

然后是构造函数本体：

[_generator.pyx:196-205] `Generator.__init__`：保存 BitGenerator，通过 capsule 校验并取出 C 层 `bitgen_t`，并共享其锁。
[Generator.__init__](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L196-L205)

```cython
    def __init__(self, bit_generator):
        self._bit_generator = bit_generator

        capsule = bit_generator.capsule
        cdef const char *name = "BitGenerator"
        if not PyCapsule_IsValid(capsule, name):
            raise ValueError("Invalid bit generator. The bit generator must "
                             "be instantiated.")
        self._bitgen = (<bitgen_t *> PyCapsule_GetPointer(capsule, name))[0]
        self.lock = bit_generator.lock
```

逐行解读：

- `self._bit_generator = bit_generator`：保存 Python 对象，之后可以用 `self.bit_generator` 属性访问。
- `capsule = bit_generator.capsule`：取出 BitGenerator 暴露的 `PyCapsule`（一个携带 C 指针的标准 Python 对象）。
- `PyCapsule_IsValid(capsule, "BitGenerator")`：校验这个 capsule 确实是 NumPy 认识的「BitGenerator」类型，名字必须精确匹配 `"BitGenerator"`。这能防止把别的 capsule 误传进来。
- `self._bitgen = (<bitgen_t *> PyCapsule_GetPointer(capsule, name))[0]`：从 capsule 里取出指向 `bitgen_t` 的指针，并**解引用复制**整个结构到 `self._bitgen`。从此 `Generator` 就直接持有这份 C 层函数指针表。
- `self.lock = bit_generator.lock`：共享同一把锁，保证多线程下底层状态不被破坏。

注意「直接构造 `Generator`」也是合法的——文档示例就给出了这种用法：

```python
>>> from numpy.random import Generator, PCG64
>>> rng = Generator(PCG64())
>>> rng.standard_normal()
```

也就是说，`default_rng(42)` 本质上等价于 `Generator(PCG64(42))`，只是 `default_rng` 帮你写了 `PCG64(...)` 这一层，并额外处理了 BitGenerator/Generator/RandomState 等特殊情况。

#### 4.2.4 代码实践

**实践目标**：亲手用两种方式构造同一个 `Generator`，验证它们等价；并观察 `bit_generator` 属性。

**操作步骤**：

```python
import numpy as np
from numpy.random import Generator, PCG64

# 方式 A：直接构造（文档示例写法）
rng_a = Generator(PCG64(42))

# 方式 B：用 default_rng 兜底分支
rng_b = np.random.default_rng(42)

# 两者底层都是 PCG64，且用相同种子，前几个数应一致
print(rng_a.bit_generator)            # PCG64
print(type(rng_a.bit_generator))      # <class 'numpy.random._pcg64.PCG64'>

print(rng_a.random(3))
print(rng_b.random(3))                # 与上一行相同
```

**需要观察的现象**：`rng_a.bit_generator` 打印为 `PCG64`；`rng_a.random(3)` 与 `rng_b.random(3)` 的数值**完全相同**（因为底层 BitGenerator 和种子都一样）。

**预期结果**：两种构造方式产生相同的比特流，因此 `random(3)` 的输出逐位一致。这验证了「`default_rng(42)` ≈ `Generator(PCG64(42))`」。

> 数值结果「待本地验证」：具体浮点数取决于你本机的 NumPy 版本，但同一次运行中两行必定相同。

#### 4.2.5 小练习与答案

**练习 1**：如果执行 `Generator(42)`（直接把整数 42 传给 `Generator`），会发生什么？为什么？
**答案**：会抛 `AttributeError`（`int` 没有 `capsule` 属性）或类似错误。因为 `Generator.__init__` 第一行就访问 `bit_generator.capsule`，而整数没有这个属性。`Generator` 只能接受 `BitGenerator` 实例，整数要先交给 `PCG64`/`default_rng` 处理。

**练习 2**：`Generator.__init__` 里为什么要做 `PyCapsule_IsValid(capsule, "BitGenerator")` 这一步校验？
**答案**：为了防止传入「不是 NumPy BitGenerator」的 capsule。capsule 名字 `"BitGenerator"` 相当于一个类型标签，校验它能保证取出的指针确实指向合法的 `bitgen_t`，避免内存安全问题。

---

### 4.3 seed 参数：可复现性的关键

#### 4.3.1 概念说明

「随机数」听起来不可预测，但计算机里的随机数其实是**伪随机（pseudo-random）**：由一个确定的算法，从一个确定的起点出发，产生一串看起来随机的数列。这个「起点」就是 **seed（种子）**。

可复现性的核心原理就一句话：

> **相同的 seed + 相同的抽取顺序 = 相同的输出。**

`default_rng` 对 `seed` 的处理有三种典型情况：

| seed 取值 | 行为 | 是否可复现 |
| --- | --- | --- |
| `None`（默认） | 从操作系统获取新鲜熵（如 `/dev/urandom`、时间等） | ❌ 每次都不同 |
| 一个整数 `int` | 交给 `SeedSequence` 推导出 BitGenerator 初始状态 | ✅ 可复现 |
| 一个整数序列 / `SeedSequence` | 同上，可提供更多熵 | ✅ 可复现 |

所以：

- 想「每次都不一样」：`rng = np.random.default_rng()`（不传 seed）。
- 想「每次都一样」（做实验、调试、单元测试）：`rng = np.random.default_rng(42)`。

#### 4.3.2 核心流程

当 `seed` 是 `None` 或整数时，`default_rng` 走兜底分支 `Generator(PCG64(seed))`，整体流程是：

```
default_rng(42)
   └─> PCG64(42)
          └─> 42 被包装成 SeedSequence
                 └─> SeedSequence 把 42「混合/扩散」成一串 uint32 状态字
                        └─> 用这串状态字初始化 PCG64 的内部计数器
                           └─> Generator 持有这个 PCG64，准备产出样本
```

其中 `SeedSequence` 的「混合/扩散」算法是第 3 单元（`u3-l1`、`u3-l2`）的重点。本讲你只需记住结论：**同样的 seed 会被确定性地转成同样的初始状态，因此整条数列都可复现。**

还需要理解一个边界条件（呼应 `u1-l1` 的「兼容承诺」）：

- `Generator` **不保证跨 NumPy 版本的比特流一致**（类文档里明确写着 *No Compatibility Guarantee*）。算法改进时，同一个 `seed=42` 在未来版本可能产生不同的数列。
- 因此「同 seed ⇒ 同输出」只在**同一个 NumPy 版本、同一套调用顺序**内成立。需要永久冻结结果的场景（如复现旧论文）应考虑使用 `RandomState`。

#### 4.3.3 源码精读

`default_rng` 的文档字符串清楚地列出了 `seed` 的所有合法类型：

[_generator.pyx:4996-5004] `seed` 可以是 `None`/`int`/整数数组/`SeedSequence`/`BitGenerator`/`Generator`/`RandomState`；`None` 时从 OS 拉取新鲜熵，整数则交给 `SeedSequence` 推导初始状态。
[seed 参数说明](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4996-L5004)

而兜底分支正是把 `seed` 原样交给 `PCG64`：

[_generator.pyx:5081-5083] 兜底分支：`seed` 不是已有生成器时，用 `Generator(PCG64(seed))` 构造默认生成器。
[兜底分支 PCG64(seed)](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5081-L5083)

```cython
    # Otherwise we need to instantiate a new BitGenerator and Generator as
    # normal.
    return Generator(PCG64(seed))
```

`Generator` 类的文档里也直接给出了「同 seed ⇒ 同输出」的示例，并强调重启解释器后结果依然一致：

[_generator.pyx:5047-5068] 文档示例：`default_rng(seed=42)` 后重启解释器再运行，`random((3,3))` 输出完全一致——这就是可复现性。
[可复现性示例](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L5047-L5068)

而「不保证跨版本一致」的声明写在 `Generator` 类文档里：

[_generator.pyx:159-162] `Generator` 明确声明 *No Compatibility Guarantee*：随算法改进，比特流可能改变。
[No Compatibility Guarantee 声明](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L159-L162)

> 这两条合起来，就是「可复现性的边界」：版本内可复现，跨版本不保证。

#### 4.3.4 代码实践

**实践目标**：亲手验证「固定 seed ⇒ 两次运行结果完全一致」，并体会 `seed=None` 的不可复现性。

**操作步骤**：

```python
import numpy as np

# (1) 固定 seed：两次构造、分别抽样，结果应完全一致
run1 = np.random.default_rng(2024)
run2 = np.random.default_rng(2024)

a1 = run1.standard_normal((3, 3))
a2 = run2.standard_normal((3, 3))
print(a1)
print(np.array_equal(a1, a2))   # True

# (2) 同一个对象连续抽取：第二次会接着第一次往后走（状态会推进）
b1 = run1.integers(0, 10, size=5)
b2 = run1.integers(0, 10, size=5)
print(b1)
print(b2)                       # 与 b1 不同，因为状态已被 b1 推进

# (3) seed=None：每次都不同
print(np.array_equal(
    np.random.default_rng().random(5),
    np.random.default_rng().random(5),
))                              # 几乎必然为 False
```

**需要观察的现象**：

- `a1` 与 `a2` 完全相同（`np.array_equal` 为 `True`）——这是固定 seed 带来的可复现性。
- `b1` 与 `b2` 不同——因为它们来自**同一个**生成器的两次连续抽取，内部状态在第一次抽取后已经前进。
- 第 (3) 步两个数组几乎必然不同——`seed=None` 每次拉取新鲜熵。

**预期结果**：固定 seed 重建生成器可逐位复现；但同一个生成器连续抽两次会得到不同结果（状态在推进）；`seed=None` 无法复现。

> 具体数值「待本地验证」：它们依赖本机 NumPy 版本，但上述三个「同/不同」的判断关系是确定的。

#### 4.3.5 小练习与答案

**练习 1**：下面两段代码，哪一段能保证两次输出完全相同？为什么？

```python
# A
rng = np.random.default_rng(7)
print(rng.random(3))
print(rng.random(3))
```
```python
# B
print(np.random.default_rng(7).random(3))
print(np.random.default_rng(7).random(3))
```

**答案**：**B** 能保证两次相同。B 每次都用 `seed=7` 新建生成器，初始状态相同，第一次抽取必然相同。而 A 是同一个生成器连续抽两次，状态在第一次后已推进，所以两次不同。这正说明「可复现 = 相同 seed + 相同调用顺序」，缺一不可。

**练习 2**：为什么说「固定 seed 也不代表能跨 NumPy 版本复现」？
**答案**：因为 `Generator` 类明确声明 *No Compatibility Guarantee*，当分布算法或 BitGenerator 改进时，比特流可能改变。固定 seed 只保证在**同一版本、同一调用顺序**下复现。需要跨版本冻结结果时应使用 `RandomState`（见 `u8-l1`）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿任务（这是本讲规格要求的代码实践）：

**任务**：编写一个脚本 `first_rng.py`，调用 `default_rng(2024)`，完成以下三件事，并把脚本运行两次，验证结果完全一致。

1. 生成一个 **3×3 的标准正态矩阵**（均值 0、标准差 1）。
2. 生成 **10 个 0–9 的整数**（注意 `integers` 的区间是左闭右开）。
3. 额外做一个「同 seed 重建」的断言：用 `default_rng(2024)` 再建一个生成器抽同样的整数，断言两次结果 `np.array_equal` 为 `True`。

**参考脚本（示例代码）**：

```python
# first_rng.py —— 本讲综合实践示例代码
import numpy as np

def make_samples(seed=2024):
    rng = np.random.default_rng(seed)
    normal_mat = rng.standard_normal((3, 3))   # 3x3 标准正态
    ints = rng.integers(0, 10, size=10)        # 10 个 [0, 10) 的整数
    return normal_mat, ints

if __name__ == "__main__":
    m1, i1 = make_samples()
    m2, i2 = make_samples()                    # 重新用同一种子构造

    print("标准正态矩阵:\n", m1)
    print("0-9 整数:", i1)

    # 可复现性断言
    assert np.array_equal(m1, m2), "正态矩阵不一致"
    assert np.array_equal(i1, i2), "整数序列不一致"
    print("✅ 两次运行结果完全一致，可复现性验证通过")
```

**操作步骤**：

1. 把上面的脚本保存为 `first_rng.py`。
2. 运行 `python first_rng.py`，记录输出。
3. 再运行一次 `python first_rng.py`，对比两次输出。

**需要观察的现象**：

- 两次运行的「标准正态矩阵」和「整数序列」**逐位相同**。
- 断言全部通过，打印 ✅。
- 正态矩阵的元素有正有负、大致围绕 0 波动；整数序列每个值都在 0–9 之间。

**预期结果**：固定 `seed=2024`，两次独立运行产生完全一致的输出，可复现性成立。

> 具体数值「待本地验证」：它们依赖你本机的 NumPy 版本（因 *No Compatibility Guarantee*）。但「两次运行完全一致」这一点在本机内必然成立。

**进阶思考（可选）**：把脚本里的 `default_rng(2024)` 改成 `default_rng()`（不传 seed），再运行两次，观察输出是否还相同，并用一句话解释原因。（答案：不再相同，因为 `seed=None` 每次从 OS 拉取新鲜熵。）

---

## 6. 本讲小结

- `default_rng()` 是官方推荐的创建随机数生成器的统一入口，默认返回 `Generator(PCG64(seed))`，且**不维护任何全局实例**——每次调用都是独立的新对象。
- `default_rng(seed)` 会按 `seed` 的类型做四路派发：BitGenerator（直接包装）、Generator（透传）、RandomState（转换）、其它（用 `PCG64(seed)` 兜底构造）。
- `Generator` 是「持有 BitGenerator 的容器」，构造时通过 `capsule` 校验并取出 C 层 `bitgen_t` 结构，从而把底层比特流桥接到各分布方法。
- **seed 是伪随机数列的起点**：`seed=None` 不可复现（每次拉取 OS 熵），固定整数/序列则可复现。
- 可复现性的精确表述是「相同 seed + 相同调用顺序 + 相同 NumPy 版本 ⇒ 相同输出」；注意 `Generator` 声明了 *No Compatibility Guarantee*，跨版本不保证比特流一致。
- 「同 seed 重建」可逐位复现，但「同一个生成器连续抽两次」会得到不同结果，因为内部状态会随每次抽取而推进。

## 7. 下一步学习建议

本讲你已经能跑通第一个可复现的程序。建议接下来：

- **横向扩展用法**：阅读 `u1-l4 Generator 常用方法速览`，系统了解 `random`/`integers`/`standard_normal`/`uniform`/`choice`/`shuffle`/`permutation` 等方法，建立对新 API 的使用直觉。
- **纵向深入架构**：若你想搞懂「`capsule` 里那个 `bitgen_t` 到底是什么、`Generator` 怎么靠它调用 C 采样」，请进入第 2 单元，先读 `u2-l1 bitgen_t：连接 C 与 Cython 的核心结构`。
- **理解种子细节**：若你想知道「`PCG64(42)` 里的 42 是怎么被混合成初始状态的」，请进入第 3 单元 `u3-l1 SeedSequence 的熵混合机制`。

推荐按「`u1-l4` → `u2-l1`」的顺序推进：先用熟 API，再拆开它的底层骨架。
