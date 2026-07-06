# 内存模型与原子操作

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚为什么 cuTile 中**跨 block 的访存顺序默认不保证**，以及这种「可重排」设计为什么是必要的。
- 准确解释 `MemoryOrder`（WEAK/RELAXED/ACQUIRE/RELEASE/ACQ_REL）五个取值各自的语义。
- 准确解释 `MemoryScope`（NONE/BLOCK/CLUSTER/DEVICE/SYS）的范围层级，并知道原子操作默认取哪个 scope。
- 掌握 `ct.atomic_add / atomic_cas / atomic_max ...` 一族原子读改写（RMW）操作的用法、返回值与索引约定，以及它们与 `gather/scatter` 的关系。
- 理解 cuTile 里**没有独立的 `ct.fence()` 函数**，定序（fence 的作用）是借助原子操作（或显式带上 order 的 `load/store`）的 acquire/release 语义来实现的，并能据此写出正确的跨 block 同步代码。

本讲对应实践是「直方图内核」：用 `atomic_add` 把多个 block 的局部计数累加到全局直方图。

## 2. 前置知识

在进入内存模型前，请先回忆下面这些在前面讲义中已经建立的概念（本讲不再重复细节）：

- **block 是执行单元、tile 是数据单元**（u2-l1）。一个 kernel 由 grid 里的若干 block 并行执行，每个 block 把 kernel 函数体完整跑一遍；tile 是 kernel 内部不可变的数据块。
- **block 之间允许同步、block 内部禁止显式同步**（u2-l1）。这条规则正是本讲的出发点：既然 block 间可以通信，就必须有一套约定来保证它们看到的内存视图是一致的。
- **load–compute–store 范式**（u3-l1）：`ct.load(array, index, shape)` 按「瓦片索引」取一块 tile，`ct.store(array, index, tile)` 写回。
- **gather/scatter**（u4-l2）：按「元素下标」而非瓦片索引读写数组，越界处用掩码处理。本讲的原子操作**沿用 gather/scatter 的索引约定**，所以理解 gather 的索引元组规则是前置。

如果这几个概念还模糊，建议先回顾 u2-l1 与 u4-l2 再继续。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [docs/source/memory_model.rst](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/memory_model.rst#L7-L23) | 用一段话点明 cuTile 内存模型的核心立场：编译器与硬件可以重排访存，跨 block 的顺序默认不保证；并引出 MemoryOrder / MemoryScope 两个定序维度。 |
| [src/cuda/tile/_memory_model.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L1-L80) | 定义 `MemoryOrder` 与 `MemoryScope` 两个枚举（以及 `MemorySpace`），是本讲最核心的数据结构来源。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py) | 定义全部原子操作 `stub`：顶层 `ct.atomic_*` 函数、`RawArray` 的 `atomic_*_offset` 方法、`TiledView` 的 `atomic_store_*` 方法；以及 `load/store` 上的 `memory_order`/`memory_scope` 形参。 |
| [test/test_atomic.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py) | 原子操作的端到端测试，是本讲「代码实践」最可靠的依据：它演示了正确的用法，也演示了非法 order/scope 组合会抛什么错。 |

`MemoryOrder`、`MemoryScope` 与八个 `ct.atomic_*` 都在顶层 [`__init__.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/__init__.py#L81-L88) 中再导出并列入 `__all__`，是公开契约。

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：**内存模型基础**、**MemoryOrder**、**MemoryScope**、**atomic_* 原子操作族**，最后用一节讨论「fence 在 cuTile 里如何实现」。

### 4.1 内存模型基础：为什么跨 block 顺序不保证

#### 4.1.1 概念说明

你可能已经习惯「程序文本里写在前的语句先执行、写在后的后执行」。但在 GPU 上，这只在一个 block **内部**近似成立；一旦涉及**多个 block**，这个直觉就失效了。

cuTile 的内存模型（memory model）允许编译器和硬件**为了性能而重排（reorder）访存操作**。文档用一句话点明了这个立场：

> cuTile's memory model permits the compiler and hardware to reorder operations for performance. Without explicit synchronization, the ordering of memory accesses across blocks is not guaranteed.

为什么要这样设计？因为 GPU 上同时跑着成百上千个 block，每个 block 内部又跑着大量 warp，如果强制所有访存都按程序顺序全局排队，硬件流水线会被完全堵死。允许重排让硬件可以乱序发射、合并访存、利用缓存，从而换来数量级的吞吐提升。代价就是：**你需要显式地告诉编译器「这两次访存不能乱序」**，否则编译器没有义务保证它们的全局先后。

这条规则和 CUDA C++ 的内存模型一脉相承，只是 cuTile 把抽象层级抬高到了 block/tile 级。一个关键直觉是：

- **block 内部**：集体运算（如一次完整的 tile `load`/`store`/归约）的边界已经隐式同步了（u2-l1），所以 block 内一般不需要你再操心定序。
- **block 之间**：默认没有任何顺序保证。block A `store` 了一个值，block B 紧接着 `load`，**不保证** B 能读到 A 写的值——除非你用了本讲讲的同步原语。

#### 4.1.2 核心流程

把跨 block 的访存「钉住顺序」需要两个维度协同：

1. **告诉编译器这一操作参与定序**——这就是 `MemoryOrder`（4.2）。
2. **告诉编译器和哪些 block 一起定序**——这就是 `MemoryScope`（4.3）。

二者缺一不可。文档还强调一个容易被忽视的细节：**同步是逐元素（per-element）粒度的**——数组里每个元素各自独立地参与内存模型，而不是「整个数组一把锁」。

#### 4.1.3 源码精读

内存模型的总纲在文档里只有短短几行，但它就是后续所有规则的根：

[docs/source/memory_model.rst:10-23](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/memory_model.rst#L10-L23) —— 这段文字确认了三件事：(1) 编译器/硬件可重排访存；(2) 无显式同步时跨 block 顺序不保证；(3) 用 MemoryOrder + MemoryScope 协调，且粒度是 per-element。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用一句话把「跨 block 默认无序」和 cuTile 的设计取舍联系起来。

1. 打开 [docs/source/memory_model.rst](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/memory_model.rst#L10-L12)，阅读前 3 行。
2. 回忆 u2-l1 讲过的「block 内禁止显式同步、block 间允许同步」。
3. 思考：既然 block 内已经隐式同步，为什么内存模型只强调「across blocks」？

**预期结果**：你能用自己的话说出「block 内的集体运算边界就是同步点，所以内存模型真正需要操心的是 block 之间的通信」。

#### 4.1.5 小练习与答案

**练习**：假设 block A 执行 `ct.store(buf, 0, 1)`，block B 执行 `print(ct.load(buf, 0, shape=()))`。能否保证 B 打印出 `1`？

**答案**：**不能保证**。在没有任何定序标注（默认都是 `MemoryOrder.WEAK`）时，跨 block 的 store 与 load 顺序不保证，B 可能读到旧值、也可能读到新值，行为未定义。要保证可见，必须让 A 的 store 带 `RELEASE`、B 的 load 带 `ACQUIRE`，且二者 scope 覆盖彼此（见 4.5）。

---

### 4.2 MemoryOrder：定序语义

#### 4.2.1 概念说明

`MemoryOrder` 描述**单次访存操作本身提供多强的顺序保证**，它是一个枚举，定义在 [`_memory_model.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L30-L53)。理解它的关键是 C++11/CUDA 风格的「acquire/release」配对模型，而不是把每次访存都当成「全局屏障」。

五个取值：

| 取值 | 含义 | 典型用在哪 |
| --- | --- | --- |
| `WEAK` | 非原子、无任何顺序保证。`load/store` 的**默认值**。 | 普通的 load/store，不需要跨 block 可见性时。 |
| `RELAXED` | 原子，但**不提供顺序保证**，只保证这次读改写本身原子。 | 计数器累加（如直方图），只关心最终值不丢更新，不关心顺序。 |
| `ACQUIRE` | 获取语义：读到一次 release 写入后，**本操作之后的读写不会被重排到它前面**。 | 「读完标志位，再读数据」。 |
| `RELEASE` | 释放语义：**本操作之前的读写不会被重排到它后面**。 | 「先写数据，再发布标志位」。 |
| `ACQ_REL` | 同时具备 acquire 与 release。**原子 RMW 操作的默认值**。 | 既读又写的原子操作（如 `atomic_add`），既是发布又是获取。 |

两条直觉记忆法：

- **acquire/release 必须配对**才有意义：release 端的「之前的写入」对 acquire 端可见。单向用 acquire 而没人 release，等于没有同步。
- **RELAXED 只保证原子性、不保证顺序**：它适合「我不在乎谁先谁后，只在乎最终结果正确」的场景，直方图就是典型。

#### 4.2.2 核心流程

`MemoryOrder` 在源码里如何生效，分两类操作：

- **`load/store`**：默认 `WEAK`。可合法地改成 `RELAXED`，或对 `load` 用 `ACQUIRE`、对 `store` 用 `RELEASE`（但不能反过来——`load` 不能 `RELEASE`，`store` 不能 `ACQUIRE`，因为它们不既是读又是写）。
- **原子 RMW（`atomic_*`）**：默认 `ACQ_REL`。允许取 `RELAXED/ACQUIRE/RELEASE/ACQ_REL`，但**不允许 `WEAK`**——因为原子操作如果退化为非原子就毫无意义。测试里专门有一条断言：用 `WEAK` 调原子会抛 `Invalid memory order for tile_atomic_rmw/cas`。

#### 4.2.3 源码精读

枚举定义与每个取值的精确语义注释：

[src/cuda/tile/_memory_model.py:30-53](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L30-L53) —— 五个取值的语义注释（`WEAK` 是 load/store 默认；`ACQUIRE/RELEASE` 描述了配对可见性；`ACQ_REL` 是二者合并）。

`load` 接受的合法 order 取值（注意 `store` 没有 `ACQUIRE`、`load` 没有 `RELEASE`）：

[src/cuda/tile/_stub.py:1300-1303](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1300-L1303) —— `load` 的 `memory_order` 注释：合法值为 `WEAK/RELAXED/ACQUIRE`。

[src/cuda/tile/_stub.py:1436-1439](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1436-L1439) —— `store` 的 `memory_order` 注释：合法值为 `WEAK/RELAXED/RELEASE`。

原子操作的默认 order 是 `ACQ_REL`，例如 `atomic_add`：

[src/cuda/tile/_stub.py:1829-1840](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1829-L1840) —— `atomic_add` 的签名，默认 `memory_order=MemoryOrder.ACQ_REL`。

非法组合的校验在测试里被钉死：原子操作用 `WEAK` 会抛错：

[test/test_atomic.py:395-410](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L395-L410) —— `test_atomic_rmw_weak_ordering` 断言：原子 RMW 用 `WEAK` 抛 `Invalid memory order for tile_atomic_rmw`。

#### 4.2.4 代码实践（阅读型 + 待本地验证）

**目标**：体会 `RELAXED` 与 `ACQ_REL` 的差别。

1. 阅读 [test/test_atomic.py:298-342](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L298-L342) 的 `test_atomic_order_scope`：它对 `None/ACQ_REL/ACQUIRE/RELEASE/RELAXED` 五种 order 各启动一次，并用 filecheck 验证生成的字节码里 `atomic_rmw_tko` 指令确实带着正确的 order 标注。
2. 在本地写一个最小内核，分别用 `memory_order=ct.MemoryOrder.RELAXED` 和默认（`ACQ_REL`）调用 `ct.atomic_add`，用 `CUDA_TILE_DUMP_TILEIR=1`（见 u8-l5）dump 出中间产物，对比两条 `atomic_rmw_tko` 指令的 order 字段是否如预期不同。

**需要观察的现象**：dump 的 IR 里 `atomic_rmw_tko` 操作的 order 属性从 `acq_rel` 变成 `relaxed`。

**预期结果 / 待本地验证**：order 字段随参数变化；数值结果（多次累加的最终和）两种 order 都正确，因为累加只依赖原子性、不依赖顺序。

#### 4.2.5 小练习与答案

**练习 1**：`ct.load` 能不能传 `memory_order=ct.MemoryOrder.RELEASE`？为什么？

**答案**：**不能**。`load` 的合法 order 只有 `WEAK/RELAXED/ACQUIRE`（见源码注释）。`RELEASE` 描述的是「之前的写不能排到本操作之后」，对一次纯读操作没有意义；要发布写入应当用带 `RELEASE` 的 `store`。

**练习 2**：为什么 `atomic_add` 默认是 `ACQ_REL` 而不是 `RELAXED`？

**答案**：原子 RMW 既读又写，默认 `ACQ_REL` 让它同时充当 acquire 与 release，方便用户直接拿来做轻量同步（如自旋等待、生产者发布）。若你只关心累加结果、不需要顺序保证，可显式降级为 `RELAXED` 以换取更少的定序开销（直方图就是这样）。

---

### 4.3 MemoryScope：定序范围

#### 4.3.1 概念说明

`MemoryOrder` 说的是「多强的保证」，`MemoryScope` 说的是「这个保证对**哪些 block** 生效」。定序是有代价的：scope 越大，硬件要刷的缓存层级越多、越慢。所以 cuTile 让你按需选择最小的、够用的范围。

`MemoryScope` 也是一个枚举，定义在 [`_memory_model.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L8-L27)。范围从小到大：

| 取值 | 范围 | 备注 |
| --- | --- | --- |
| `NONE` | 无范围 | **load/store 的默认值**，与 `WEAK` 配对；原子操作不能用 `NONE`。 |
| `BLOCK` | 同一个 block 内 | 仅当同一 block 内有需要定序的访存时用。 |
| `CLUSTER` | 同一 thread-block cluster 内 | 注释标注「cuda.lang only」，本 `cuda.tile` 手册中较少触及。 |
| `DEVICE` | 同一 GPU 上所有线程 | **原子 RMW 的默认值**，最常用的跨 block scope。 |
| `SYS` | 全系统（多 GPU + host） | 跨 GPU 或与 host 共享内存时才需要，开销最大。 |

一条硬规则：**原子操作的 order 一旦不是 `WEAK`，scope 就不能是 `NONE`**。测试里专门验证了这一点——非 `WEAK` order 配 `NONE` scope 会抛 `tile_atomic_rmw ... requires a memory scope`。直觉解释：你既然要求了定序（非 WEAK），就必须指明「对谁定序」，否则编译器无从生成正确的屏障。

#### 4.3.2 核心流程

scope 的选择流程很简单，按「需要同步的 block 范围」取最小够用的：

1. 只在本 block 内同步 → `BLOCK`。
2. 在整个 GPU 上跨 block 同步（绝大多数场景）→ `DEVICE`。
3. 跨 GPU 或与 host 同步 → `SYS`。

注意 cuTile 不暴露线程级（warp）scope，因为它的抽象是 block 级；block 内部的细粒度定序由编译器在集体运算边界隐式完成。

#### 4.3.3 源码精读

枚举定义与范围注释：

[src/cuda/tile/_memory_model.py:8-27](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L8-L27) —— `MemoryScope` 五个取值，注意 `NONE` 与 `WEAK` 配对、`DEVICE` 是原子默认、`SYS` 含多 GPU 与 host。

`load` 的 scope 注释（仅在 order 非 WEAK 时有意义）：

[src/cuda/tile/_stub.py:1302-1303](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1302-L1303) —— `load` 的 `memory_scope` 注释：仅当 `memory_order` 非 `WEAK` 时才有意义。

非 `WEAK` order + `NONE` scope 是非法的，测试钉死了这条规则：

[test/test_atomic.py:412-439](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L412-L439) —— `test_atomic_rmw_none_scope` 断言：原子 RMW 配非 WEAK order 与 `NONE` scope 抛 `tile_atomic_rmw ... requires a memory scope`。

#### 4.3.4 代码实践（阅读型）

**目标**：把 scope 的取值和字节码输出对应起来。

1. 阅读 [test/test_atomic.py:270-274](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L270-L274) 的 `ct_scope_to_tileir_scope` 映射：它告诉你 Python 端的 `BLOCK/DEVICE/SYS` 在 Tile IR 里分别叫 `tl_blk/device/sys`。
2. 在 `test_atomic_order_scope` 里，filecheck 指令拼接出 `atomic_rmw_tko <order> <scope>`（如 `atomic_rmw_tko acq_rel device`），说明 order 与 scope 共同决定一条原子指令的属性。

**预期结果**：你能说出「换 `memory_scope=ct.MemoryScope.BLOCK` 后，dump 的 IR 里 scope 字段会变成 `tl_blk`」。

#### 4.3.5 小练习与答案

**练习**：直方图内核里，`atomic_add` 应该选哪个 scope？为什么不是 `SYS`？

**答案**：选默认的 `DEVICE` 就够了。直方图的输入和输出都在同一块 GPU 的全局显存里，参与累加的 block 都在同一设备上，`DEVICE` 已经覆盖全部相关 block。`SYS` 会引入跨 GPU/host 的额外屏障开销，对单 GPU 直方图是纯浪费。

---

### 4.4 atomic_* 原子操作族

#### 4.4.1 概念说明

原子操作（atomic operation）解决的核心问题是**多个 block 同时改同一个数组元素时的数据竞争**。普通 `store` 是「读—改—写」在概念上分开的，两个 block 同时 `store` 同一地址会互相覆盖；原子操作把「读—改—写」打包成**硬件保证不可分割**的一步，从而不会丢更新。

cuTile 提供两类语义的原子操作：

- **读改写（RMW）类**：`atomic_add / atomic_max / atomic_min / atomic_and / atomic_or / atomic_xor / atomic_xchg`。语义是「读旧值 → 计算 → 写新值 → 返回旧值」，整个过程（**逐元素**）原子。
- **比较交换（CAS）类**：`atomic_cas`。语义是「如果当前值 == expected，就写成 desired；无论如何返回旧值」。CAS 是构建更复杂无锁结构（如无锁队列、自旋锁）的基石。

它们都**沿用 `gather/scatter` 的索引约定**（u4-l2）：`indices` 是长度等于数组秩的元组，每个分量是整数 tile/scalar，按 NumPy 规则广播；一维数组可省略元组。这一点很关键——原子操作的「下标」是**元素下标**而非瓦片索引。

还有一个易混点：原子性是**逐元素**的，**整批操作并不原子**。文档原话：「For each individual element, the operation is performed atomically, but the operation as a whole is not atomic, and the order of individual writes is unspecified.」也就是说，对 128 个元素的 `atomic_add`，每个元素的累加各自原子、互不干扰，但 128 次累加的先后顺序未指定，且不可作为一个整体被其它 block 看到。

#### 4.4.2 核心流程

一次 `ct.atomic_add(array, indices, update)` 的语义可写成下面的伪代码（其余 RMW 操作只是把 `+=` 换成对应运算）：

```text
in parallel, for each element e at indices:
    if not check_bounds or e within bounds:
        old[e] = array[e]               # 原子地读
        array[e] = old[e] + update[e]   # 原子地写回
    else:
        old[e] = <implementation-defined>   # 越界：不操作，返回实现定义值
return old                              # 返回的是「旧值」tile
```

注意三个要点：

1. **返回旧值**：所有原子 RMW 都返回操作前的旧值（tile），这是实现「读—判定—写」无锁算法的入口。
2. **越界处理**：默认 `check_bounds=True`，越界处不操作、返回实现定义值；设 `check_bounds=False` 则越界是未定义行为（UB），由调用方保证不越界，换性能。
3. **类型要求**：算术原子（add/max/min/xchg）支持 int/float 32/64 等；位运算原子（and/or/xor）要求 update 与目标 dtype **完全一致**且为整数；`atomic_cas` 的 dtype 集合更窄（如 `uint32/uint64/int32/int64/float32/float64`，见 [test_atomic.py:233-234](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L233-L234)）。

#### 4.4.3 源码精读

顶层 `ct.atomic_add` 的签名与语义（默认 `ACQ_REL` + `DEVICE`）：

[src/cuda/tile/_stub.py:1829-1840](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1829-L1840) —— `atomic_add`：读旧值、加 `update`、写回、返回旧值。

`ct.atomic_cas` 的签名、索引约定与「逐元素原子、整批非原子」的说明，含一段可直接对照的伪代码：

[src/cuda/tile/_stub.py:1695-1766](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1695-L1766) —— `atomic_cas`：比较相等才写入 desired，否则不改；始终返回旧值。注意它的 `indices`/`expected`/`desired` 广播约定与 `gather` 一致。

其余 RMW 操作（xchg/max/min/and/or/xor）由同一个装饰器 `_doc_atomic_rmw_op` 统一补充「逐元素原子、整批非原子、索引约定同 gather、默认 bounds 检查」的说明：

[src/cuda/tile/_stub.py:1769-1812](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1769-L1812) —— `_doc_atomic_rmw_op` 给一族 RMW 操作附加统一的索引/越界/原子性说明。

除顶层函数外，还有两个等价的「方法式」入口（多用于按裸偏移、带掩码访问的场景）：

- `RawArray` 上的 `atomic_*_offset`：通过 `array.get_raw_memory()` 拿到裸内存视图，按 `base_ptr + offset` 寻址，支持 `mask` 参数（而非 `check_bounds`）。例如 `atomic_add_offset`：

[src/cuda/tile/_stub.py:459-469](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L459-L469) —— `RawArray.atomic_add_offset(offset, update, *, mask=None, memory_order=ACQ_REL, memory_scope=DEVICE)`：按元素偏移做原子累加，`mask=False` 处不操作。

- `TiledView` 上的 `atomic_store_*`（add/max/min/and/or/xor）：更简化的接口，不暴露 order/scope，按瓦片索引写：

[src/cuda/tile/_stub.py:891-941](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L891-L941) —— `TiledView.atomic_store_add/max/min/and/or/xor`：瓦片索引式原子写。

测试用例把这几条路都跑通，是最佳示范。例如 `atomic_arith_kernel` 同时演示了顶层函数与裸内存两条路径：

[test/test_atomic.py:56-74](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L56-L74) —— `atomic_arith_kernel`：用 `static_eval` 在编译期选出操作符，分别走 `ct.atomic_add(...)` 与 `x.get_raw_memory().atomic_add_offset(...)` 两条路，再把旧值 `scatter` 到输出数组。

带 `mask` 的裸内存原子（直方图实践会用到这个能力）：

[test/test_atomic.py:471-492](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L471-L492) —— `offset_atomic_add_with_mask`：`mask = (offset % 2) == 0`，只对偶数偏移做原子累加，奇数偏移保持不变。

#### 4.4.4 代码实践

**目标**：用 `ct.atomic_add` 实现一个一维计数内核，并验证「不用原子会丢更新」。

操作步骤：

1. 准备一个长度 `N` 的 int32 数组 `x`，初值全 0；准备一个全 1 的 `ones`。
2. 写两个内核：一个用普通 `store` 累加（错误示范），一个用 `atomic_add` 累加（正确）。
3. 用 `grid = cdiv(N, TILE)` 启动，让多个 block 写同一批地址。

```python
# 示例代码（基于 test_atomic.py:56-74 的模式改写，未在本地运行验证）
import cuda.tile as ct
import torch

N = 4096
TILE = 128

@ct.kernel
def broken_inc(x, ones, N: ct.Constant[int], TILE: ct.Constant[int]):
    bid = ct.bid(0)
    offset = ct.arange(TILE, dtype=ct.int64) + bid * TILE
    mask = offset < N
    old = ct.gather(x, offset)           # 读旧值
    new = old + ct.gather(ones, offset)  # 加 1
    ct.scatter(x, offset, new)           # 普通写回：多 block 抢同一地址会丢更新

@ct.kernel
def atomic_inc(x, ones, N: ct.Constant[int], TILE: ct.Constant[int]):
    bid = ct.bid(0)
    offset = ct.arange(TILE, dtype=ct.int64) + bid * TILE
    mask = offset < N
    update = ct.gather(ones, offset)
    mem = x.get_raw_memory()
    mem.atomic_add_offset(offset, update, mask=mask)   # 原子累加，不会丢更新

x = torch.zeros(N, dtype=torch.int32, device="cuda")
ones = torch.ones(N, dtype=torch.int32, device="cuda")
stream = torch.cuda.current_stream()
grid = (ct.cdiv(N, TILE),)
ct.launch(stream, grid, atomic_inc, (x, ones, N, TILE))
print(x.sum().item())   # 预期 N（每个地址恰好 +1）
```

**需要观察的现象 / 待本地验证**：

- `atomic_inc`：最终 `x.sum()` 应等于 `N`（每个元素恰好被加 1）。
- 换成 `broken_inc`：因为多个 block 对同一地址「读—改—写」交织，部分更新会被覆盖，`x.sum()` 通常**小于** `N`，且每次运行结果可能不同——这正是数据竞争。

> 说明：上述示例代码改编自测试模式，具体的网格/掩码细节请以本地运行为准；核心结论（普通 `scatter` 抢写会丢更新、`atomic_add` 不会）是确定成立的。

#### 4.4.5 小练习与答案

**练习 1**：`atomic_add` 返回什么？为什么直方图内核通常**忽略**这个返回值？

**答案**：返回操作前的**旧值** tile。直方图只关心「最终累加结果正确」，不关心每次累加前的旧值，所以忽略返回值即可。`RELAXED` order 在这里也足够，因为不需要顺序保证。

**练习 2**：为什么说「整批 `atomic_add` 不是原子的」并不影响直方图正确性？

**答案**：因为原子性是**逐元素**的——每个 bin 的累加各自原子、互不干扰。整批操作的「顺序未指定」只影响 128 次累加谁先谁后，但累加满足交换律与结合律，最终和与顺序无关。所以逐元素原子性已足够保证直方图正确。

---

### 4.5 fence：通过 acquire/release 实现的定序栅栏

#### 4.5.1 概念说明

许多并行框架（CUDA C++、C++11）都有一个独立的 `__threadfence()` / `std::atomic_thread_fence` 函数——它不读写数据，只插入一道「栅栏」，强制栅栏前后的访存不被重排跨过它。这一节要讲清楚一件容易踩坑的事：

> **cuTile Python 目前没有独立的 `ct.fence()` 函数。**

在源码里全局搜索 `fence`（`src/cuda/tile` 与 `docs/source`）找不到任何公开 API；`__init__.py` 的 `__all__` 里也没有它。文档 [docs/source/memory_model.rst:14-19](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/memory_model.rst#L14-L19) 明确说，跨 block 协调访存只靠两个属性：`MemoryOrder` 与 `MemoryScope`——也就是说，**fence 的职责被吸收进了原子操作（以及带 order 的 load/store）的 acquire/release 语义里**。

这是一种「atomic-as-fence」的设计：一次带 `ACQUIRE/RELEASE/ACQ_REL` 的原子操作，除了完成自身的读改写，**同时充当一道栅栏**——它前后的同 scope 访存不会被重排跨过它。所以你不需要单独的 `fence`，而是**在合适的位置放一次带正确 order/scope 的原子操作**（或带 `RELEASE`/`ACQUIRE` 的 `store`/`load`）。

#### 4.5.2 核心流程

用 acquire/release 配对实现「生产者发布数据、消费者安全读取」的经典流程：

```text
# 生产者 block：
write data                                    # WEAK 即可，顺序无所谓
store flag=1, order=RELEASE, scope=DEVICE     # 发布：data 的写入不会被重排到这次 store 之后

# 消费者 block：
while load(flag, order=ACQUIRE, scope=DEVICE) != 1:   # 获取：读到 1 之后，后续读不会重排到这次 load 之前
    pass
read data                                     # 此时一定能看到生产者写的 data
```

注意 cuTile 没有独立的 `atomic_load`——消费者轮询时直接用 `ct.load(flag, ..., memory_order=ACQUIRE, memory_scope=DEVICE)` 即可（`load` 本身就接受 `ACQUIRE`，是首选）。栅栏（fence）效果就来自配对的 release-store 与 acquire-load：二者把「写 data」和「读 data」之间的顺序钉死了。

#### 4.5.3 源码精读

文档声明「定序只靠 MemoryOrder + MemoryScope」（即没有独立 fence 入口）：

[docs/source/memory_model.rst:14-23](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/memory_model.rst#L14-L23) —— 跨 block 协调只提供 MemoryOrder 与 MemoryScope 两个属性；同步是逐元素粒度。

`store` 接受 `RELEASE`、`load` 接受 `ACQUIRE`，二者配对即可形成栅栏：

[src/cuda/tile/_stub.py:1398-1406](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1398-L1406) —— `store` 签名，含 `memory_order=WEAK`（可改 `RELEASE`）与 `memory_scope=NONE`（可改 `DEVICE/SYS`）。

[src/cuda/tile/_stub.py:1245-1253](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1245-L1253) —— `load` 签名，含 `memory_order=WEAK`（可改 `ACQUIRE`）与 `memory_scope=NONE`。

#### 4.5.4 代码实践（源码阅读型 + 待本地验证）

**目标**：用 acquire/release 配对替代想象中的 `fence()`，构建一个跨 block 的「发布—等待」小例子。

1. 在源码与文档里确认「没有 `ct.fence`」（`grep -ni fence src/cuda/tile docs/source` 应无结果）。
2. 设计：生产者 block 0 写完数据后，用 `ct.store(flag, ..., memory_order=ct.MemoryOrder.RELEASE, memory_scope=ct.MemoryScope.DEVICE)` 发布；消费者 block 用 `ct.load(flag, ..., memory_order=ct.MemoryOrder.ACQUIRE, memory_scope=ct.MemoryScope.DEVICE)` 轮询，读到发布值后再读数据。

```python
# 示例代码：跨 block 发布—等待，用 acquire/release 替代 fence（未在本地运行验证）
@ct.kernel
def prod_cons(data, flag):
    bid = ct.bid(0)
    if bid == 0:
        # 生产者：先写数据，再 release 发布
        ct.store(data, (0,), ct.full((4,), 7, dtype=ct.int32),
                 memory_order=ct.MemoryOrder.RELEASE, memory_scope=ct.MemoryScope.DEVICE)
        ct.store(flag, (0,), 1,
                 memory_order=ct.MemoryOrder.RELEASE, memory_scope=ct.MemoryScope.DEVICE)
    else:
        # 消费者：acquire 等待发布，再读数据
        ready = ct.load(flag, (0,), shape=())  # 默认 WEAK，仅作占位
        while ready == 0:
            ready = ct.load(flag, (0,), shape=(),
                            memory_order=ct.MemoryOrder.ACQUIRE,
                            memory_scope=ct.MemoryScope.DEVICE)
        got = ct.load(data, (0,), shape=(4,))   # 由于 acquire，必能看到生产者写的 7
```

**需要观察的现象 / 待本地验证**：

- 加上 `RELEASE/ACQUIRE` 配对后，消费者应稳定读到 `data` 的发布值 `7`。
- 若把两端的 order 都退回默认 `WEAK`，则消费者**可能**读到旧值或未初始化值（行为未定义）——这正好反向印证了「fence 不可或缺，且它在 cuTile 里就是 acquire/release」。

> 说明：示例代码用于说明配对思路，跨 block 自旋等待在真实硬件上需要至少 2 个 block 且要保证它们能并发调度；具体可运行性请以本地验证为准。

#### 4.5.5 小练习与答案

**练习**：有人写了 `ct.fence()` 来同步两个 block，运行报 `AttributeError`。请解释原因并给出正确做法。

**答案**：cuTile Python **没有** `ct.fence`。定序要靠 acquire/release：发布方用 `ct.store(..., memory_order=RELEASE, memory_scope=DEVICE)`，等待方用 `ct.load(..., memory_order=ACQUIRE, memory_scope=DEVICE)`（或一次带 `ACQ_REL` 的原子操作）配对，二者 scope 覆盖彼此。这道「配对」就是 fence 的等价物。

---

## 5. 综合实践：直方图内核

把四个最小模块串起来，实现一个真正的直方图（histogram）内核。问题是：给定一个值域在 `[0, BINS)` 的输入数组 `data`，统计每个值出现的次数，写入长度为 `BINS` 的 `counts` 数组。

**为什么必须用原子操作**：`counts` 的每个 bin 可能被**多个 block、同一 block 的多个 tile 元素**同时累加。普通 `gather`+`scatter` 是「读—改—写」三步分离，并发写同一 bin 会互相覆盖、丢失计数。只有 `atomic_add` 把「读旧值—加一—写回」打包成逐元素原子，才能保证不丢更新。又因为累加满足交换律，我们**不需要顺序保证**，所以 `order=RELAXED`（或默认 `ACQ_REL`）即可，scope 用默认 `DEVICE`。

**边缘处的掩码**：当 `N` 不是 `TILE` 整数倍时，最后一个 tile 会越界。我们用 `RawArray.atomic_add_offset` 的 `mask` 参数把越界元素排除，避免把「padding 出来的假 bin」也累加进去。

```python
# 示例代码：直方图内核（基于 test_atomic.py:471-492 的 mask 用法，未在本地运行验证）
import cuda.tile as ct
import torch

BINS = 16
N = 4096
TILE = 128

@ct.kernel
def histogram(data, counts, N: ct.Constant[int], BINS: ct.Constant[int],
              TILE: ct.Constant[int]):
    bid = ct.bid(0)
    offset = ct.arange(TILE, dtype=ct.int64) + bid * TILE
    mask = offset < N                          # 排除越界的尾部元素
    vals = ct.gather(data, offset)             # 每个元素是一个 bin 下标（元素下标式 gather）
    ones = ct.full((TILE,), 1, dtype=ct.int32)
    mem = counts.get_raw_memory()
    # 对每个 bin 下标原子 +1；mask=False 处（越界）不操作
    mem.atomic_add_offset(vals, ones, mask=mask)

# host 端
data = torch.randint(0, BINS, (N,), dtype=torch.int32, device="cuda")
counts = torch.zeros(BINS, dtype=torch.int32, device="cuda")
stream = torch.cuda.current_stream()
grid = (ct.cdiv(N, TILE),)
ct.launch(stream, grid, histogram, (data, counts, N, BINS, TILE))

ref = torch.bincount(data.cpu(), minlength=BINS)
print("matches torch.bincount:", torch.equal(counts.cpu(), ref))
```

**操作步骤与待验证项**：

1. 确认 `data` 的所有元素都落在 `[0, BINS)` 内（这是 `vals` 作为 bin 下标合法的前提）。
2. 运行并与 `torch.bincount` 对比，预期 `matches torch.bincount: True`。
3. 把 `atomic_add_offset` 换成「gather 旧值 → 加一 → scatter 新值」的非原子写法，观察计数总和**小于** `N` 且每次结果不同，亲历数据竞争。
4. 用 `CUDA_TILE_DUMP_TILEIR=1`（见 u8-l5）dump IR，确认中间产物里出现了带 `device`（默认 scope）的 `atomic_rmw_tko` 指令。

> 上述内核改编自测试用例（[test/test_atomic.py:471-492](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_atomic.py#L471-L492)），结构可信；具体数值与可运行性请以本地验证为准。

**讨论（为何需要原子操作）**：

- **正确性**：`counts` 是多 block 共享写目标。非原子「读—改—写」会让两个 block 的「读」都拿到同一旧值、再各自写回，导致一次累加被吞掉（经典 lost update）。原子操作保证「读—改—写」不可分割。
- **逐元素原子足够**：不同 bin 互不干扰；同一 bin 的多次累加顺序未指定，但求和与顺序无关。
- **scope 选 `DEVICE`**：所有 block 在同一 GPU 上，`DEVICE` 是覆盖它们的最小 scope，开销低于 `SYS`。
- **order 选默认/`RELAXED`**：直方图不需要可见性顺序，不需要更强的 acquire/release。

## 6. 本讲小结

- cuTile 的内存模型**允许编译器/硬件重排访存**，跨 block 的访存顺序默认不保证；block 内部由集体运算边界隐式同步。
- 定序靠两个维度：**`MemoryOrder`**（WEAK/RELAXED/ACQUIRE/RELEASE/ACQ_REL，描述单次操作多强的顺序保证）与 **`MemoryScope`**（NONE/BLOCK/CLUSTER/DEVICE/SYS，描述对哪些 block 生效）；同步是**逐元素**粒度。
- `load/store` 默认 `WEAK`+`NONE`；原子 RMW 默认 `ACQ_REL`+`DEVICE`。**非 WEAK 的 order 必须配非 NONE 的 scope**，原子操作**不允许 WEAK**——这两条非法组合会被编译期校验拒绝。
- 原子操作族 `ct.atomic_add/cas/xchg/max/min/and/or/xor` 沿用 `gather/scatter` 的**元素下标**约定，返回**旧值**，逐元素原子、整批非原子；另有 `RawArray.atomic_*_offset`（带 `mask`）与 `TiledView.atomic_store_*` 两套等价入口。
- cuTile **没有独立的 `ct.fence()`**；栅栏的职责由配对的 `RELEASE`-store 与 `ACQUIRE`-load（或带 `ACQ_REL` 的原子操作）承担——这就是「fence 在 cuTile 里的实现方式」。
- 直方图是原子操作的典型用例：用 `atomic_add` 把多 block 的局部计数累加到全局 `counts`，逐元素原子保证不丢更新，累加结合律使得无需顺序保证。

## 7. 下一步学习建议

- **深入 IR 视角**：本讲多次提到原子操作在字节码里是 `atomic_rmw_tko`（token-ordered）。token 链如何为内存操作定序、保证 GPU 内存模型正确性，请看 **u6-l3「内存序 Token 排序」**——它会解释 `tko` 后缀的由来，以及为什么原子操作天然带着一个 token。
- **IR 优化的前提**：理解数据流分析如何追踪别名与整除性，有助于你判断「哪些访存可以被安全重排」，见 **u6-l2「数据流分析与整除性传播」**。
- **调试与可见性**：本讲的 dump 实践依赖 `CUDA_TILE_DUMP_TILEIR` 等环境变量，系统讲解见 **u8-l5「调试、性能与开发者工具」**。
- **更底层的内存空间**：本讲的 `MemoryScope` 是「定序范围」，与 `MemorySpace`（GLOBAL/SHARED/...，[src/cuda/tile/_memory_model.py:55-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_memory_model.py#L55-L79)）不同——后者描述地址空间，是后续讲 shared memory 与指针运算时才会深入的话题。
