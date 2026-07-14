# bitgen_t：连接 C 与 Cython 的核心结构

## 1. 本讲目标

学完本讲，你应当能够：

- 准确说出 `bitgen_t` 这个 C 结构里有哪些字段，以及每个字段的作用。
- 理解「`state` 指针 + 四个 `next_*` 函数指针」为什么构成了 BitGenerator 与分布层之间的**契约**。
- 看懂具体生成器（以 PCG64 为例）如何在 `__init__` 里把函数指针表填进 `_bitgen`。
- 解释 `next_double` 与 `next_uint64` 的数学关系（53 位浮点的由来）。
- 说清楚为什么分布层 C 代码（`distributions.c`）**只依赖函数指针，而不依赖任何具体生成器的实现**——这是整个 `numpy.random` 两层架构能够解耦的关键。

## 2. 前置知识

在进入源码前，先用通俗语言把几个底层概念讲清楚。如果你已经熟悉，可以跳过本节。

### 2.1 函数指针：把「行为」当成数据存起来

在 C 语言里，一个普通的函数调用是写死的：`foo()` 永远调用 `foo`。而**函数指针**把「要调用哪个函数」这件事变成了一个变量：

```c
uint64_t (*fp)(void *st);   // fp 是一个函数指针：接收 void*，返回 uint64_t
fp = &pcg64_uint64;          // 现在 fp 指向 pcg64_uint64
fp(st);                      // 通过指针调用，等价于 pcg64_uint64(st)
```

你可以把不同的函数赋给同一个指针变量，调用方写的代码完全不变，但实际执行的函数可以换。这正是面向对象语言里「多态」在 C 里的等价写法。

### 2.2 `void *`：类型擦除的「万能指针」

`void *` 是一个「不指明所指对象类型」的指针。任何类型的指针都能隐式转成 `void *`，反过来则需要强制转换：

```c
void *st = &my_pcg_state;            // 任何指针都能塞进去
pcg64_state *s = (pcg64_state *)st;  // 用的时候再转回真实类型
```

它的价值在于「容器不关心你装的是什么，只要你自己用的时候记得转回来」。`bitgen_t` 用 `void *state` 来存放**任意一种**生成器的内部状态结构，从而做到「一套接口，多种实现」。

### 2.3 Cython 的 `.pxd`：C 头文件的 Python 侧镜像

Cython 是一种「长得像 Python、能直接调 C」的语言。`.pyx` 是实现文件，`.pxd` 是声明文件（类似 C 的 `.h`）。当 Cython 想调用一个已经写好的 C 函数或结构时，需要在 `.pxd` 里用 `cdef extern from "头文件"` 把它**重新声明**一遍，告诉 Cython 编译器「这个名字在某个 C 头文件里存在，签名长这样」。

所以你在本讲会看到：真正的结构定义在 C 头文件 `numpy/random/bitgen.h` 里，而 `bit_generator.pxd` 把它镜像出来供 Cython 使用。

### 2.4 两层架构回顾（承接 u1）

前置讲义 u1 已经建立：`numpy.random` 采用「BitGenerator 只产比特流，Generator 把比特流转成分布」的两层架构。本讲要回答的正是：**这两层之间到底用什么数据结构对接？** 答案就是 `bitgen_t`。

## 3. 本讲源码地图

本讲涉及的关键文件如下，按「从契约声明 → 契约使用 → 契约实现」的顺序排列：

| 文件 | 作用 |
| --- | --- |
| `bit_generator.pxd` | 声明 `bitgen_t` 结构与 `BitGenerator` 类（含 `_bitgen` 成员）。是本讲的核心。 |
| `c_distributions.pxd` | 声明 `npyrandom` 库里所有分布函数的签名，它们全部以 `bitgen_t *` 为首参。 |
| `_pcg64.pyx` | 具体生成器 PCG64 的 Cython 包装，演示如何**填充** `_bitgen` 的函数指针表。 |
| `_common.pxd` | 定义 `uint64_to_double`——`next_double` 与 `next_uint64` 之间的数学桥梁。 |
| `src/distributions/distributions.c` | 分布层 C 实现，演示它**只通过函数指针**消费比特流。 |
| `bit_generator.pyx` / `_common.pyx` | 基类如何把 `_bitgen` 装进 `capsule`、暴露成 `ctypes`/`cffi`。 |

> 说明：真正的 C 结构定义在安装时生成的头文件 `numpy/random/bitgen.h`、`numpy/random/distributions.h` 中（它们由构建系统生成，不在本源码树里）。本讲引用的 `.pxd` 文件已把这些声明逐字镜像出来，是可信的依据。

## 4. 核心概念与源码讲解

### 4.1 bitgen_t 结构：一个极简的「比特流接口」对象

#### 4.1.1 概念说明

`bitgen_t` 是一个**极其精简**的 C 结构，它只做一件事：把「一个生成器的状态」和「从该状态取随机数的方法」打包在一起，对外形成一个统一的接口。

可以这样理解：分布层（写正态分布、泊松分布的人）不需要知道 PCG64、MT19937、Philox 内部是怎么转的。它只需要问生成器一个问题——「给我下一个随机数」。`bitgen_t` 就是这个问题的标准答案容器：

- 我不知道你是谁，但我把你的状态指针放在 `state` 里（`void *`，我不管具体类型）。
- 我也不想知道你的算法，但你得给我几个**函数**，让我能从 `state` 里取出 `uint64`、`uint32`、`double` 这些标准形状的随机数。

在面向对象的术语里，这相当于一个**接口（interface）**或**策略（strategy）**：定义了一组方法签名，把具体实现延迟到子类。在 C 里没有 `interface` 关键字，于是用「函数指针表 + 不透明状态指针」来实现，这是一种经典手法（有时也叫「手写的 vtable」）。

#### 4.1.2 核心流程

从「定义 → 填充 → 消费」三个阶段看 `bitgen_t` 的生命周期：

```text
[1] 定义（C 头文件 + pxd 镜像）
        bitgen_t { void *state; next_uint64; next_uint32; next_double; next_raw }
             │
             ▼
[2] 填充（具体生成器的 __init__，如 PCG64）
        self._bitgen.state      = &自己的状态结构
        self._bitgen.next_uint64 = &自己的取数函数
        ...
             │
             ▼
[3] 消费（分布层 distributions.c）
        收到一个 bitgen_t *bitgen_state
        bitgen_state->next_uint64(bitgen_state->state)  // 取一个 64 位随机数
```

关键点：阶段 [3] 的代码完全不出现 PCG64 / MT19937 这些名字，它只认函数指针。这就是「解耦」。

#### 4.1.3 源码精读

`bitgen_t` 的声明在 [bit_generator.pxd:4-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd#L4-L12)。这段代码把 C 头文件 `numpy/random/bitgen.h` 里的结构镜像出来：

```cython
cdef extern from "numpy/random/bitgen.h":
    struct bitgen:
        void *state
        uint64_t (*next_uint64)(void *st) nogil
        uint32_t (*next_uint32)(void *st) nogil
        double (*next_double)(void *st) nogil
        uint64_t (*next_raw)(void *st) nogil

    ctypedef bitgen bitgen_t
```

逐行解读：

- `struct bitgen:` —— C 里的结构体标签是 `bitgen`。
- `void *state` —— 指向**任意**生成器内部状态的不透明指针（类型擦除）。
- 四个函数指针，签名都是 `(void *st) -> 某种数`，它们共同接收那个 `state` 指针作为参数。`nogil` 表示这些函数可以在释放 GIL（全局解释器锁）的情况下执行，这对高性能批量采样至关重要。
- `ctypedef bitgen bitgen_t` —— 给 `struct bitgen` 起了个短别名 `bitgen_t`。所以 C 里其实写的是 `typedef struct bitgen { ... } bitgen_t;`，全仓库统一用 `bitgen_t` 这个名字。

#### 4.1.4 代码实践

**实践目标**：亲手数清 `bitgen_t` 有几个字段，并理解 `void *state` 的「万能指针」性质。

**操作步骤**（源码阅读型）：

1. 打开 [bit_generator.pxd:4-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd#L4-L12)。
2. 统计 `struct bitgen` 内一共有几个成员（答案应为 5 个：1 个 `state` + 4 个函数指针）。
3. 思考：为什么 `state` 的类型是 `void *` 而不是某个具体结构？

**预期结果**：你会确认 `bitgen_t` 是一个「1 个状态指针 + 4 个取数函数指针」的极简结构，`void *` 让它能容纳任意生成器的状态。

#### 4.1.5 小练习与答案

**练习 1**：`bitgen_t` 里为什么没有直接存放「生成器名字」「周期」之类元信息？

> **答案**：`bitgen_t` 只关心「如何取随机数」这一个职责，刻意保持最小化。元信息（名字、周期、是否可跳跃）由外层的 Python `BitGenerator` 子类（如 `PCG64`）以属性/方法的形式提供，C 层不需要知道。这是「单一职责」的体现。

**练习 2**：`ctypedef bitgen bitgen_t` 这一行如果删掉，会有什么影响？

> **答案**：Cython 代码后续就无法用 `bitgen_t` 这个名字了，只能写全名 `bitgen`。它只是一个类型别名，删除不影响内存布局，只影响代码可读性和全仓库命名一致性。

---

### 4.2 四个 next_* 函数指针：state + 回调表

#### 4.2.1 概念说明

四个函数指针各自提供一种「形状」的随机数：

| 函数指针 | 返回类型 | 含义 |
| --- | --- | --- |
| `next_uint64` | `uint64_t` | 一个完整的 64 位无符号随机整数（原始比特）。 |
| `next_uint32` | `uint32_t` | 一个 32 位无符号随机整数。 |
| `next_double` | `double` | 一个 \([0,1)\) 区间的 53 位精度浮点数。 |
| `next_raw` | `uint64_t` | 「原始」64 位输出，**不经过任何缓冲**，专供 `random_raw()` 等需要逐字对比的场景。 |

注意 `next_double` 和 `next_uint64` 的关系：在大多数生成器里，`next_double` **就是**把 `next_uint64` 的输出做一次位运算压缩得到的。我们以 PCG64 为例，这两者的实现高度对称：

- `pcg64_uint64(st)` 返回 `pcg64_next64(st)`（原始 64 位）。
- `pcg64_double(st)` 返回 `uint64_to_double(pcg64_next64(st))`（压缩成 `[0,1)` 的 double）。

也就是说：`next_double` 建立在 `next_uint64` 之上，中间多了一步 `uint64_to_double` 的位映射。这个映射的数学含义见 4.2.3。

> **关于 `next_raw` 与 `next_uint64` 的区别**：对 PCG64 这类不缓冲的生成器，两者指向同一个函数（见 [4.2.3](#423-源码精读) 源码）。差异体现在**会缓冲**的生成器上：`next_uint32` 可能为了效率把一次 64 位抽取拆成两次 32 位返回，第二次数是从缓冲区取的；而 `next_raw` 永远强制拉取一个全新的完整字，保证逐字可对比。

#### 4.2.2 核心流程

一个具体生成器「填表」的伪代码（以 PCG64 为例）：

```text
PCG64.__init__(seed):
    1. 调用基类 BitGenerator.__init__，初始化 self._bitgen（此时 state=NULL，函数指针未填）
    2. 让自己的状态指针就位：self.rng_state.pcg_state = &self.pcg64_random_state
    3. 把状态指针塞进契约：self._bitgen.state = &self.rng_state
    4. 填四个函数指针：
         next_uint64 = &pcg64_uint64
         next_uint32 = &pcg64_uint32
         next_double = &pcg64_double
         next_raw    = &pcg64_uint64
    5. 用 SeedSequence 生成种子，初始化内部状态
```

填完之后，这个 `_bitgen` 就成了一个「能产出比特流的黑盒」。任何拿到 `bitgen_t *` 的代码都能从中取数，无需知道它是 PCG64。

#### 4.2.3 源码精读

**第一处：PCG64 的四个回调函数。** 在 [_pcg64.pyx:35-42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L35-L42)，可以看到 `next_uint64` 与 `next_double` 的对称实现：

```cython
cdef uint64_t pcg64_uint64(void* st) noexcept nogil:
    return pcg64_next64(<pcg64_state *>st)

cdef uint32_t pcg64_uint32(void *st) noexcept nogil:
    return pcg64_next32(<pcg64_state *> st)

cdef double pcg64_double(void* st) noexcept nogil:
    return uint64_to_double(pcg64_next64(<pcg64_state *>st))
```

注意两点：一是每个回调都把 `void *st` 强转回真实类型 `<pcg64_state *>st`（与 2.2 节的类型擦除呼应）；二是 `pcg64_double` 比 `pcg64_uint64` 多套了一层 `uint64_to_double`。

**第二处：`uint64_to_double`——`next_double` 与 `next_uint64` 的数学桥梁。** 在 [_common.pxd:71-72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pxd#L71-L72)：

```cython
cdef inline double uint64_to_double(uint64_t rnd) noexcept nogil:
    return (rnd >> 11) * (1.0 / 9007199254740992.0)
```

这里的 `9007199254740992 = 2^{53}`。整个表达式的含义是：把 64 位随机数右移 11 位（丢弃低 11 位，保留高 53 位），再除以 \(2^{53}\)，得到一个 \([0,1)\) 区间、具有 53 位精度的 `double`。写成数学形式：

\[
\text{next\_double} = \frac{\lfloor \text{next\_uint64} / 2^{11} \rfloor}{2^{53}} \in [0,\,1)
\]

为什么是 53 位？因为 IEEE 754 的 `double` 尾数正好有 52 位（外加隐含的 1 位共 53 位），再多给位数也没有意义。这就是 `next_double` 建立在 `next_uint64` 之上的精确关系。

**第三处：把函数指针填进 `_bitgen`。** 在 [_pcg64.pyx:126-130](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L126-L130)：

```cython
self._bitgen.state = <void *>&self.rng_state
self._bitgen.next_uint64 = &pcg64_uint64
self._bitgen.next_uint32 = &pcg64_uint32
self._bitgen.next_double = &pcg64_double
self._bitgen.next_raw = &pcg64_uint64
```

这正是 4.2.2 伪代码的真实落地：`state` 指向自己的 `rng_state`，四个函数指针各归各位。注意 `next_raw` 和 `next_uint64` 都指向 `&pcg64_uint64`（PCG64 不做缓冲）。

#### 4.2.4 代码实践

**实践目标**：在运行时观察 `next_double` 与 `next_uint64` 的关系，验证「前者是后者的位压缩」。

**操作步骤**（可运行）：

```python
import numpy as np

bg = np.random.PCG64(0)
# next_uint64 与 next_double 都通过 ctypes 暴露为函数指针
print("next_uint64:", bg.ctypes.next_uint64)
print("next_double:", bg.ctypes.next_double)
print("bit_generator 指针:", bg.ctypes.bit_generator)

# 验证：raw 输出（等价 next_uint64）的范围应该是 [0, 2**64)
raw = bg.random_raw(3)
print("raw 样本:", raw)
print("是否都在 [0, 2**64):", all(0 <= r < 2**64 for r in raw))
```

**需要观察的现象**：

1. `next_uint64` 与 `next_double` 是两个**不同的函数指针**（地址不同），但属于同一个 `interface` 命名元组。
2. `random_raw()` 返回的是原始 64 位整数（很大），而 `Generator(bg).random()` 返回 \([0,1)\) 的小数——后者正是前者经 `uint64_to_double` 映射的结果。

**预期结果**：你会看到 `next_double` 和 `next_uint64` 是两个独立的函数指针，分别产出小数和整数；它们背后共享同一个底层状态推进。

> 若手头环境没有编译好的 numpy，可改用源码阅读型实践：对照 [_pcg64.pyx:35-42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L35-L42) 与 [_common.pxd:71-72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pxd#L71-L72)，手算 `uint64_to_double(0xFFFFFFFFFFFFFFFF >> 11)` 应得到接近 1 的值。

#### 4.2.5 小练习与答案

**练习 1**：把 `uint64_to_double` 里的 `>> 11` 改成 `>> 12`，会引入什么问题？

> **答案**：结果是 `(rnd >> 12) / 2^{53}`，最大值变成约 \(2^{52}/2^{53} = 0.5\)，范围从 \([0,1)\) 缩水成 \([0,0.5]\) 附近，且只用到 52 位熵。正确的做法是移位位数与除数的指数保持一致（移 11 位 → 除以 \(2^{53}\)），这样 \(64 - 11 = 53\) 正好等于 `double` 尾数位数。

**练习 2**：为什么 PCG64 的 `next_raw` 和 `next_uint64` 指向同一个函数？

> **答案**：PCG64 的底层 `pcg64_next64` 每次直接产出一个完整的 64 位字，不做「一次抽取拆成两次返回」的缓冲。因此「带缓冲语义的 `next_uint64`」和「强制拉取的 `next_raw`」在这里没有区别，可以共用同一个实现。会缓冲的生成器（如某些为 32 位优化的实现）才会让两者不同。

---

### 4.3 _bitgen 成员：Cython 对象如何持有 C 结构并对外暴露

#### 4.3.1 概念说明

`bitgen_t` 是一个 C 结构，它本身不能直接被 Python 看到。Python 世界里能看到的是 `BitGenerator` 这个 Cython 类（例如 `PCG64` 实例）。那么一个 Python 对象如何「内部持有一个 C 结构」？

答案就是 `_bitgen` 成员：在 Cython 类里用 `cdef bitgen_t _bitgen` 声明一个**值类型的 C 成员**（不是指针，是结构体本身嵌在对象里）。这样每个 `BitGenerator` 实例的内存里都直接内嵌了一个完整的 `bitgen_t`。

但仅有内部持有还不够——`numpy.random` 的一个设计目标是让**外部 C/Cython/Numba 代码**也能直接调用这些函数指针。于是基类还提供了三套对外暴露方式：

- `capsule`：一个 `PyCapsule`，内部存放 `&self._bitgen` 的地址，名字校验为 `"BitGenerator"`。
- `ctypes`：一个命名元组，把 `state`、各函数指针等以 `ctypes` 句柄形式给出。
- `cffi`：与 `ctypes` 同构，但面向 CFFI 生态。

这三套接口都指向**同一个** `_bitgen`，只是包装方式不同，分别适配不同的宿主语言/扩展方式（u7 单元会详述）。

#### 4.3.2 核心流程

基类 `BitGenerator.__init__` 负责「打底」：

```text
BitGenerator.__init__(seed):
    1. self.lock = RLock()              # 线程锁
    2. self._bitgen.state = <void *>0   # 先把状态指针置空（等子类填）
    3. 若直接实例化基类 → 抛 NotImplementedError
    4. self._ctypes = None; self._cffi = None
    5. self.capsule = PyCapsule_New(&self._bitgen, "BitGenerator", NULL)
    6. 把 seed 包成 SeedSequence 存到 self._seed_seq
```

注意第 2 步：基类只把 `state` 置空，**不填函数指针**。填函数指针是子类（PCG64/MT19937/...）的职责（见 4.2.3）。这就是「基类定契约骨架，子类填具体实现」的分工。

#### 4.3.3 源码精读

**第一处：`_bitgen` 成员的声明。** 在 [bit_generator.pxd:14-20](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd#L14-L20)：

```cython
cdef class BitGenerator():
    cdef readonly object _seed_seq
    cdef readonly object lock
    cdef bitgen_t _bitgen
    cdef readonly object _ctypes
    cdef readonly object _cffi
    cdef readonly object capsule
```

第 17 行 `cdef bitgen_t _bitgen` 就是关键：每个 `BitGenerator` 对象内嵌一个 `bitgen_t`。注意它**没有** `readonly`——因为子类需要在 `__init__` 里写它的函数指针（如 `self._bitgen.next_uint64 = ...`），所以必须是可写的。

**第二处：基类 `__init__` 如何打底并建 capsule。** 在 [bit_generator.pyx:536-549](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L536-L549)：

```cython
def __init__(self, seed=None):
    self.lock = RLock()
    self._bitgen.state = <void *>0
    if type(self) is BitGenerator:
        raise NotImplementedError('BitGenerator is a base class and cannot be instantized')

    self._ctypes = None
    self._cffi = None

    cdef const char *name = "BitGenerator"
    self.capsule = PyCapsule_New(<void *>&self._bitgen, name, NULL)
    if not isinstance(seed, ISeedSequence):
        seed = SeedSequence(seed)
    self._seed_seq = seed
```

要点：`<void *>&self._bitgen` 取的是这个内嵌结构体的地址，塞进 `PyCapsule`。名字 `"BitGenerator"` 用作校验——外部代码取出 capsule 时会用这个名字确认「这确实是一个 bitgen_t 指针」，防止误用。

**第三处：`ctypes` 属性如何把函数指针暴露给 Python。** 在 [bit_generator.pyx:685-704](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L685-L704)（`cffi` 属性在 706–725 行结构相同）：

```cython
@property
def ctypes(self):
    """
    Returns a namedtuple containing ctypes wrapper
        * state_address - Memory address of the state struct
        * state - pointer to the state struct
        * next_uint64 - function pointer to produce 64 bit integers
        * next_uint32 - function pointer to produce 32 bit integers
        * next_double - function pointer to produce doubles
        * bitgen - pointer to the bit generator struct
    """
    if self._ctypes is None:
        self._ctypes = prepare_ctypes(&self._bitgen)
    return self._ctypes
```

这个命名元组的字段在 [_common.pyx:21-23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L21-L23) 定义，与上面的文档字符串一一对应：

```cython
interface = namedtuple('interface', ['state_address', 'state', 'next_uint64',
                                     'next_uint32', 'next_double',
                                     'bit_generator'])
```

而 `prepare_cffi` 真正读出指针并做 cast 的代码在 [_common.pyx:134-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L134-L138)，它把 `bitgen.state`、`bitgen.next_uint64`、`bitgen.next_double` 等逐个转成 CFFI 句柄——本质上就是把 `_bitgen` 里的字段原封不动地递给外部。

#### 4.3.4 代码实践

**实践目标**：确认 `_bitgen` 被三套接口以同一份底层数据暴露出来。

**操作步骤**（可运行）：

```python
import numpy as np

bg = np.random.PCG64(0)

# 1) capsule：PyCapsule，名字为 "BitGenerator"
print("capsule 类型:", type(bg.capsule))
print("capsule 名字:", bg.capsule.__name__)   # 期望: BitGenerator

# 2) ctypes：命名元组，含 state / next_uint64 / next_double / bit_generator
iface = bg.ctypes
print("ctypes 字段:", bg.ctypes._fields)
print("state_address == bit_generator 指向同一结构:", iface.state_address)

# 3) 验证基类不能直接实例化
try:
    np.random.BitGenerator()
except NotImplementedError as e:
    print("基类不可实例化:", e)
```

**需要观察的现象**：

1. `capsule.__name__` 为 `"BitGenerator"`，对应 [bit_generator.pyx:545](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L545) 的校验名。
2. `ctypes._fields` 正是 [_common.pyx:21-23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L21-L23) 定义的那 6 个字段。
3. 直接 `np.random.BitGenerator()` 会抛 `NotImplementedError`，对应 [bit_generator.pyx:539-540](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L539-L540)。

**预期结果**：三套接口都指向同一个内嵌的 `_bitgen`；基类本身不可实例化，必须用具体子类。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_bitgen` 没有声明成 `readonly`，而 `capsule` 是 `readonly`？

> **答案**：`_bitgen` 的函数指针需要在子类 `__init__` 里被写入（见 4.2.3），所以必须可写；而 `capsule` 在基类 `__init__` 里一次性建好就不再改变，对外只读即可。`readonly` 既是约束也是承诺：告诉使用者「这个属性是构建期产物，别去改它」。

**练习 2**：`PyCapsule_New` 的第二个参数 `"BitGenerator"` 有什么用？

> **答案**：它是 capsule 的**名字**，用作类型校验。外部代码（比如 Cython 扩展）从 capsule 取指针时会调用 `PyCapsule_IsValid(capsule, "BitGenerator")`，只有名字匹配才认为里面装的是 `bitgen_t *`，从而避免把一个装着别的类型的 capsule 误当成生成器来用——这是一种轻量的类型安全机制。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个**源码阅读 + 动手验证**的综合任务。

### 任务

对照 `bit_generator.pxd` 画一张 `bitgen_t` 结构图，说明 `next_double` 与 `next_uint64` 的关系，并解释为什么分布层代码只依赖函数指针而不依赖具体生成器实现。

### 步骤

1. **画结构图**。阅读 [bit_generator.pxd:4-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd#L4-L12)，画出如下关系（可用纸笔或文本）：

   ```text
   bitgen_t
   ├── void *state           ── 指向具体生成器状态（如 pcg64_state），类型擦除
   ├── next_uint64(void *st) ── 取一个 64 位随机整数
   ├── next_uint32(void *st) ── 取一个 32 位随机整数
   ├── next_double(void *st) ── 取一个 [0,1) 的 53 位 double
   └── next_raw(void *st)    ── 取一个原始 64 位字（不缓冲）
   ```

2. **说明 `next_double` 与 `next_uint64` 的关系**。结合 [_pcg64.pyx:35-42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L35-L42) 和 [_common.pxd:71-72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pxd#L71-L72)，写出一句话结论：`next_double(st) = uint64_to_double(next_uint64(st))`，即把 64 位整数右移 11 位后除以 \(2^{53}\)，映射到 \([0,1)\)。

3. **解释「只依赖函数指针」**。阅读分布层 [src/distributions/distributions.c:12-17](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/src/distributions/distributions.c#L12-L17) 的内联包装：

   ```c
   static inline uint64_t next_uint64(bitgen_t *bitgen_state) {
     return bitgen_state->next_uint64(bitgen_state->state);
   }
   ```

   以及 [src/distributions/distributions.c:28-30](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/src/distributions/distributions.c#L28-L30) 的 `random_standard_uniform`：

   ```c
   double random_standard_uniform(bitgen_t *bitgen_state) {
       return next_double(bitgen_state);
   }
   ```

   你会发现：`distributions.c` 全程只通过 `bitgen_state->next_uint64(...)` 这样的函数指针调用取数，**完全没有出现 `pcg64` / `mt19937` 等字样**。

4. **动手验证解耦性**（可选运行）：

   ```python
   import numpy as np
   for BG in (np.random.PCG64, np.random.MT19937, np.random.Philox):
       g = np.random.Generator(BG(42))
       # 同一个 standard_normal 接口，背后是不同的 BitGenerator
       print(BG.__name__, g.standard_normal(3))
   ```

   同一段分布代码，换不同 BitGenerator 依然工作——这就是「分布层只依赖函数指针」带来的可替换性。

### 预期结论（一句话）

`bitgen_t` 用 `void *state` + 四个函数指针构成了一份「取数契约」：具体生成器负责填表，分布层只调函数指针，两者因此彻底解耦，生成器可自由替换而不需重写分布算法。

## 6. 本讲小结

- `bitgen_t` 是一个极简 C 结构：1 个 `void *state`（类型擦除的状态指针）+ 4 个函数指针（`next_uint64` / `next_uint32` / `next_double` / `next_raw`），定义在 `bit_generator.pxd` 中镜像自 C 头文件。
- 四个函数指针提供四种「形状」的随机数；`next_double` 建立在 `next_uint64` 之上，关系是 `uint64_to_double(rnd) = (rnd >> 11) / 2^53`，正好利用 `double` 的 53 位尾数。
- 具体生成器（如 PCG64）在 `__init__` 里把 `state` 指向自己的状态结构、把四个回调函数地址填进 `_bitgen`，完成「填表」。
- `_bitgen` 是 `BitGenerator` 类内嵌的值类型 C 成员（可写，非 readonly），基类只置空 `state` 并建好 `capsule`，函数指针交给子类填。
- 对外通过 `capsule`（PyCapsule，名为 `"BitGenerator"`）、`ctypes`、`cffi` 三套接口暴露同一个 `_bitgen`，供 C/Cython/Numba 等外部代码直接调用。
- 分布层 `distributions.c` 全程只通过函数指针取数，不依赖任何具体生成器实现——这是两层架构能够解耦、生成器可替换的根本原因。

## 7. 下一步学习建议

本讲建立了「契约」的静态结构，接下来建议：

1. **u2-l2（BitGenerator 基类）**：深入基类的 `state`/`lock`/`capsule` 抽象，理解 `lock` 如何配合 `nogil` 保证线程安全，以及基类与子类的完整分工。
2. **u2-l3（Generator 包装）**：看 `Generator` 如何拿到 `BitGenerator` 的 `bitgen_t` 指针，并把分布方法桥接到 C 采样函数——这是契约的「消费方」。
3. **u4-l1（distributions.c 与 npyrandom 库）**：从分布层视角回头再看本讲的「只依赖函数指针」结论，理解 `npyrandom` 静态库为何能被任意 BitGenerator 复用。

阅读建议：先把本讲的 `bitgen_t` 结构图贴在手边，再进入 u2-l2/u2-l3，你会发现自己能轻松看懂所有「跨层调用」的代码。
