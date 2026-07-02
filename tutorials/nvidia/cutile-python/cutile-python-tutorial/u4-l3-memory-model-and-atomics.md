# 内存模型与原子操作

## 1. 本讲目标

本讲聚焦「跨 block 共享数据」这一最容易出错的话题。学完后你应当能够：

- 理解 cuTile 的内存模型为什么允许编译器/硬件**重排**跨 block 的访存操作，以及为什么「没有显式同步就不能假设顺序」。
- 准确说出 `MemoryOrder`（`WEAK/RELAXED/ACQUIRE/RELEASE/ACQ_REL`）与 `MemoryScope`（`NONE/BLOCK/CLUSTER/DEVICE/SYS`）每一项的语义，并能组合出正确的排序契约。
- 掌握 `ct.atomic_add / atomic_max / atomic_cas / ...` 这一族原子读-改-写（RMW）与比较交换（CAS）操作的使用方式，理解它们「逐元素原子、整体非原子」的特性。
- 理解内存栅栏（fence）的概念，并知道它在当前 Python API 中以何种形式存在。

本讲对应的代码实践是：**用 `atomic_add` 实现一个直方图（histogram）内核**——这是「多对一写入必须原子」最经典的场景。

## 2. 前置知识

在进入内存模型之前，先回顾几个前置结论（来自 u2-l1、u3-l1）：

- cuTile 只表达 **block 级并行**：一个 kernel 由 grid 中的若干 block 并行执行，每个 block 把 kernel 函数体完整跑一遍。block 内部不暴露单个线程，集体运算的边界已经隐式同步。
- 关键的同步规则是：**block 内禁止显式同步（已隐式同步），block 之间允许同步**。当多个 block 要共享同一块全局显存时，问题才真正出现。
- 内核里写回全局数组用 `ct.store`（u3-l1），它是普通（非原子）写。

**为什么需要内存模型？** GPU 为了性能，会让编译器和硬件**重排（reorder）**访存指令：一个 block 先写的值，另一个 block 不一定「按代码顺序」读到。文档里这句话很关键：

> cuTile's memory model permits the compiler and hardware to reorder operations for performance. Without explicit synchronization, the ordering of memory accesses across blocks is not guaranteed.
>
> —— [docs/source/memory_model.rst:10-12](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst#L10-L12)（说明无显式同步时跨 block 顺序不保证）

于是 cuTile 提供两个「旋钮」来让你显式建立排序契约：**Memory Order（内存序）**——定义一次操作给出什么样的排序保证；**Memory Scope（内存范围）**——定义哪些 block 参与这套排序。这两个旋钮既适用于原子操作，也以受限形式适用于 `load`/`store`。本讲就围绕它们展开。

补充一个易被忽略但很重要的粒度规则：

> Synchronization operates at per-element granularity: each element in the array participates independently in the memory model.
>
> —— [docs/source/memory_model.rst:20-21](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst#L20-L21)（同步以「单个数组元素」为粒度，元素之间互不影响）

意思是：内存模型的排序保证是**逐元素**生效的，对元素 A 建立的同步关系不会自动延伸到元素 B。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/source/memory_model.rst](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst) | 内存模型的官方说明：跨 block 可重排、MemoryOrder/MemoryScope 的定位、逐元素粒度。 |
| [src/cuda/tile/_memory_model.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_memory_model.py) | `MemoryOrder`、`MemoryScope`（以及用于指针类型的 `MemorySpace`）三个枚举的定义与逐项文档。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py) | 模块级原子函数 `atomic_add / atomic_max / atomic_cas / ...`，以及 `RawArrayMemory` 上的 `_offset` 系列原子方法；`load`/`store` 的 `memory_order`/`memory_scope` 参数说明。 |
| [src/cuda/tile/__init__.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/__init__.py) | 把 `MemoryOrder`/`MemoryScope` 与八个 `atomic_*` 再导出为 `cuda.tile` 的公共 API。 |
| [test/test_atomic.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py) | 原子操作的权威行为参照：使用模式、order/scope 的合法组合、以及各种非法组合抛出的错误。 |
| [src/cuda/tile/_bytecode/encodings.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_bytecode/encodings.py) | 后端字节码编码器；其中存在 `MemoryFenceAliasTkoOp` 的编码，是「Tile IR 层确有内存栅栏」的线索。 |

## 4. 核心概念与源码讲解

### 4.1 MemoryOrder：一次操作给出什么排序保证

#### 4.1.1 概念说明

`MemoryOrder` 回答的问题是：**这一次内存操作，会限制编译器/硬件如何重排它前后的访存吗？** 它是建立「甲 block 的写对乙 block 可见」这种契约的核心工具。cuTile 沿用了 C++/CUDA 内存模型里成熟的 acquire/release 词汇表，因此先把这套直觉讲清楚：

- **WEAK（弱序）**：这是普通 `load`/`store` 的默认序，本质上是**非原子**。它不参与任何排序契约，编译器可以自由重排。原子操作**不允许**使用 WEAK。
- **RELAXED（宽松）**：操作本身是原子的（不会读到「半个值」），但**不提供任何排序保证**，也不能用来在线程/block 之间同步——只在你只关心「最终值正确」、不在乎顺序时用，例如计数器自增。
- **ACQUIRE（获取）**：一次 acquire 读，如果读到了某次 release 写的值，那么**那次 release 之前的所有写，对当前 block 都变得可见**；并且本 block 在 acquire **之后**的读写，不能被重排到 acquire 之前。
- **RELEASE（释放）**：一次 release 写，会让**本 block 在 release 之前的所有写**，被随后的某个 acquire 读到；本 block 在 release **之前**的读写，不能被重排到 release 之后。
- **ACQ_REL（获取-释放）**：同时具备 acquire 与 release 语义，是**读-改-写（RMW）类原子操作的默认序**。

一个有用的口诀：**acquire 是「读到对方 release 的值后，开启可见性闸门」；release 是「把自己之前的写打包好，等对方来 acquire」。**

#### 4.1.2 核心流程

设想两个 block 协作的典型模式（生产者写 `data` 与 `flag`，消费者读）：

```
producer block:                consumer block:
  store(data, value)   # 普通写    old = load(flag, order=ACQUIRE)
  atomic_xchg(flag, 1,            use(data)        # 现在能安全读到 value
     memory_order=RELEASE)
```

1. 生产者先把 `value` 写进 `data`，再用 `RELEASE` 序写 `flag`。RELEASE 保证了「`data` 的写」不会被重排到「`flag` 的写」之后。
2. 消费者用 `ACQUIRE` 序读 `flag`；一旦读到生产者写入的值，ACQUIRE 保证「读 `data`」不会被重排到「读 `flag`」之前。
3. 两侧的「不能重排」约束合起来，就保证了消费者 `use(data)` 时一定能看到 `value`。这就是 acquire/release 配对建立可见性的机制。

注意这里的 **scope** 还没指定——它决定「这套契约在多大范围内生效」，见 4.2。

#### 4.1.3 源码精读

`MemoryOrder` 是一个普通 `Enum`，每项的 docstring 即权威定义：

```python
class MemoryOrder(Enum):
    """Memory ordering semantics of a memory operation."""
    WEAK = "weak"        # load/store 默认，非原子
    RELAXED = "relaxed"  # 原子但无排序保证，不能用来同步
    ACQUIRE = "acquire"  # 读取 release 写入的值后，对方先前的写对本 block 可见
    RELEASE = "release"  # 本 block 先前的写，可被随后的 acquire 看到
    ACQ_REL = "acq_rel"  # 同时具备 acquire 与 release
```

完整定义见 [src/cuda/tile/_memory_model.py:30-52](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_memory_model.py#L30-L52)（定义五个枚举值及其语义注释）。

`MemoryOrder` 同时也是 `load`/`store` 的可选参数，但合法取值更窄：`load` 只接受 `WEAK / RELAXED / ACQUIRE`，`store` 只接受 `WEAK / RELAXED / RELEASE`——读端给 acquire、写端给 release，正好凑成一对：

```
memory_order (MemoryOrder): Memory ordering semantics for the load.
    Defaults to ``MemoryOrder.WEAK``. Valid values: ``WEAK``, ``RELAXED``, ``ACQUIRE``.
```

> —— [src/cuda/tile/_stub.py:1274-1275](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1274-L1275)（`load` 的 memory_order 合法取值）；store 的对应说明见 [src/cuda/tile/_stub.py:1410-1411](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1410-L1411)（`store` 的 memory_order 合法取值为 `WEAK/RELAXED/RELEASE`）。

`MemoryOrder` 在 `__init__.py` 被再导出为公共 API：

> [src/cuda/tile/__init__.py:11-14](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/__init__.py#L11-L14)（从 `_memory_model` 导入 `MemoryOrder`、`MemoryScope`）；并在 `__all__` 中声明为公共契约 [src/cuda/tile/__init__.py:183-184](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/__init__.py#L183-L184)。

#### 4.1.4 代码实践

**实践目标**：直观感受 acquire/release 配对如何保证可见性（源码阅读型 + 可选运行）。

1. 阅读上面的「生产者/消费者」伪代码，确认你能在 `ct.store` 与 `ct.atomic_xchg(..., memory_order=ct.MemoryOrder.RELEASE)`、以及 `ct.load(..., memory_order=ct.MemoryOrder.ACQUIRE)` 之间指出「哪些写不能被重排到哪之后」。
2. （可选）写一个最小内核：用一个 1 元素 `flag` 数组（`int32`）和一个 `data` 数组，生产者 block 用 `ct.store` 写 `data` 后用 `ct.atomic_xchg(flag, ..., memory_order=RELEASE)` 发信号，消费者 block 用 `ct.load(flag, memory_order=ACQUIRE)` 轮询。

**需要观察的现象**：当两端都保持 acquire/release 时，消费者读到信号后一定能读到生产者写入的 `data`；若把任一端降级为 `RELAXED`，理论上就可能读到旧 `data`（但单 block、单次运行未必复现，因为重排是「允许」而非「必然」）。

**预期结果**：保持 ACQUIRE/RELEASE 配对时结果稳定正确；降级为 RELAXED 后正确性变为「不保证」。**若你无法在本地 GPU 上稳定复现重排导致的错误，请记为「待本地验证」**——这正是内存模型 bug 难以调试的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么原子 RMW 操作（如 `atomic_add`）的默认序是 `ACQ_REL` 而不是 `ACQUIRE` 或 `RELEASE`？
> **答案**：RMW 既读又写。`ACQUIRE` 只约束「之后的访存不能上移」，`RELEASE` 只约束「之前的访存不能下移」；`ACQ_REL` 同时具备两者，能在「读旧值 + 写新值」这一步同时充当释放点与获取点，因此是 RMW 最自然、最安全的默认。

**练习 2**：把 `ct.store(flag, ..., memory_order=ct.MemoryOrder.ACQUIRE)` 写进内核会发生什么？
> **答案**：编译期类型错误。`store` 的合法序只有 `WEAK/RELAXED/RELEASE`（见 4.1.3 引用），`ACQUIRE` 是「读端」语义，不能用于写。

---

### 4.2 MemoryScope：这套契约在多大范围内生效

#### 4.2.1 概念说明

`MemoryScope` 回答的问题是：**这次排序契约，对哪些 block 生效？** 内存序定义「什么样的保证」，内存范围定义「保证给谁看」。范围越小，硬件同步成本越低；只有在真正需要的范围内建立排序，才能拿到性能。

五档范围（从小到大）：

- **NONE**：无范围。仅用于 `WEAK` 序的 `load`/`store`（即不参与排序）。
- **BLOCK**：保证仅在同 block 内生效。
- **CLUSTER**：保证在同一线程块簇（thread-block cluster）内生效。**注意：这是 `cuda.lang` 专属**，`cuda.tile` 通常用不到。
- **DEVICE**：保证在同一 GPU 上所有线程间生效。**这是原子操作的默认范围**。
- **SYS**：保证跨整个系统生效，包括多 GPU 与 host CPU。

#### 4.2.2 核心流程

选择 scope 的决策树：

```
需要同步的范围是什么？
├─ 仅本 block 内部排序          → BLOCK（最便宜）
├─ 本 GPU 上的所有 block        → DEVICE（原子默认，绝大多数场景）
├─ 多 GPU / GPU 与 CPU          → SYS（最昂贵，慎用）
└─ （cuda.lang 簇内）           → CLUSTER
```

实战要点：

1. 原子操作默认 `MemoryScope.DEVICE`，因此**跨 block 的直方图、归约尾段、锁等都开箱即用**。
2. `memory_scope` **仅在 `memory_order` 不是 `WEAK` 时才有意义**（[src/cuda/tile/_stub.py:1276-1277](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1276-L1277)）。
3. 原子操作若给了非 `WEAK` 的序却把 scope 设成 `NONE`，会在**编译期报错**（见 4.2.4 的实践）。

#### 4.2.3 源码精读

```python
class MemoryScope(Enum):
    """The scope of threads that participate in memory ordering."""
    NONE = "none"      # 仅用于 WEAK 序的 load/store
    BLOCK = "block"    # 同 block 内
    CLUSTER = "cluster"  # 同簇内，cuda.lang only
    DEVICE = "device"  # 同 GPU 所有线程
    SYS = "sys"        # 全系统，含多 GPU 与 host
```

> 完整定义见 [src/cuda/tile/_memory_model.py:8-27](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_memory_model.py#L8-L27)（定义五档范围与逐项注释，其中 `CLUSTER` 标注为 `cuda.lang only`）。

scope 在下沉到 Tile IR 字节码时会被翻译成具体符号，测试里给出了明确映射：

```python
ct_scope_to_tileir_scope = {
    ct.MemoryScope.BLOCK: "tl_blk",
    ct.MemoryScope.DEVICE: "device",
    ct.MemoryScope.SYS: "sys",
}
```

> —— [test/test_atomic.py:270-274](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L270-L274)（cuTile 的 MemoryScope 到 Tile IR 字节码 scope 符号的映射）。

合法性约束由前端在编译期检查，下面三条来自测试的断言是权威规则：

> - 原子 CAS 用 `WEAK` 序 → `TileTypeError: Invalid memory order for tile_atomic_cas`，见 [test/test_atomic.py:379-393](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L379-L393)。
> - 原子 RMW 用 `WEAK` 序 → `Invalid memory order for tile_atomic_rmw`，见 [test/test_atomic.py:396-410](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L396-L410)。
> - 非 `WEAK` 序但 scope 为 `NONE` → `tile_atomic_rmw with (.+) memory ordering requires a memory scope`，见 [test/test_atomic.py:412-439](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L412-L439)（CAS 同理，见 [test/test_atomic.py:441-468](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L441-L468)）。

（说明：上面三条都是把 order 与 scope 的非法组合拦在编译阶段，从而避免运行时的静默错误。）

#### 4.2.4 代码实践

**实践目标**：验证 scope 合法性约束（阅读测试 + 复现编译期错误）。

1. 阅读 [test/test_atomic.py:298-342](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L298-L342) 的 `test_atomic_order_scope`：它枚举 `order × scope` 组合，用 `get_bytecode` 取字节码，再 `filecheck "// CHECK: atomic_rmw_tko <order> <scope>"` 确认 order/scope 确实下沉到了 Tile IR。
2. 运行下面的非法组合（任选其一）观察编译期报错：
   ```python
   @ct.kernel
   def k(x):
       ct.atomic_add(x, 0, 0, memory_order=ct.MemoryOrder.WEAK)        # 触发 "Invalid memory order"
   # 或
   @ct.kernel
   def k2(x):
       ct.atomic_add(x, 0, 0, memory_order=ct.MemoryOrder.ACQ_REL,
                     memory_scope=ct.MemoryScope.NONE)                  # 触发 "requires a memory scope"
   ```

**需要观察的现象**：`ct.launch` 时（而非 GPU 执行时）即抛出 `TileTypeError`，错误信息与上面引用的断言完全一致。

**预期结果**：两类非法组合都在 `launch` 阶段被前端拒绝；合法组合则能正常生成含 `atomic_rmw_tko` 的字节码。**若你未本地配置 tileiras，至少应在 `launch` 的编译阶段看到错误**。

#### 4.2.5 小练习与答案

**练习 1**：一个只在「同一个 block 内部」生效的原子计数器，scope 该设什么以拿到最好性能？
> **答案**：`MemoryScope.BLOCK`。范围越小硬件屏障越便宜；既然不会跨 block 被看见，就没有理由为 `DEVICE` 付出代价。

**练习 2**：`MemoryScope.NONE` 何时合法？
> **答案**：仅在搭配 `MemoryOrder.WEAK` 的普通 `load`/`store` 时合法——也就是「不参与任何排序」。把它用在带序的原子操作上会编译失败。

---

### 4.3 原子操作族 atomic_* 与 RawArrayMemory

#### 4.3.1 概念说明

当多个 block（或同一 block 内的多个元素）要**写同一份存储**时，普通 `ct.store` 会产生数据竞争：两个写同时落到同一地址，结果不可预测。原子操作（atomic）保证「读-改-写」这一串动作对**单个元素**是不可分割的，从而让并发更新有定义良好的结果。

cuTile 的原子族分为两组：

- **模块级函数** `ct.atomic_add / atomic_max / atomic_min / atomic_and / atomic_or / atomic_xor / atomic_xchg`，以及比较交换 `ct.atomic_cas`。它们以「数组 + 索引」为输入，遵循与 `ct.gather`/`ct.scatter`（u4-l2）相同的索引约定。
- **RawArrayMemory 方法** `array.get_raw_memory().atomic_*_offset(...)`，以「裸基址指针 + 元素偏移」为输入，适合你手上已经有线性偏移、不想再做 shape/stride 计算的场景。

两条**贯穿全族的性质**务必牢记：

1. **逐元素原子，整体非原子**：每个被寻址的元素独立地、原子地完成 RMW；但整批操作不是一次原子事务，**各元素写入之间的顺序未定义**（[src/cuda/tile/_stub.py:1745-1748](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1745-L1748)）。
2. **返回旧值**：所有 RMW/CAS 操作都返回「操作之前」的旧值（post-increment 语义）。

#### 4.3.2 核心流程

以直方图最常用的 `atomic_add` 为例，伪代码语义是：

```
in parallel, for each index in (broadcast shape of indices):
    old = array[index]            # 读
    array[index] = old + update  # 改 + 写  （以上三步对单个元素原子）
    result[index] = old          # 返回旧值
```

关键参数与约束：

- `indices`：长度等于数组秩的**整数 tile/标量元组**，各分量按 NumPy 规则广播；1D 数组可省略元组直接传单个 tile（与 gather/scatter 完全一致）。
- `update`：标量或 tile，形状须能广播到 `indices` 的公共形状。
- `check_bounds`（默认 `True`）：越界索引**不执行操作**并返回一个实现定义值；设 `False` 关闭边界检查更快，但越界是**未定义行为**。
- `memory_order`（默认 `ACQ_REL`）/ `memory_scope`（默认 `DEVICE`）：见 4.1、4.2。
- CAS 特有：`expected`（比较值）与 `desired`（期望写入值）；仅当当前值 `== expected` 才写入 `desired`，无论是否写入都返回旧值。
- 类型规则（来自测试）：算术类 `add/max/min/xchg` 允许 `update` 向数组 dtype **隐式转换**（若类型可安全转换）；**位运算类 `and/or/xor` 要求 `update` 的 dtype 与目标 dtype 完全一致**，且不支持 float32/float64 数组（见 [test/test_atomic.py:147-193](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L147-L193)）。

#### 4.3.3 源码精读

八个模块级原子函数集中定义在 `_stub.py` 的 `# =========== Atomic ============` 段。`atomic_cas` 的签名（最特殊的一个）：

```python
@stub
def atomic_cas(array, indices, expected, desired, /, *,
               check_bounds=True,
               memory_order=MemoryOrder.ACQ_REL,
               memory_scope=MemoryScope.DEVICE) -> Tile:
    """Bulk atomic compare-and-swap on array elements with given indices."""
```

> —— [src/cuda/tile/_stub.py:1669-1673](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1669-L1673)（`atomic_cas` 签名与默认 order/scope）。其详细语义（含 bounds 行为与二维索引示例）见 [src/cuda/tile/_stub.py:1674-1740](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1674-L1740)。

七个 RMW 操作共享同一套 docstring 模板 `_doc_atomic_rmw_op`，签名形如：

```python
@stub
@_doc_atomic_rmw_op
def atomic_add(array, indices, update, /, *,
               check_bounds=True,
               memory_order=MemoryOrder.ACQ_REL,
               memory_scope=MemoryScope.DEVICE) -> Tile:
    """Bulk atomic post-increment of array elements at given indices."""
```

> —— [src/cuda/tile/_stub.py:1803-1814](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1803-L1814)（`atomic_add` 签名）。其余六个（`xchg/max/min/and/or/xor`）结构相同，见 [src/cuda/tile/_stub.py:1789-1889](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1789-L1889)。共享模板里关于「逐元素原子、整体非原子、写入顺序未定义」的表述见 [src/cuda/tile/_stub.py:1745-1748](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1745-L1748)。

`RawArrayMemory` 上有平行的 `_offset` 系列，例如：

```python
@stub
def atomic_add_offset(self, offset, update, /, *,
                      mask=None,
                      memory_order=MemoryOrder.ACQ_REL,
                      memory_scope=MemoryScope.DEVICE):
    """Bulk atomic post-increment on raw array memory at base_ptr + offset."""
```

> —— [src/cuda/tile/_stub.py:459-469](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L459-L469)（裸内存版的 `atomic_add_offset`，多了一个 `mask` 参数）。CAS 裸内存版见 [src/cuda/tile/_stub.py:395-446](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L395-L446)。

测试给出了「array 模式」与「raw memory 模式」等价的对照写法，是理解二者关系的最佳材料：

```python
@ct.kernel
def atomic_arith_kernel(x, y, z, TILE, op_id, test_raw_memory):
    bid = ct.bid(0)
    offset = ct.arange(TILE, dtype=ct.int64)
    offset += bid * TILE
    val = ct.gather(y, offset)
    if not test_raw_memory:
        func = ct.static_eval(_op_to_func[AtomicOp(op_id)])
        old_val = func(x, offset, val,
                       memory_order=ct.MemoryOrder.ACQ_REL,
                       memory_scope=ct.MemoryScope.DEVICE)
    else:
        get_func = ct.static_eval(_op_to_raw_memory_func[AtomicOp(op_id)])
        func = get_func(x)
        old_val = func(offset, val,
                       memory_order=ct.MemoryOrder.ACQ_REL,
                       memory_scope=ct.MemoryScope.DEVICE)
    ct.scatter(z, offset, old_val)
```

> —— [test/test_atomic.py:56-74](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L56-L74)（说明：两种 API 在同一 kernel 里分支切换，证明它们语义平行；`_op_to_func` 把枚举映射到 `ct.atomic_*`，`_op_to_raw_memory_func` 映射到 `get_raw_memory().atomic_*_offset`，映射表见 [test/test_atomic.py:25-53](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L25-L53)）。

一个带 `mask` 的 raw-memory 原子加，本质就是「带条件的直方图累加」：

```python
@ct.kernel
def offset_atomic_add_with_mask(x, update, TILE):
    bid = ct.bid(0)
    offset = ct.arange(TILE, dtype=ct.int64)
    offset += bid * TILE
    val = ct.gather(update, offset)
    mem_x = x.get_raw_memory()
    mask = (offset % 2) == 0
    mem_x.atomic_add_offset(offset, val, mask=mask)
```

> —— [test/test_atomic.py:471-479](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L471-L479)（说明：只对偶数偏移做原子加，奇数偏移被 `mask` 跳过；这正是直方图「按条件计数」的雏形）。该测试断言「偶数位被加、奇数位不变」见 [test/test_atomic.py:482-492](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L482-L492)。

#### 4.3.4 代码实践

**实践目标**：用最小内核验证 `atomic_add` 的「多对一并发累加」语义。

1. 构造一个长度为 1 的 `int32` 数组 `x = torch.zeros(1, dtype=torch.int32, device='cuda')`。
2. 用 grid `(4,)` 启动一个内核：每个 block 都对 `x[0]` 执行 `ct.atomic_add(x, 0, ct.full((), 1, dtype=ct.int32))`。
3. 启动后把 `x` 拷回 host 打印。

**需要观察的现象**：尽管 4 个 block 同时向同一地址写，最终 `x[0]` 精确等于 4。

**预期结果**：`x[0] == 4`。把 `atomic_add` 换成普通 `ct.store(x, 0, tile)` 对比，结果会丢更新（通常为 1）。**若本地无 GPU，请记为「待本地验证」，并可转而阅读 [test/test_atomic.py:108-144](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L108-L144) 的 `test_atomic_arith`，其 `ref_atomic_arith` 给出了期望的并发累加结果。**

#### 4.3.5 小练习与答案

**练习 1**：`ct.atomic_add` 一次处理一个形状为 `(128,)` 的 tile（128 个不同索引），这「整体」是原子的吗？
> **答案**：不是。每个被索引到的元素**各自**原子地完成「读-加-写」，但 128 个元素之间没有事务性，写入顺序也未定义。只要每个索引互不相同，结果仍正确；若多个索引指向同一元素，结果是「所有更新都生效」，但顺序不定。

**练习 2**：为什么位运算原子（`atomic_and/or/xor`）要求 `update` 的 dtype 与目标**完全一致**，而 `atomic_add` 允许隐式转换？
> **答案**：位运算是「按比特」操作，任何隐式数值转换（如 float→int 或 int32→int64 的符号/宽度扩展）都会改变比特模式，使结果失去意义；算术加法是数值操作，只要类型可安全转换就能保持语义。测试 [test/test_atomic.py:159-193](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L159-L193) 正是验证这条：dtype 不一致时抛出 `Bitwise atomic read-modify-write operations require the update dtype (...) to exactly match the target dtype (...)`。

**练习 3**：`atomic_cas(x, idx, expected, desired)` 在「当前值不等于 expected」时返回什么？
> **答案**：返回**当前的旧值**（不写入），与「等于 expected」时的返回值（也是旧值）形式一致；区别仅在于是否执行了写入。bounds 越界时则返回 `expected`（见 [src/cuda/tile/_stub.py:1692-1694](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1692-L1694)）。

---

### 4.4 fence：内存栅栏

#### 4.4.1 概念说明

**内存栅栏（fence，又称内存屏障 barrier）** 是一种「不带数据、只立规矩」的操作：它本身不读不写任何数组元素，只是强制在它「之前」与「之后」的访存之间建立一道不可逾越的排序墙。栅栏常用于「我想同步，但此刻没有合适的原子写可以挂载 acquire/release」的场景。

栅栏与 acquire/release 的关系可以这样理解：

- acquire/release 把排序保证**附着在具体的某次 load/store/atomic 上**；
- fence 则是**独立**的一次排序点，相当于「无数据的 acquire-release」。

#### 4.4.2 核心流程

一个典型的栅栏用法（概念性伪代码，**仅示意，非项目 API**）：

```
# block A：先写一批数据，再立一道 fence，确保之前的写全部落盘
store(data_a, ...)
fence(release, scope=DEVICE)        # 概念性，非真实函数名
set_flag(1)

# block B：等 flag，再立一道 fence，确保之后的读不会被提前
wait_flag(1)
fence(acquire, scope=DEVICE)        # 概念性，非真实函数名
load(data_a)                        # 现在一定能读到对方写入
```

栅栏的 scope 同样决定「这道墙对多大范围内的 block 生效」。

#### 4.4.3 源码精读（关于 fence 的诚实说明）

**需要特别说明（待确认）**：在本讲义所对应的 HEAD（`0c46a62`），`cuda.tile` 的 **Python 公共 API 并没有暴露一个独立的 `ct.fence()` 原语**。在 `__init__.py` 的导出表（[src/cuda/tile/__init__.py:81-88](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/__init__.py#L81-L88) 与 `__all__` [src/cuda/tile/__init__.py:244-251](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/__init__.py#L244-L251)）中，与内存排序相关的公共符号只有 `MemoryOrder`、`MemoryScope` 与八个 `atomic_*`，没有 `fence`。因此在当前版本里，「跨 block 排序」是通过**给原子操作和 `load`/`store` 显式指定 `memory_order`/`memory_scope`** 来实现的——这事实上承担了栅栏的职责。

不过，**底层 Tile IR 确实存在内存栅栏操作**，证据是后端字节码编码器：

```python
def encode_MemoryFenceAliasTkoOp(  # since 13.4
    code_builder: CodeBuilder,
    result_token_type: TypeId,  # since 13.4
    token: Value,  # since 13.4
) -> Value:
    _buf = code_builder.buf
    # Opcode
    encode_varint(131, _buf)
    ...
```

> —— [src/cuda/tile/_bytecode/encodings.py:1328-1339](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_bytecode/encodings.py#L1328-L1339)（说明：这是把 Tile IR 的 `MemoryFenceAliasTkoOp` 编码为字节码的函数，opcode 为 131，自字节码版本 13.4 起引入；它操作的是 token，说明栅栏在 Tile IR 里是「token 链」的一部分。）

文档也明确指向了更底层的规范：

> For further details, see the Memory Model section of the `Tile IR documentation`.
>
> —— [docs/source/memory_model.rst:23](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst#L23)（说明：cuTile 把内存模型/栅栏的细节规范交给了 Tile IR 文档）

**结论**：在 cuTile Python 层，fence 当前以「`memory_order`/`memory_scope` + token 排序」的形式间接可用（u6-l3 会讲解 `token_order_pass` 如何用 token 链为内存操作定序）；独立的 `ct.fence()` 是否会在后续版本暴露，请以项目最新文档为准。

#### 4.4.4 代码实践（源码阅读型）

由于没有独立的 `ct.fence`，本节实践为**源码阅读型**：

1. 打开 [src/cuda/tile/_bytecode/encodings.py:1328-1339](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_bytecode/encodings.py#L1328-L1339)，确认 Tile IR 存在 `MemoryFenceAliasTkoOp`（opcode 131，since 13.4）。
2. 阅读 `docs/source/memory_model.rst` 全文，圈出「逐元素粒度」「跨 block 可重排」「order + scope 两个旋钮」三句话。
3. （进阶）在后续学习 u6-l3 的 `token_order_pass` 时，回顾本节：栅栏在 Tile IR 里是 token 链的一个节点，token 链就是 cuTile 给内存操作「隐性立墙」的机制。

**需要观察的现象**：Python 公共 API 无 `fence`，但后端字节码层与 Tile IR 层都有栅栏概念；二者通过 token 排序 pass 桥接。

**预期结果**：理解「fence 在 cuTile 里目前不是显式 Python 原语，而是由 order/scope + token 链实现」这一分层事实。

#### 4.4.5 小练习与答案

**练习 1**：既然没有 `ct.fence()`，那要在两个 block 间建立「A 的写在 B 的读之前完成」的排序，当前 API 里怎么做？
> **答案**：用 4.1 的 acquire/release 配对——A 端用 `RELEASE` 序（如 `ct.store(..., memory_order=RELEASE)` 或带 `ACQ_REL` 的原子写）发信号，B 端用 `ACQUIRE` 序读信号；并选合适的 `memory_scope`（通常 `DEVICE`）。这等价于「把栅栏附着在具体的访存上」。

**练习 2**：`MemoryFenceAliasTkoOp` 的输入输出是 token 而非数据，这暗示了什么？
> **答案**：栅栏不搬运数据，只编排「先后」。用 token 作为输入输出，说明它在 IR 里是 token 依赖链的一环——前一个内存操作的输出 token 喂给栅栏，栅栏再产出新 token 喂给后续操作，从而在编译期就把「先于」关系固化下来（这正是 u6-l3 token 排序 pass 的核心思想）。

---

## 5. 综合实践：直方图内核

本任务把本讲的「跨 block 可重排 → 需要原子」串起来。直方图（histogram）是「多对一写入」的教科书例子：输入里有大量元素，要把每个元素的值映射到一个 bin，再给该 bin 的计数 `+1`。由于不同 block 会处理不同的输入块、却可能命中**同一个 bin**，普通 `store` 必然丢更新——必须用 `atomic_add`。

### 5.1 实践目标

实现内核 `histogram_kernel`：给定输入数组 `x`（元素取值在 `[0, NBINS)`）与长度为 `NBINS` 的计数数组 `hist`（初始为 0），用 `atomic_add` 把每个元素累加进对应的 bin，最终 `hist[b]` 等于 `x` 中等于 `b` 的元素个数。

### 5.2 参考实现（示例代码）

> 下面是基于本讲 API 写的**示例代码**（非项目自带 sample），可在本地用 `torch` 张量作为宿主数组（u2-l2 的 DLPack/CUDA Array Interface 入口）启动。

```python
# 示例代码：直方图内核
import torch
import cuda.tile as ct
from math import ceil

@ct.kernel
def histogram_kernel(x, hist, N: ct.Constant[int], TILE: ct.Constant[int]):
    bid = ct.bid(0)
    # 1) 计算本 block 负责的元素索引（瓦片空间 → 元素空间）
    idx = ct.arange(TILE, dtype=ct.int64) + bid * TILE
    # 2) 用 gather 取出这一块输入值（idx 作为元素下标）
    vals = ct.gather(x, idx, check_bounds=False)  # 调用方保证 idx < N
    # 3) 多个 block 可能命中同一 bin，必须原子累加
    ct.atomic_add(hist, vals, ct.full((TILE,), 1, dtype=ct.int32),
                  memory_order=ct.MemoryOrder.ACQ_REL,
                  memory_scope=ct.MemoryScope.DEVICE)

def run_histogram():
    N, NBINS, TILE = 1 << 16, 256, 128
    x = torch.randint(0, NBINS, (N,), dtype=torch.int32, device='cuda')
    hist = torch.zeros(NBINS, dtype=torch.int32, device='cuda')
    grid = (ceil(N / TILE),)
    ct.launch(torch.cuda.current_stream(), grid, histogram_kernel,
              (x, hist, N, TILE))
    # 与 torch 参考实现对照
    ref = torch.bincount(x.cpu(), minlength=NBINS)
    assert torch.equal(hist.cpu(), ref), (hist.cpu()[:8], ref[:8])
    print("histogram OK, total =", int(hist.cpu().sum()), "expected", N)

run_histogram()
```

要点对照本讲概念：

- `idx` 跨 block 互不重叠，故 `gather` 读不冲突；但 `vals` 里**不同 block 可能产生相同 bin 值**，所以 `atomic_add(hist, vals, ...)` 是必须的——这就是「为何需要原子操作」。
- 用 `gather`（u4-l2）做逐点读、用 `atomic_add` 做逐点原子写，二者索引约定一致（1D 数组直接传单个 tile）。
- 显式写出 `ACQ_REL / DEVICE` 只是为了点明默认值；直方图只关心「最终计数正确」，其实用 `RELAXED` 也够（更便宜），可作为下面的练习。

### 5.3 操作步骤与现象

1. 按 u1-l2 的方式安装 `cuda-tile[tileiras]`，准备一块 NVIDIA GPU。
2. 粘贴并运行上面的 `run_histogram()`。
3. 把 `memory_order` 改成 `ct.MemoryOrder.RELAXED` 再跑一次。

**需要观察的现象**：两次都能得到与 `torch.bincount` 完全一致的直方图；`hist.sum()` 恰好等于 `N`（无丢更新）。

**预期结果**：`histogram OK, total = 65536 expected 65536`（N=65536 时）。**若你本地未配置 GPU/tileiras，请记为「待本地验证」，并可转而阅读 [test/test_atomic.py:471-492](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_atomic.py#L471-L492) 的 `offset_atomic_add_with_mask`——它就是「带掩码的直方图累加」最小可参照实现。**

### 5.4 进阶讨论：为什么不能去掉原子？

把内核里的 `ct.atomic_add(...)` 换成等价的「读-加-写」`ct.scatter(hist, vals, ct.gather(hist, vals) + 1)`（普通读+普通写）。由于这是**非原子**的读-改-写，两个 block 同时读到同一个 bin 的旧值、各自加 1 再写回，会导致一次更新被覆盖——`hist.sum()` 将**小于** `N`。这正是 4.3.1 所说「逐元素原子」要解决的问题。直方图因此是理解「为何需要原子操作」最直观的窗口。

---

## 6. 本讲小结

- cuTile 的内存模型**允许编译器/硬件重排跨 block 的访存**；没有显式同步，就不能假设 block 间的读写顺序（[docs/source/memory_model.rst:10-12](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst#L10-L12)）。同步以**单个数组元素**为粒度（[docs/source/memory_model.rst:20-21](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst#L20-L21)）。
- **MemoryOrder** 给出排序语义：`WEAK`（非原子，load/store 默认）、`RELAXED`（原子但不排序）、`ACQUIRE/RELEASE`（配对建立可见性）、`ACQ_REL`（RMW 默认）。`load` 只能 `WEAK/RELAXED/ACQUIRE`，`store` 只能 `WEAK/RELAXED/RELEASE`。
- **MemoryScope** 给出排序范围：`NONE/BLOCK/CLUSTER(cuda.lang)/DEVICE(默认)/SYS`；scope 仅在 order 非 `WEAK` 时有意义，原子操作配 `NONE` 会编译失败。
- **原子族** `atomic_add/max/min/and/or/xor/xchg/cas`（模块级）与 `RawArrayMemory.atomic_*_offset`（裸内存级）遵循 gather/scatter 索引约定，**逐元素原子、整体非原子、返回旧值**；位运算要求 dtype 完全一致。
- **fence** 在当前 Python API 中**没有独立原语**；跨 block 排序由 `memory_order`/`memory_scope` + token 链实现，Tile IR 后端存在 `MemoryFenceAliasTkoOp`（opcode 131，since 13.4）作为底层支撑。
- 直方图是「多对一写入必须原子」的典型场景，`atomic_add` 是其标准解法。

## 7. 下一步学习建议

- **u4-l2 gather/scatter 与高级索引**：本讲的原子操作与 gather/scatter 共享同一套索引约定，建议把三者放在一起对照，理解「读端可幂等、写端必原子」的边界。
- **u6-l3 token 排序 pass**：想知道 cuTile 在编译期如何把零散的内存操作用 token 链串成「有序」、从而保证 GPU 内存模型正确性，就去读 `token_order_pass`——它是本讲「fence/order 在编译端如何落地」的真正答案。
- **u8-l1 launch 与调度**：理解 `atomic_*` 这类内存排序操作最终如何随 kernel 一起 JIT 编译并经 `cuLaunchKernel` 上 GPU。
- **延伸阅读**：官方文档 [docs/source/memory_model.rst](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/docs/source/memory_model.rst) 指向的 Tile IR Memory Model 文档，那里有栅栏与排序的形式化定义。
