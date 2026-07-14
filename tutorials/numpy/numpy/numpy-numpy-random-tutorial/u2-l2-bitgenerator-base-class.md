# BitGenerator 基类：状态、锁与 PyCapsule

## 1. 本讲目标

上一讲（u2-l1）我们已经认识了连接 C 与 Cython 的核心结构 `bitgen_t`：1 个 `void *state` 加 4 个函数指针，相当于一份「手写的虚函数表」。那时我们留下了一个问题：**这张表是谁来填的？填表之前的那层公共基础设施又由谁提供？**

本讲就回答这个问题。我们走进 `BitGenerator` **基类**本身，读完它你会掌握：

1. `BitGenerator.__init__` 做了哪些「所有生成器都用得上的公共初始化」，以及它如何把任意 `seed` 统一包装成 `SeedSequence`。
2. 为什么基类不能直接实例化（会抛 `NotImplementedError`），以及「基类定骨架、子类填实现」这条主线。
3. `capsule`、`ctypes`、`cffi` 三套对外接口背后其实是同一份 `_bitgen` 数据，以及 `lock` 如何配合 `with lock, nogil:` 实现「线程安全 + 释放 GIL」。
4. `state` 为什么在基类里是一个「抽象属性」（取/赋值都抛 `NotImplementedError`），它和 pickle 又是什么关系。

学完本讲，你就能看懂任何一个具体生成器（PCG64、MT19937……）的 `__init__` 是怎么「站在基类肩膀上」把 `bitgen_t` 填满的。

## 2. 前置知识

在进入源码前，先用大白话理清几个概念：

- **基类（base class）与子类（subclass）**：`BitGenerator` 是「随机比特生成器」这个概念的抽象，它规定了所有生成器**必须具备**的能力（能被播种、能取原始比特、能被序列化、能被外部 C 代码调用），但它自己**没有具体的随机算法**。`PCG64`、`MT19937` 这些才是有真实算法的子类。
- **抽象方法 / 抽象属性**：基类可以声明「我有一个 `state` 属性」，但不给出实现，而是直接抛 `NotImplementedError`，强制子类去覆盖。这是面向对象里常见的「定契约、不实现」手法。注意 Cython 的 `cdef class` 不能像普通 Python 类那样方便地继承 `abc.ABC`，所以 NumPy 用「运行时检查 + 抛异常」来模拟抽象类。
- **种子（seed）与种子序列（SeedSequence）**：用户传进来的 `seed` 可能是 `None`、一个整数、一个整数列表，甚至已经是一个 `SeedSequence` 对象。为了让所有生成器用统一的播种协议，基类把这些五花八门的输入统一归一化成一个 `SeedSequence`。
- **PyCapsule**：CPython 提供的一种「把 C 指针安全地塞进 Python 对象」的机制。基类用 capsule 把自己的 `_bitgen` 结构地址暴露出去，这样 Cython / C / Numba 代码就能拿到指针、直接调用那些函数指针。
- **RLock（可重入锁）**：Python `threading.RLock` 是「同一个线程可以重复加锁而不会死锁」的锁。基类为每个生成器建一把锁，保证「同一时刻只有一个线程在推进生成器内部状态」，从而可以安全共享。

> 承接 u2-l1：`bitgen_t` 的 4 个函数指针就是「虚函数表」，本讲讲的就是这张表所在的外层对象——`BitGenerator`——是如何构造、如何对外暴露这张表的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bit_generator.pxd](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd) | Cython 声明层。声明 `BitGenerator` 这个 `cdef class` 拥有哪些 C 级成员（`_bitgen`、`lock`、`capsule` 等）。 |
| [bit_generator.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx) | 基类的真正实现。`__init__`、`seed_seq`、`state`、`spawn`、`random_raw`、`ctypes`、`cffi` 全在这里。 |
| [_common.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx) | 公共工具。`random_raw`、`benchmark`、`prepare_ctypes`、`prepare_cffi` 在这里实现，演示了 `with lock, nogil:` 模式。 |
| [_pcg64.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx) | 子类范例。`PCG64` 演示了「调用基类 `__init__` → 填满 `_bitgen` → 覆盖 `state`」的标准三步。 |

本讲的核心源码集中在 `bit_generator.pyx` 第 506–725 行的 `BitGenerator` 类；`_pcg64.pyx` 用来对照「子类如何填空」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `__init__` 与 SeedSequence 包装**：基类构造函数做了哪些公共初始化，以及种子归一化。
- **4.2 capsule 与 lock**：如何对外暴露 `_bitgen`，如何保证多线程/多 Generator 共享安全。
- **4.3 state 抽象属性**：为什么 `state` 在基类里没有实现，子类必须覆盖。

### 4.1 `__init__` 与 SeedSequence 包装

#### 4.1.1 概念说明

一个生成器在被使用前，需要完成两类事情：

1. **公共基础设施**：建一把锁、建好对外暴露的 capsule、把种子归一化。这些和「具体用什么随机算法」无关，所有生成器都一样。
2. **算法相关初始化**：分配自己的状态内存、把函数指针指向自己的算法实现、用种子填好初始状态。这些每个生成器不同。

`BitGenerator` 基类只负责第 1 类，并且**主动拒绝单独被实例化**——因为只有第 1 类、没有第 2 类的生成器根本无法产出比特。这就是「基类定骨架、子类填实现」。

种子归一化是这里的另一条主线。用户的 `seed` 可能是：

- `None`（让 OS 提供熵，不可复现）；
- 一个 `int`（可复现）；
- 一个整数序列；
- 甚至已经是一个 `SeedSequence`（或任何实现了 `generate_state` 的对象）。

为了让下游（具体生成器）不用关心这些差异，基类把它们统一变成一个 `SeedSequence`（或更广义地说，一个 `ISeedSequence`），存进 `self._seed_seq`。下游只需要调用 `self._seed_seq.generate_state(n_words)` 就能拿到一组确定长度的种子字。

#### 4.1.2 核心流程

基类 `__init__` 的执行流程（伪代码）：

```text
BitGenerator.__init__(seed):
    1. self.lock = RLock()                 # 建一把可重入锁
    2. self._bitgen.state = <void *>0      # 状态指针先置空（占位）
    3. if type(self) is BitGenerator:      # 运行时抽象检查
           raise NotImplementedError        # → 拒绝直接实例化基类
    4. self._ctypes = None                 # ctypes 接口惰性构造
       self._cffi   = None                 # cffi   接口惰性构造
    5. self.capsule = PyCapsule_New(&self._bitgen, "BitGenerator", NULL)
    6. if not isinstance(seed, ISeedSequence):
           seed = SeedSequence(seed)        # 种子归一化
       self._seed_seq = seed
```

注意第 3 步用 `type(self) is BitGenerator`（精确相等，不是 `isinstance`），所以**只有直接 new 基类才会被拦下**，子类（`type(self) is PCG64`）能正常通过。

子类的构造流程则是「先调基类，再填空」：

```text
PCG64.__init__(seed):
    1. BitGenerator.__init__(self, seed)   # 拿到 lock / capsule / _seed_seq
    2. self._bitgen.state       = &self.rng_state        # 指向自己的状态
       self._bitgen.next_uint64 = &pcg64_uint64          # 填 4 个函数指针
       self._bitgen.next_uint32 = &pcg64_uint32
       self._bitgen.next_double = &pcg64_double
       self._bitgen.next_raw    = &pcg64_uint64
    3. val = self._seed_seq.generate_state(4, np.uint64) # 用统一协议取种
       pcg64_set_seed(...)                                # 真正播种
```

#### 4.1.3 源码精读

先看声明层，`BitGenerator` 这个 `cdef class` 拥有的 C 级成员（注意 `_bitgen` 是**值类型成员**，直接内嵌在对象里，而不是指针）：

[bit_generator.pxd:L14-L20](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pxd#L14-L20) — 声明 `BitGenerator` 的全部成员：`_seed_seq`（归一化后的种子序列）、`lock`（线程锁）、`_bitgen`（上一讲讲的 `bitgen_t` 结构，值类型内嵌）、`_ctypes`/`_cffi`（惰性构造的对外接口）、`capsule`（PyCapsule）。

接着是基类构造函数本体：

[bit_generator.pyx:L536-L549](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L536-L549) — `BitGenerator.__init__` 的全部内容。逐行含义见上面伪代码。其中 `RLock` 来自 `from threading import RLock`（[bit_generator.pyx:L41](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L41)）。

几个关键细节：

- [bit_generator.pyx:L538](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L538)：`self._bitgen.state = <void *>0` 把状态指针置空，等子类覆盖。若直接用基类，函数指针全是空的，一旦调用就会崩——所以下一步主动抛异常更友好。
- [bit_generator.pyx:L539-L540](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L539-L540)：抽象检查。源码里把「instantiated」拼成了 `instantized`，引用时请保持原样。
- [bit_generator.pyx:L547-L549](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L547-L549)：种子归一化。`isinstance(seed, ISeedSequence)` 不是检查具体的 `SeedSequence` 类，而是检查抽象接口 `ISeedSequence`。

`ISeedSequence` 是一个普通的 Python ABC，只声明了一个抽象方法 `generate_state`：

[bit_generator.pyx:L172-L208](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L172-L208) — `ISeedSequence` 抽象基类，定义了「能被 BitGenerator 当作种子序列」的最小契约：必须实现 `generate_state(n_words, dtype)`。这意味着基类并不绑定具体的 `SeedSequence`，任何实现了该接口的对象（包括 `SeedlessSeedSequence`）都能被接受。

再看子类 `PCG64` 如何站在基类肩膀上把 `_bitgen` 填满：

[_pcg64.pyx:L122-L136](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L122-L136) — `PCG64.__init__`。第 1 行调用 `BitGenerator.__init__(self, seed)` 拿到 `lock`/`capsule`/`_seed_seq`；随后把 `_bitgen.state` 指向自己的 `rng_state`，并把 4 个函数指针指向 PCG64 专属的 `pcg64_*` 回调；最后用 `self._seed_seq.generate_state(4, np.uint64)` 拿到 4 个 64 位字，喂给 `pcg64_set_seed` 完成真正的播种。

这些回调是几个极薄的 `nogil` 包装函数，例如：

[_pcg64.pyx:L35-L42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L35-L42) — `pcg64_uint64`/`pcg64_uint32`/`pcg64_double` 把 `void *st` 转回 `pcg64_state *` 再调用 C 实现；`pcg64_double` 里的 `uint64_to_double` 正是 u2-l1 讲过的「64 位整数 → 53 位 double」映射。

#### 4.1.4 代码实践

**实践目标**：亲手验证「基类不可实例化、子类可以」，并观察种子的归一化。

**操作步骤**：

```python
import numpy as np

# 1) 直接实例化基类 —— 应当抛 NotImplementedError
try:
    np.random.BitGenerator()
except NotImplementedError as e:
    print("基类被拦截：", e)

# 2) 子类可以正常构造
pcg = np.random.PCG64(123)
print("seed_seq 类型：", type(pcg.seed_seq))
print("seed_seq.entropy：", pcg.seed_seq.entropy)

# 3) 观察归一化：传 int 与传 SeedSequence 应得到同一个 _seed_seq
ss = np.random.SeedSequence(123)
pcg2 = np.random.PCG64(ss)
print("直接传 SeedSequence，类型：", type(pcg2.seed_seq))
```

**需要观察的现象**：

- 第 1 步打印出 `BitGenerator is a base class and cannot be instantized`（注意源码拼写）。
- 第 2 步 `type(pcg.seed_seq)` 应为 `SeedSequence` 类，说明整数 `123` 被归一化成了 `SeedSequence`。
- 第 3 步直接传入 `SeedSequence` 时，`_seed_seq` 就是那个对象本身（因为 `isinstance(seed, ISeedSequence)` 为真，不再二次包装）。

**预期结果**：基类实例化被拒；整数种子被包装为 `SeedSequence`；已是 `SeedSequence` 的对象透传。

> 待本地验证：不同 NumPy 版本里 `type(pcg.seed_seq)` 的字符串显示路径可能略有差异（如 `numpy.random.bit_generator.SeedSequence`），但类名应为 `SeedSequence`。

#### 4.1.5 小练习与答案

**练习 1**：为什么基类用 `type(self) is BitGenerator` 而不是 `isinstance(self, BitGenerator)` 来判断？

**答案**：`isinstance` 对子类也返回 `True`，那样所有子类都会被误拦。用精确类型相等 `type(self) is BitGenerator` 只拦截「直接 new 基类」这一种情况，子类不受影响。

**练习 2**：如果有人传一个自定义类、并实现了 `generate_state` 方法，`PCG64(my_obj)` 会怎么处理？

**答案**：只要 `my_obj` 被注册为 `ISeedSequence` 的虚拟子类（或本就是 `SeedSequence`），基类的 `isinstance(seed, ISeedSequence)` 就为真，`my_obj` 会被直接当作 `_seed_seq` 透传，`PCG64` 随后调用 `my_obj.generate_state(...)` 取种。否则会被 `SeedSequence(seed)` 尝试包装，可能抛 `TypeError`。

### 4.2 capsule 与 lock

#### 4.2.1 概念说明

基类提供的两个「横切关注点」是本模块的重点：

1. **如何让外部 C / Cython / Numba 代码够得着比特流？** —— 通过 `capsule`，以及它在 Python 侧的两个「视图」`ctypes`、`cffi`。三者指向**同一份 `_bitgen` 数据**，只是包装形式不同，适配不同的宿主语言。
2. **如何让同一个生成器被多个 Generator、多个线程安全共享？** —— 通过 `lock`。任何要推进生成器状态的代码，都应先持有这把锁。

`capsule` 的本质是一个 `PyCapsule`，里面装着 `&self._bitgen`（也就是对象内嵌那个 `bitgen_t` 值的地址），并带一个名字 `"BitGenerator"`。这个名字是「口令」：消费方在取指针时会用这个名字校验，确保拿到的确实是 `bitgen_t` 而不是别的 capsule（这会在 u7 的 Cython 扩展里看到 `PyCapsule_GetPointer(capsule, "BitGenerator")`）。

`ctypes` 和 `cffi` 则是惰性构造的命名元组（namedtuple），把 `_bitgen` 里的字段拆成 Python 能用的形式：状态地址、状态指针、`next_uint64`/`next_uint32`/`next_double` 三个函数指针、以及 `bitgen` 结构本身的指针。这样纯 Python 侧（ctypes）或 CFFI/Numba 侧也能直接调用这些函数指针。

`lock` 是一把 `RLock`，**在基类 `__init__` 里创建、被所有使用者共享**。它解决的是「同一个 `BitGenerator` 被多个 `Generator` 包裹、或被多线程并发推进」时的数据竞争：生成器的状态转移不是原子操作，若两个线程同时各取一个数，状态可能错乱。`RLock` 选「可重入」是为了允许同一调用链里嵌套加锁而不死锁。

#### 4.2.2 核心流程

取数的标准模式（在 `_common.pyx` 中反复出现）：

```text
with lock, nogil:
    for i in range(n):
        out[i] = bitgen.next_uint64(bitgen.state)
```

这两行 `with` 的顺序很重要，体现了 NumPy 的并发设计：

- `with lock`：先获取这把 `RLock`（获取 Python 锁需要持有 GIL），保证「当前线程独占生成器状态」。
- `nogil`：进入代码块前释放 GIL，让**其他 Python 线程**能继续跑；而由于已经持有 `lock`，本线程对生成器状态的读写是安全的。

效果是：C 层的紧凑循环既不会被 GIL 卡住并发，也不会破坏生成器状态。这就是「线程安全 + 释放 GIL」的标准姿势。

三种对外接口的关系：

| 接口 | 形式 | 适合的消费者 |
| --- | --- | --- |
| `capsule` | `PyCapsule`，装 `&self._bitgen`，名 `"BitGenerator"` | Cython（直接取 `bitgen_t *` 指针） |
| `ctypes` | namedtuple，字段含函数指针的 `CFUNCTYPE` 包装 | Python `ctypes`、Numba |
| `cffi` | namedtuple，字段含 `ffi.cast` 出的指针 | CFFI、Numba |

三者**指向同一份 `_bitgen`**，区别只在于「指针用什么类型系统表达」。

#### 4.2.3 源码精读

基类在 `__init__` 里创建 capsule：

[bit_generator.pyx:L545-L546](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L545-L546) — `PyCapsule_New(<void *>&self._bitgen, name, NULL)`，`name = "BitGenerator"`。注意它装的是**值类型成员 `_bitgen` 的地址**，所以子类后续填写的 4 个函数指针、`state` 指针，capsule 持有方都能立刻看到——同一块内存。

`ctypes` / `cffi` 是惰性的属性，首次访问才构造：

[bit_generator.pyx:L684-L704](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L684-L704) — `ctypes` 属性。`if self._ctypes is None: self._ctypes = prepare_ctypes(&self._bitgen)`，构造好的 namedtuple 缓存到 `self._ctypes`。文档里列出了字段：`state_address`、`state`、`next_uint64`、`next_uint32`、`next_double`、`bitgen`。

[bit_generator.pyx:L706-L725](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L706-L725) — `cffi` 属性，结构与 `ctypes` 对称，调用 `prepare_cffi(&self._bitgen)`。

两个 `prepare_*` 把 `_bitgen` 的字段翻译成对应类型系统，可见它们读的是同一组字段：

[_common.pyx:L142-L177](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L142-L177) — `prepare_ctypes`：把 `bitgen.state`、`bitgen.next_uint64`/`next_uint32`/`next_double`、`bitgen` 本体分别用 `ctypes.c_void_p` / `CFUNCTYPE` 包成 Python 可调用对象，塞进 `interface` 命名元组。

[_common.pyx:L107-L140](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L107-L140) — `prepare_cffi`：做同样的事，但用 `ffi.cast` 表达指针类型，供 CFFI/Numba 使用。

`lock` 配合 `with lock, nogil:` 的取数模式，看 `random_raw` 的实现：

[_common.pyx:L46-L105](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L46-L105) — `random_raw(bitgen, lock, size, output)`。注意函数签名里 `lock` 是**从外部传进来**的（就是基类的 `self.lock`），它不是 `_common` 自己建的——这正体现了「锁属于 BitGenerator、被共享」。其中 [_common.pyx:L102-L104](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L102-L104) 用 `with lock, nogil:` 在释放 GIL 的同时保护状态推进；而 [_common.pyx:L94-L96](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L94-L96) 单个标量取数时只 `with lock`（不 `nogil`，因为要返回 Python 对象，需要 GIL）。

基类把 `random_raw` 暴露成方法，转发时把自己的 `_bitgen` 和 `lock` 一起传下去：

[bit_generator.pyx:L649-L678](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L649-L678) — `BitGenerator.random_raw`，最后一行 `return random_raw(&self._bitgen, self.lock, size, output)`。这就是「锁随生成器走、被所有取数路径共享」的具体体现。

#### 4.2.4 代码实践

**实践目标**：直观看到 `capsule` / `ctypes` / `cffi` 三套接口的存在与字段，并确认它们来自同一个生成器。

**操作步骤**：

```python
import numpy as np

pcg = np.random.PCG64()

# capsule：PyCapsule 对象
print("capsule 类型：", type(pcg.capsule))

# ctypes：命名元组，含函数指针等字段
ic = pcg.ctypes
print("ctypes 类型：", type(ic).__name__)
print("ctypes 字段：", ic._fields)

# cffi：若环境装了 cffi 才可用
try:
    ifc = pcg.cffi
    print("cffi 类型：", type(ifc).__name__)
except ImportError as e:
    print("cffi 不可用：", e)
```

**需要观察的现象**：

- `type(pcg.capsule)` 显示为 `capsule`（即 `<class 'PyCapsule'>`）。
- `pcg.ctypes` 是一个 `namedtuple`（`type(ic).__name__` 通常为 `interface` 或其子类名），字段包含 `state_address`、`state`、`next_uint64`、`next_uint32`、`next_double`、`bitgen`。
- 没装 `cffi` 时，访问 `.cffi` 会抛 `ImportError`（由 `prepare_cffi` 内部 `import cffi` 触发）。

**预期结果**：capsule 是 PyCapsule；ctypes/cffi 是含函数指针字段的命名元组；三者背后是同一个 `_bitgen`。

> 待本地验证：`type(ic).__name__` 的具体字符串取决于 namedtuple 的实际类名，请以本地输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ctypes` / `cffi` 要做成惰性属性（首次访问才构造），而 `capsule` 在 `__init__` 里就建好？

**答案**：capsule 几乎零成本（只是包一个指针），且是 Cython 取指针的标准入口，所以早建；而 ctypes/cffi 需要构造一堆包装对象、甚至触发 `import cffi`/`import ctypes`，多数用户用不到，做成惰性可避免不必要的开销和依赖。

**练习 2**：把 `with lock, nogil:` 的两个 `with` 顺序反过来写（即先 `nogil` 再 `lock`）会有什么问题？

**答案**：`with lock` 需要操作 Python 对象（`RLock`）、必须持有 GIL，若已先进入 `nogil` 段再尝试获取 `RLock` 是非法/不可行的。所以顺序必须是「先在 GIL 下拿锁，再释放 GIL 跑 C 循环」。

### 4.3 state 抽象属性

#### 4.3.1 概念说明

「状态」是生成器的核心：它决定了下一条比特流。但不同生成器的状态表示天差地别——

- MT19937 是 624 个 32 位字 + 一个位置索引；
- PCG64 是两个 128 位整数（`state` 和 `inc`）；
- Philox 是计数器 + 密钥。

基类不可能知道「状态长什么样」，所以它**只声明 `state` 这个属性，却不给实现**——取值和赋值都直接抛 `NotImplementedError`，强迫子类覆盖。这就是用「抛异常的 property」模拟抽象属性。

不过要注意区分两层「状态」：

- **C 层状态**：`_bitgen.state` 这个 `void *` 指针，指向子类自己的状态结构（如 PCG64 的 `pcg64_state`）。这是取数时函数指针实际操作的对象。
- **Python 层 `state` 属性**：一个**人类可读的 dict**，用来 get/set/序列化。基类把这套 dict 接口定义为抽象的，由子类实现「dict ↔ C 结构」的互相转换。

这层 Python dict 接口最大的用处是 **pickle**：`__getstate__` 返回 `(self.state, self._seed_seq)`，`__reduce__` 把重建函数与状态三元组打包。也就是说，序列化完全依赖子类把 `state` 实现好。

#### 4.3.2 核心流程

基类对 `state` 的定义（伪代码）：

```text
@property
def state(self):
    raise NotImplementedError('Not implemented in base BitGenerator')

@state.setter
def state(self, value):
    raise NotImplementedError('Not implemented in base BitGenerator')
```

pickle 流程依赖 `state`：

```text
__getstate__(self):  return (self.state, self._seed_seq)   # 取 dict + 种子
__setstate__(self, x):
    self._seed_seq = x[1]
    self.state     = x[0]                  # 调子类的 setter 写回 C 结构
__reduce__(self):
    return (__bit_generator_ctor, (type(self),), (self.state, self._seed_seq))
```

子类（以 PCG64 为例）覆盖 `state` 的流程：

```text
@property state(self):
    调 pcg64_get_state(...) 把 C 结构导出成 4 个 uint64
    组装成 {'bit_generator':'PCG64', 'state':{'state':..., 'inc':...},
            'has_uint32':..., 'uinteger':...} 返回

@state.setter state(self, value):
    解析 value dict → 4 个 uint64
    调 pcg64_set_state(...) 写回 C 结构
```

#### 4.3.3 源码精读

基类的抽象 `state`（取/赋值都抛异常）：

[bit_generator.pyx:L575-L592](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L575-L592) — `state` 的 getter 与 setter，二者都 `raise NotImplementedError('Not implemented in base BitGenerator')`。这就是「抽象属性」的落点。

`seed_seq` 属性则相反，它在基类里有完整实现，只是把 `_seed_seq` 暴露出来：

[bit_generator.pyx:L594-L608](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L594-L608) — `seed_seq` 属性，返回 `self._seed_seq`（归一化后的种子序列，通常是 `SeedSequence`）。

pickle 三件套，全部依赖子类的 `state`：

[bit_generator.pyx:L552-L553](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L552-L553) — `__getstate__` 返回 `(self.state, self._seed_seq)`。

[bit_generator.pyx:L566-L573](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/bit_generator.pyx#L566-L573) — `__reduce__`，返回 `(重建函数, (type(self),), (self.state, self._seed_seq))` 三元组。重建函数 `__bit_generator_ctor` 在 `_pickle.py` 里（详见 u8-l2）。

子类 PCG64 覆盖 `state` getter，把 C 结构翻译成 dict：

[_pcg64.pyx:L192-L217](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_pcg64.pyx#L192-L217) — `PCG64.state` getter。调 `pcg64_get_state` 导出 4 个 `uint64`（`state.high, state.low, inc.high, inc.low`），再拼成两个 128 位整数，组装成带 `bit_generator` 标签的 dict 返回。注意还带出了 `has_uint32` / `uinteger`——这是「半个 32 位残留」的缓冲（一次 64 位取数可拆成两个 32 位用）。

> 小结——基类与子类的分工，可以用一张表收束：

| 职责 | 基类 `BitGenerator` | 子类（如 `PCG64`） |
| --- | --- | --- |
| `lock` 创建 | ✅ `__init__` 里 `RLock()` | 复用，不重建 |
| `capsule`/`ctypes`/`cffi` | ✅ 全部在基类实现 | 直接继承 |
| 种子归一化 `_seed_seq` | ✅ 包装成 `SeedSequence` | 调用 `generate_state` 取种 |
| `_bitgen.state` 指针 | 置空（占位） | ✅ 指向自己的状态结构 |
| 4 个 `next_*` 函数指针 | 不填 | ✅ 填自己的算法回调 |
| `state` 属性（dict 接口） | 抽象（抛异常） | ✅ 覆盖为 dict↔C 互转 |
| `random_raw` / `spawn` | ✅ 通用实现 | 直接继承 |

#### 4.3.4 代码实践

**实践目标**：取出一个真实生成器的 `state` dict，读懂它的字段，并验证「存取状态可复现」。

**操作步骤**：

```python
import numpy as np

rng = np.random.PCG64(2024)
# 先消耗几个数，让状态推进
print("前 3 个数：", [rng.random() for _ in range(3)])

# 取出 Python 层 state dict
st = rng.state
print("state 类型：", type(st))
print("state 内容：", st)

# 复制状态到另一个生成器
rng_b = np.random.PCG64()
rng_b.state = rng.state          # 用子类实现的 setter 写回

# 两者接下来应产生相同序列
a = [rng.random() for _ in range(3)]
b = [rng_b.random() for _ in range(3)]
print("继续抽样 A：", a)
print("继续抽样 B：", b)
print("是否一致：", a == b)
```

**需要观察的现象**：

- `type(st)` 是 `dict`，键包含 `'bit_generator'`（值为 `'PCG64'`）、`'state'`（含 `state`/`inc` 两个 128 位整数）、`'has_uint32'`、`'uinteger'`。
- 把 `st` 写回另一个 PCG64 后，两个生成器后续抽样的结果**逐值相等**。

**预期结果**：`state` 是带 `bit_generator` 标签的 dict；通过 get/set 状态可完整克隆一个生成器的「未来」。

> 待本地验证：dict 中的 `state`/`inc` 具体数值因种子和已消耗步数而异，请以本地输出为准；但 `a == b` 应为 `True`。

#### 4.3.5 小练习与答案

**练习 1**：既然有 C 层的 `_bitgen.state` 指针，为什么还要再设计一个 Python 层的 `state` dict 属性？

**答案**：C 层指针是给取数函数用的、类型擦除的 `void *`，外部无法直接读懂；Python 层 dict 是**可读、可序列化、可跨实例复制**的表示。pickle、`get_state`/`set_state` 式的调试、跨进程传递状态，都依赖这层人类可读的 dict。

**练习 2**：如果某个子类忘记覆盖 `state` getter，调用 `rng.state` 会怎样？`pickle.dumps(rng)` 又会怎样？

**答案**：`rng.state` 直接抛 `NotImplementedError('Not implemented in base BitGenerator')`；而 `pickle.dumps` 会先走 `__reduce__`，它在组装状态元组时调用 `self.state`，同样抛 `NotImplementedError`。所以「不覆盖 `state`」等于「不可序列化」。

## 5. 综合实践

把本讲三个模块串起来，完成下面的综合任务（即本讲指定的实践任务）。

**任务**：实例化 `PCG64`，打印 `seed_seq`、`state`、`capsule` 的类型，并解释为什么基类 `BitGenerator` 不能直接实例化。

```python
import numpy as np

# (A) 基类不可实例化
try:
    np.random.BitGenerator()
except NotImplementedError as e:
    print("A. 基类拦截：", e)

# (B) 实例化子类，观察三件「公共基础设施」
pcg = np.random.PCG64(42)

print("B1. seed_seq 类型：", type(pcg.seed_seq))        # 期望：SeedSequence
print("B2. seed_seq.entropy：", pcg.seed_seq.entropy)
print("B3. state 类型：", type(pcg.state))              # 期望：dict
print("B4. state['bit_generator']：", pcg.state['bit_generator'])
print("B5. capsule 类型：", type(pcg.capsule))          # 期望：PyCapsule (capsule)

# (C) 验证「基类负责公共部分、子类负责填空」
gen = np.random.default_rng(pcg)                        # 用 PCG64 包一个 Generator
print("C. Generator 内部的 bit_generator：", type(gen.bit_generator).__name__)
```

**操作步骤**：

1. 运行上面脚本。
2. 对照 4.1.3 解释 (A) 为何被拦截：因为 `__init__` 里 `type(self) is BitGenerator` 检查命中，且基类没有随机算法、函数指针为空。
3. 对照 4.1 与 4.3 解释 (B1)–(B4)：`seed_seq` 是基类把整数 `42` 归一化出的 `SeedSequence`；`state` 是子类 PCG64 覆盖后返回的 dict（基类版本会抛异常）。
4. 对照 4.2 解释 (B5)：`capsule` 是基类在 `__init__` 里用 `PyCapsule_New` 建好的、装着 `&self._bitgen` 的 PyCapsule。
5. (C) 展示「Generator 持有一个 BitGenerator」（承上启下到 u2-l3）。

**需要观察的现象与预期结果**：

- (A) 打印 `BitGenerator is a base class and cannot be instantized`。
- (B1) 类名为 `SeedSequence`；(B3) 为 `dict` 且 `bit_generator` 字段为 `'PCG64'`；(B5) 为 `capsule`/`PyCapsule`。
- (C) `type(gen.bit_generator).__name__` 为 `PCG64`，说明同一个 `pcg` 被复用进 Generator（共享同一把 `lock` 与同一个 `_bitgen`）。

**一句话解释为何基类不可实例化**：`BitGenerator` 只搭建了锁、capsule、种子归一化这些与算法无关的公共骨架，并把 `_bitgen.state` 置空、四个函数指针留白；它没有任何随机算法，于是在 `__init__` 里用 `type(self) is BitGenerator` 主动抛 `NotImplementedError`，强制用户去实例化具体的子类（如 `PCG64`），由子类把 `_bitgen` 填满并覆盖抽象的 `state` 属性。

> 待本地验证：各对象类型的字符串显示以本地 Python/NumPy 版本为准。

## 6. 本讲小结

- `BitGenerator` 基类只做**与算法无关的公共初始化**：建 `lock`、建 `capsule`、把 `seed` 归一化成 `_seed_seq`，并把 `_bitgen.state` 置空。
- 基类用 `type(self) is BitGenerator` 的运行时检查**拒绝直接实例化**，模拟「抽象类」；子类不受影响。
- 种子归一化面向抽象接口 `ISeedSequence`：是 `SeedSequence` 就透传，否则用 `SeedSequence(seed)` 包装，使下游统一通过 `generate_state` 取种。
- `capsule`/`ctypes`/`cffi` 三套对外接口**指向同一份 `_bitgen`**，分别适配 Cython / ctypes / CFFI-Numba；`ctypes`、`cffi` 是惰性构造。
- `lock` 是基类创建、被所有取数路径共享的 `RLock`；`with lock, nogil:` 是「线程安全 + 释放 GIL」的标准取数姿势。
- `state` 在基类是**抽象属性**（取/赋值都抛 `NotImplementedError`），子类必须覆盖为 dict↔C 互转；pickle 的 `__getstate__`/`__reduce__` 完全依赖它。

## 7. 下一步学习建议

- 下一讲 **u2-l3「Generator：把比特流封装成分布 API」**：看 `Generator` 如何持有一个 `BitGenerator`、通过其 `capsule`/`_bitgen` 调用 C 分布函数，以及 `bit_generator` 属性与 `spawn`。
- 想深入种子归一化的算法细节，进入 **u3 单元（SeedSequence）**：`mix_entropy`、`generate_state` 的扩散、`spawn` 独立流。
- 想看「capsule + lock + nogil」在真实扩展里怎么用，直接跳到 **u7-l2「用 Cython 扩展：自定义采样」**，那里会 `PyCapsule_GetPointer(capsule, "BitGenerator")` 取指针并自行取数。
- 想理解 pickle 的另一半（重建函数），看 **u8-l2「pickle 与 _pickle.py 构造器」**：`__bit_generator_ctor` 如何按 `type(self)` 重建实例并恢复 `state`。
