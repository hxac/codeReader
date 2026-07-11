# 分块 KV 缓存与 BlockManager

## 1. 本讲目标

本讲是「PyTorch 后端执行与调度」单元的收尾篇，专门回答一个底层问题：**KV cache 这块巨大的显存，到底是怎么被切分、分配、回收的？**

学完本讲你应当能够：

- 说清「逻辑块 / 物理块 / block table」三者的关系，理解 Paged Attention 的显存组织方式。
- 读懂 `block.py` 中的 `LogicalTokenBlocks`，理解一条序列如何用一串逻辑块编号描述自己的 KV。
- 读懂 `base_block_manager.py` 中的 `PhysicalAllocator` / `LogicalAllocator` / `BaseBlockManager`，理解物理地址与逻辑地址的两层映射、引用计数与回收。
- 读懂 `default_block_manager.py` 中 `DefaultBlockManager` 的 `allocate / free / try_swap_out / try_swap_in` 四类关键方法。
- 用 `block_size`、层数、KV 头数估算一个模型在固定显存预算下能容纳多少 token。

本讲承接 u4-l4（调度器），把调度器里反复出现的 `block_manager.allocate / free / get_block_table` 落到真实代码上。

## 2. 前置知识

### 2.1 为什么要把 KV cache 切成块

Transformer 自回归生成时，每生成一个 token，**每层**都要存它的 Key、Value 向量，供后续 token 做 attention 时复用。这部分缓存就叫 **KV cache**。

最朴素的存法是「每个请求独占一段连续显存」。问题在于：请求长度事先未知，预先分配会浪费；请求中途变长需要扩容，连续内存很难原地扩。

**Paged Attention** 借鉴操作系统的虚拟内存分页思想解决它：

- 把 KV cache 切成固定大小的**块（block）**，每块存 `block_size` 个 token 的 KV。
- 一个请求的 KV 由「一串块」拼成，这些块在物理显存里**不必连续**。
- 用一张 **block table**（块表）记录「逻辑第几块 → 物理第几块」的映射，attention kernel 按表取数。

这样：显存按需领取、碎片极小；请求可以随时追加块；多个请求还能**共享**相同前缀的块（前缀缓存的基础）。

### 2.2 两类「地址」与三层对象

为避免混淆，先约定术语（本讲会反复用到）：

| 术语 | 含义 | 谁管理 |
| --- | --- | --- |
| 逻辑块（logical block） | 一条序列视角下「我的第 0、1、2… 块」 | `LogicalTokenBlocks`（block.py） |
| 物理块（physical block） | KV cache 大池子里真实的第 N 个槽位 | `PhysicalAllocator`（base_block_manager.py） |
| block table | 逻辑块号 → 物理块号的映射数组 | `LogicalAllocator.phy_map` |
| 引用计数（ref_count） | 一个逻辑块被多少条序列共享，归 0 才真正释放 | `LogicalAllocator.ref_count` |

三层对象自顶向下：**序列持逻辑块 → BlockManager 经 LogicalAllocator 把逻辑块映射到物理块 → PhysicalAllocator 在物理池里分配/回收**。

### 2.3 与调度器的关系（承接 u4-l4）

u4-l4 讲过：调度器（`scheduler.py`）只做决策，不碰张量；它手里攥着三个 KV 家当——`block_manager`、`block_trie`、`state_manager`。本讲专攻 `block_manager`（物理块管家）；`block_trie`（前缀缓存 trie）留给 u9-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/pytorch/block.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py) | 定义 `LogicalTokenBlocks`：序列视角下「我占用了哪些逻辑块」的动态数组。 |
| [lmdeploy/pytorch/paging/block_manager/base_block_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py) | 定义物理池分配器 `PhysicalAllocator`、逻辑层映射器 `LogicalAllocator`、抽象基类 `BaseBlockManager`。 |
| [lmdeploy/pytorch/paging/block_manager/default_block_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py) | `DefaultBlockManager`：默认实现，提供 `allocate_msg / free / try_swap_out / try_swap_in`。 |
| [lmdeploy/pytorch/paging/block_manager/\_\_init\_\_.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/__init__.py) | 工厂 `build_block_manager`：按 `window_size` 选 Default 或 Window 两种实现。 |
| lmdeploy/pytorch/paging/scheduler.py（调度器） | BlockManager 的调用方：在 prefill/decode 中调 `allocate`、在驱逐中触发 `free`。 |
| lmdeploy/pytorch/paging/seq_states/states.py | 序列状态机里 `_free_seq` 调 `block_manager.free(seq)`。 |

## 4. 核心概念与源码讲解

### 4.1 Block：逻辑块 LogicalTokenBlocks

#### 4.1.1 概念说明

一条序列（`SchedulerSequence`）需要记录「我已经占用了哪些 KV 块」。这件事用一个对象描述：`LogicalTokenBlocks`。它本质上是一个**可动态扩容、只记逻辑块号**的整数数组。

注意：`LogicalTokenBlocks` **不存任何 KV 数据**，只存「块号」。真正的张量在物理池里。这就像进程的虚拟页表只记页号，真正数据在物理内存条上。

#### 4.1.2 核心流程

一条序列的生命周期里，逻辑块表是这样变化的：

1. **新建序列**：逻辑块表为空（`_num_real == 0`）。
2. **调度器决定给它分块**：`block_manager.allocate_msg` 算出需要 N 块，把 N 个逻辑块号 `append` 进来。
3. **decode 继续生成**：每写满一个块（`block_size` 个 token），调度器再 `append` 一块。
4. **被驱逐 / 结束**：`reset()` 清空逻辑块表（物理块由 `LogicalAllocator.free` 按 ref_count 回收）。

由于块数会随生成长度增长，`LogicalTokenBlocks` 用「**预分配 + 计数**」策略：底层 numpy 数组按 `ALLOC_SIZE=128` 倍数扩张，但对外只暴露前 `_num_real` 个有效块号，避免频繁 realloc。

#### 4.1.3 源码精读

整个类定义在 [lmdeploy/pytorch/block.py:L15-L74](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L15-L74)。关键片段：

```python
class LogicalTokenBlocks:
    ALLOC_SIZE = 128

    def __init__(self, blocks: np.ndarray = None):
        if blocks is None:
            self._blocks = np.zeros((self.ALLOC_SIZE, ), dtype=np.int64)
            self._num_real = 0
        ...
```

- `ALLOC_SIZE = 128`：每次扩张以 128 块为粒度（[block.py:L17](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L17)）。
- `_num_real`：真正有效的块数；`__len__` 返回它（[block.py:L57-L59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L57-L59)）。

`get_real_blocks` 把「底层数组」裁成「有效块号视图」，是所有对外访问的入口（[block.py:L44-L46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L44-L46)）：

```python
def get_real_blocks(self):
    return self._blocks[:self._num_real]
```

`append` 是调度器最常调的方法——往尾部追加若干新逻辑块号（[block.py:L48-L55](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L48-L55)）：

```python
def append(self, blocks: np.ndarray):
    num_blocks = len(blocks)
    self.reserve(num_blocks + self._num_real)   # 不够先扩容
    slice_start = self._num_real
    slice_end = slice_start + num_blocks
    self._num_real += num_blocks
    self._blocks[slice_start:slice_end] = blocks
```

`reserve` 在容量不足时用 `np.pad` 把数组补长，补到 128 的倍数（[block.py:L28-L34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L28-L34)）；`reset` 则把 `_num_real` 归零（[block.py:L66-L68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L66-L68)），用于释放。

> 提示：`SchedulerSequence.logical_blocks` 字段就是这个类型（[messages.py:L715](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L715)），而 `seq.num_blocks` 就是 `len(logical_blocks)`（[messages.py:L866-L869](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L866-L869)）。

#### 4.1.4 代码实践

**实践目标**：直观感受逻辑块表的「预分配 + 计数」行为。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：在能 import lmdeploy 的环境里运行
import numpy as np
from lmdeploy.pytorch.block import LogicalTokenBlocks

lb = LogicalTokenBlocks()          # 空表
print(len(lb))                     # 0
print(lb._blocks.size)             # 128（底层预分配了 128 格）

lb.append(np.array([10, 11, 12]))  # 追加 3 个逻辑块号
print(len(lb))                     # 3
print(lb.get_real_blocks())        # [10 11 12]

lb.reset()
print(len(lb))                     # 0
```

**需要观察的现象**：`get_real_blocks()` 始终只返回有效部分，底层 `_blocks` 数组长度始终是 128 的整数倍。

**预期结果**：见注释中的输出。若本地无运行环境，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用 Python `list` 存逻辑块号，而要用 numpy 数组 + 计数？

**答案**：调度器每步都要对成百上千条序列做批量分配/查询，numpy 数组向量化、连续内存、可与其它 numpy 操作（如 `phy_map[logical_address]`）无缝衔接；计数式扩容避免每 `append` 一次就 realloc 一次。

**练习 2**：`reset()` 之后底层 `_blocks` 数组是否被清零？

**答案**：没有。`reset` 只把 `_num_real` 设为 0（[block.py:L66-L68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/block.py#L66-L68)），旧数据仍在内存里，只是被「视作无效」，下次 `append` 会覆盖。

---

### 4.2 BaseBlockManager：物理/逻辑双层分配器

#### 4.2.1 概念说明

`BaseBlockManager` 是抽象基类，它**不真正分配显存**（真正显存是 `CacheEngine` 一次性申请的大张量），而是**管理「块号」这个抽象资源**：哪些块空闲、哪些被占、被谁引用。文件注释直白点明这点：

> The allocator won't allocate real memory. It is used to support block manager.

这一层有三个核心类，职责分明：

| 类 | 职责 |
| --- | --- |
| `LogicalMemory` | 三个并列数组：`phy_map`（逻辑→物理映射）、`ref_count`（引用计数）、`access_time`（访问时间戳，供 LRU 驱逐）。 |
| `PhysicalAllocator` | 一个物理池的分配/回收器。它管理**连续编号的物理块号区间**，用「栈式」复用回收空闲块。 |
| `LogicalAllocator` | 把 GPU、CPU 两个物理池 + 一个逻辑地址空间缝合起来，对外暴露 `allocate / free / get_physical_blocks`。 |

为什么分 GPU/CPU 两个物理池？为了支持 **swap（换入换出）**：显存吃紧时把某些序列的 KV 换到 CPU 内存（swap out），需要时再换回来（swap in）。

#### 4.2.2 核心流程

**分配一条序列的若干物理块**（`LogicalAllocator.allocate`，[base_block_manager.py:L93-L110](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L93-L110)）：

1. 同时检查「逻辑地址空间有空位」和「目标设备的物理池有空位」两个条件。
2. 从逻辑空闲池取 N 个逻辑块号。
3. 从指定设备（gpu/cpu）的 `PhysicalAllocator` 取 N 个物理块号。
4. 在 `phy_map` 里建立「这 N 个逻辑块 → 这 N 个物理块」的映射。
5. 这 N 个逻辑块引用计数置 1，更新访问时间。

**释放**（`LogicalAllocator.free`，[base_block_manager.py:L112-L136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L112-L136)）：

1. 先把引用计数减 1。
2. 只有 **ref_count 归 0** 的逻辑块才真正回收——这是「前缀缓存共享」的关键：被多条序列共享的块，一条结束不会让另一条丢数据。
3. 归 0 的逻辑块：逻辑地址空间里标记空闲，并按物理块号落入 GPU 池还是 CPU 池，分别还给对应的 `PhysicalAllocator`。

**物理块号如何区分 GPU/CPU**：`LogicalAllocator` 用一个 `cpu_mem_offset` 分界——物理块号 `< offset` 的属于 GPU，`>= offset` 的属于 CPU（[base_block_manager.py:L74-L77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L74-L77), [L163-L175](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L163-L175)）。

#### 4.2.3 源码精读

**LogicalMemory**（[base_block_manager.py:L9-L27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L9-L27)）——三个并列数组就是全部状态：

```python
class LogicalMemory:
    def __init__(self, num_blocks: int) -> None:
        self.phy_map: np.ndarray = np.zeros(self._num_blocks, dtype=np.int64)
        self.ref_count: np.ndarray = np.zeros((self._num_blocks, ), dtype=np.int64)
        self.access_time: np.ndarray = np.zeros((self._num_blocks, ), dtype=np.int64)

    def get_physical_blocks(self, logical_address: np.ndarray):
        return self.phy_map[logical_address]   # 一次花式索引即查表
```

`get_physical_blocks` 就是一次 numpy 花式索引：给一批逻辑块号，返回对应物理块号——这正是 attention kernel 取数前要准备 **block table** 的来源。

**PhysicalAllocator**（[base_block_manager.py:L30-L65](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L30-L65)）——栈式分配器：

```python
def __init__(self, num_blocks: int, offset: int = 0):
    self._free_blocks = np.arange(num_blocks, dtype=np.int64) + offset
    self._free_count = num_blocks

def allocate(self, num_blocks: int):
    if self.get_num_free_blocks() >= num_blocks:
        num_used = self._num_blocks - self._free_count
        blocks = self._free_blocks[num_used:num_used + num_blocks]  # 从头部取
        self._free_count -= num_blocks
        return blocks
    else:
        raise MemoryError('No enough free memory blocks.')
```

巧妙之处：`_free_blocks` 在初始化时一次性排好 `[offset, offset+1, ...]`，`allocate` 从「已用末尾」往后切，`free` 把回收的块塞回末尾——一个数组 + 一个游标 `_free_count` 就完成了栈式分配/回收，无需链表。

**LogicalAllocator.free** 的引用计数判断（[base_block_manager.py:L112-L136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L112-L136)）：

```python
def free(self, blocks: np.ndarray):
    self.add_ref_count(blocks, -1)          # 先减引用
    ref_count = self.get_ref_count(blocks)
    freed_blocks = blocks[ref_count == 0]   # 只回收归 0 的
    ...
    phy_blocks = self.get_physical_blocks(freed_blocks)
    cpu_blocks = phy_blocks[phy_blocks >= self._cpu_mem_offset]
    gpu_blocks = phy_blocks[phy_blocks < self._cpu_mem_offset]
    if len(cpu_blocks) > 0:
        self._cpu_allocator.free(cpu_blocks)
    if len(gpu_blocks) > 0:
        self._gpu_allocator.free(gpu_blocks)
```

**BaseBlockManager 抽象基类**（[base_block_manager.py:L201-L268](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L201-L268)）：构造期创建 `LogicalAllocator`（[L209-L215](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L209-L215)），并把 `num_required_blocks / can_allocate / allocate_msg / free / try_swap_out / try_swap_in` 声明为抽象（`raise NotImplementedError`），留给子类实现。真正有实现的是 `get_block_table`：

```python
def get_block_table(self, msg: SchedulerSequence):
    logical_blocks = msg.logical_blocks
    return self.allocator.get_physical_blocks(logical_blocks.get_real_blocks())
```

即「把序列的有效逻辑块号，翻译成物理块号数组」——这组物理块号就是喂给 attention kernel 的 block table。调度器在 `get_block_tables` 里批量调用它（[scheduler.py:L864-L866](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L864-L866)）。

#### 4.2.4 代码实践

**实践目标**：手工构造一个 `PhysicalAllocator`，观察「栈式分配/回收」。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from lmdeploy.pytorch.paging.block_manager.base_block_manager import (
    PhysicalAllocator, LogicalAllocator)

alloc = PhysicalAllocator(num_blocks=4, offset=0)
print(alloc.get_num_free_blocks())          # 4
b = alloc.allocate(2)                       # 取 2 块
print(b)                                    # [0 1]
print(alloc.get_num_free_blocks())          # 2
alloc.free(b)                               # 还回去
print(alloc.get_num_free_blocks())          # 4
```

**需要观察的现象**：分配从 `_free_blocks` 头部切，回收塞回尾部；空闲计数随之增减。

**预期结果**：见注释输出。本地若无环境则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`LogicalAllocator.free` 为什么要先 `add_ref_count(blocks, -1)` 再判断 `ref_count == 0`？

**答案**：因为块可能被多条序列共享（前缀缓存）。直接回收会让仍持有该块的序列读到被覆盖的 KV。引用计数归 0 才说明「没人需要这块了」，可安全回收。

**练习 2**：`LogicalMemory` 为什么把 `phy_map`、`ref_count`、`access_time` 做成三个**并列**的 numpy 数组，而不是一个结构体数组？

**答案**：并列同类型数组（SoA，structure of arrays）便于对「一批块」做向量化索引与运算（如 `phy_map[logical_address]`、`ref_count == 0` 布尔掩码），比逐元素访问结构体快得多。

---

### 4.3 DefaultBlockManager：分配 / 释放 / 驱逐 / 换入换出

#### 4.3.1 概念说明

`DefaultBlockManager` 继承 `BaseBlockManager`，实现了那几个抽象方法，是默认的块管家（`window_size < 0` 时由工厂选中，见 [\_\_init\_\_.py:L20-L26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/__init__.py#L20-L26)）。

它解决调度器提出的四个问题：

| 调度器的问题 | 方法 |
| --- | --- |
| 这条序列还差几块？需要的话放得下吗？ | `num_required_blocks` / `can_allocate` |
| 给这条序列补上缺的物理块 | `allocate_msg`（别名 `allocate`） |
| 这条序列结束了，回收它的块 | `free` |
| 显存满了，把它换到 CPU / 换回 GPU | `try_swap_out` / `try_swap_in` |

注意一个设计要点：`allocate_msg` **只追加序列还缺的块**，而不是「按当前长度重分」。增量分配让 decode 每步只补 0 或 1 块，开销恒定。

#### 4.3.2 核心流程

**计算「还缺几块」**（[default_block_manager.py:L25-L34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L25-L34)）：

```
需要的总块数 = ceil(全部 token 数 / block_size)
还缺的块数  = 总块数 - 已有逻辑块数
```

其中「全部 token 数」取 `num_all_ids`，并可被 `kv_token_limit`（KV 长度上限）封顶，再加上 `prealloc_size`（预分配，用于投机解码等多写 token 的场景）。

**分配**（`allocate_msg`，[default_block_manager.py:L42-L49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L42-L49)）：缺几块就从 GPU 物理池要几块，`append` 到序列的逻辑块表。

**释放**（`free`，[default_block_manager.py:L51-L54](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L51-L54)）：把序列全部有效逻辑块交给 `LogicalAllocator.free`（按引用计数回收），再 `reset()` 清空序列的逻辑块表。

**swap out / in**（[L56-L100](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L56-L100), [L102-L146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L102-L146)）：都遵循「先 `_can_swap` 检查，再 `_do_swap` 执行」两段式。检查条件包括：序列非空、整序列在同一设备、目标设备有空闲块、且**引用计数全为 1（未被共享）**——共享块不允许换出，以免破坏前缀缓存。

#### 4.3.3 源码精读

`num_required_blocks`（[default_block_manager.py:L25-L34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L25-L34)）：

```python
@classmethod
def num_required_blocks(cls, obj: SchedulerSequence, prealloc_size: int = 0):
    num_tokens = obj.num_all_ids
    if obj.kv_token_limit is not None:
        num_tokens = min(num_tokens, obj.kv_token_limit)
    num_tokens += prealloc_size
    num_all_blocks = _div_up(num_tokens, obj.block_size)   # 向上取整
    return max(0, num_all_blocks - len(obj.logical_blocks))  # 减去已有
```

`_div_up(x, n) = (x + n - 1) // n` 即向上取整（[default_block_manager.py:L9-L11](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L9-L11)）。

`can_allocate` 仅做一次比较（[default_block_manager.py:L36-L40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L36-L40)）：

```python
def can_allocate(self, msg, prealloc_size=0):
    num_required_blocks = self.num_required_blocks(msg, prealloc_size)
    num_free_phy = self.get_num_free_gpu_blocks()
    return num_required_blocks <= num_free_phy
```

`allocate_msg`（[default_block_manager.py:L42-L49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L42-L49)）：

```python
def allocate_msg(self, msg, prealloc_size=0):
    logical_blocks = msg.logical_blocks
    num_required_blocks = self.num_required_blocks(msg, prealloc_size)
    if num_required_blocks > 0:
        blocks = self.allocator.allocate(num_required_blocks, 'gpu')
        logical_blocks.append(blocks)
```

**调度器如何调用**（承接 u4-l4）：在 `_schedule_decoding` 里，对每条 running 序列先算 `num_required_blocks`，断言「序列需求不超过物理总块数」，驱逐到够为止，最后调用一次 `allocate`（[scheduler.py:L765-L779](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L765-L779)）：

```python
num_required_blocks = self.block_manager.num_required_blocks(seq, prealloc_size)
assert seq.num_blocks + num_required_blocks <= self.block_manager.num_gpu_blocks, (
    'Sequence requires more blocks than total gpu blocks.')
while not __evict_for_seq(seq, num_required_blocks):
    ...
if self.block_manager.get_num_free_gpu_blocks() < num_required_blocks:
    seq.state.evict(); continue
self.block_manager.allocate(seq, prealloc_size)
self.block_trie.allocate(seq)
```

**释放路径**：序列状态机里的 `_free_seq` 在驱逐/结束时调用 `block_manager.free(seq)`（[states.py:L10-L25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/seq_states/states.py#L10-L25)），而真正的驱逐决策在 `RecomputeEvictionHelper._evict_for_seq_default`：先释放可驱逐序列的块，再视情况驱逐前缀缓存 trie 里的块（[recompute_eviction_helper.py:L19-L58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/eviction_helper/recompute_eviction_helper.py#L19-L58)）。

> 补充：物理块总数 `num_gpu_blocks` 从哪来？由 `CacheConfig.num_gpu_blocks` 给出（[config.py:L107-L129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L107-L129)），引擎启动时根据剩余显存与每块字节数反推（见 4.3.4）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：(a) 在源码里列出 `DefaultBlockManager` 的 allocate/free/evict 关键方法；(b) 用 `block_size` 概念估算一个 8B 模型在 1 GB KV 预算下能容纳多少 token。

**操作步骤 1（源码阅读型）**：打开 [default_block_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py)，把下表填全：

| 功能 | 方法名 | 行号 |
| --- | --- | --- |
| 算还缺几块 | `num_required_blocks` | L25-34 |
| 能否放下 | `can_allocate` | L36-40 |
| 分配（补块） | `allocate_msg` / `allocate`（基类别名） | L42-49 / base L252-254 |
| 释放 | `free` | L51-54 |
| 驱逐（触发 free） | 不在 manager，而在 `RecomputeEvictionHelper` 调 `seq.state.free()` | eviction_helper L19-58 |
| 换出到 CPU | `try_swap_out` | L56-100 |
| 换回 GPU | `try_swap_in` | L102-146 |

注意：`DefaultBlockManager` **没有** `evict` 方法——驱逐是调度器 + eviction_helper 的职责，manager 只负责「被通知释放」。

**操作步骤 2（估算型）**：每块 KV cache 字节数由 `CacheEngine.get_cache_block_size` 给出（[cache_engine.py:L432-L450](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/cache_engine.py#L432-L450)），它本质是「一个 block（`block_size` 个 token）在所有层、所有 KV 头上的 K+V 张量字节数」。每块的 K/V 形状是 `(block_size, num_kv_heads / world_size, head_size)`（[cache_engine.py:L181-L207](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/cache_engine.py#L181-L207)），于是：

\[ \text{bytes\_per\_block} = \text{num\_layers} \times 2_{(K+V)} \times \text{block\_size} \times \frac{\text{num\_kv\_heads}}{\text{world\_size}} \times \text{head\_dim} \times \text{element\_size} \]

反推物理块数的逻辑在 executor 里（[executor/base.py:L162-L173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/base.py#L162-L173), [L343-L359](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/base.py#L343-L359)）：`num_gpu_blocks = available_mem // bytes_per_block`，且 `available_mem = free_mem * cache_max_entry_count`（默认 0.8，[config.py:L117](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L117)）。

以 **Llama-3-8B**（`num_layers=32`, `num_kv_heads=8`（GQA）, `head_dim=128`, `block_size=64`, fp16 即 2 字节, 单卡 `world_size=1`）为例，示例代码：

```python
# 示例代码：估算 1GB KV 预算能装多少 token
num_layers, num_kv_heads, head_dim = 32, 8, 128
block_size, element_size = 64, 2          # fp16
budget = 1 << 30                          # 1 GiB

bytes_per_block = num_layers * 2 * block_size * num_kv_heads * head_dim * element_size
# = 32*2*64*8*128*2 = 8,388,608 = 8 MiB
num_blocks = budget // bytes_per_block     # 128
num_tokens = num_blocks * block_size       # 8192
print(f'每块 {bytes_per_block/1024/1024:.1f} MiB, '
      f'1GiB 可装 {num_blocks} 块 = {num_tokens} tokens')
```

**需要观察的现象 / 预期结果**：每块 8 MiB，1 GiB 可装 128 块 = **8192 tokens**。换言之，Llama-3-8B 大约每 128 KiB 显存放 1 个 token 的 KV。换不同模型（KV 头数/层数不同）结果会变。

**重要说明**：上述数字取决于具体模型配置（GQA 的 KV 头数是关键变量）；若启用 KV cache 量化（`quant_policy`，u2-l3 讲过）则 `element_size` 更小、可装更多 token。真实部署请以引擎启动日志里的 `num_gpu_blocks` 为准。若本地无对应模型，标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`num_required_blocks` 为什么要 `max(0, num_all_blocks - len(logical_blocks))`？

**答案**：因为分配是「增量」的——序列可能已经持有若干逻辑块（前缀缓存命中或上一步已分），只需补齐差额；若已有块数已满足（差额为负），返回 0 表示无需新分配。

**练习 2**：`try_swap_out` 为什么拒绝换出「引用计数不为 1」的序列？

**答案**：引用计数 >1 表示该块被多条序列共享（前缀缓存）。换出会改写其物理块号映射，破坏其它序列正在引用的 KV，所以只允许换出独占（ref_count 全 1）的序列（[default_block_manager.py:L80-L82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/default_block_manager.py#L80-L82)）。

**练习 3**：把 `block_size` 从 64 调成 128，单块字节数和「可容纳 token 数」分别怎么变（显存预算固定）？

**答案**：单块字节数翻倍（每块装 128 token）；可容纳块数减半，但「块数 × block_size = 可容纳 token 数」基本不变。块更大的代价是**内部碎片**更严重（短序列浪费更多），收益是 block table 更短、调度开销更低。

## 5. 综合实践

把本讲三块知识串起来，做一个**纯推理型**的 KV 容量测算（无需 GPU）：

1. **读配置**：打开 [config.py 的 CacheConfig](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L107-L129)，记下 `block_size`、`num_gpu_blocks`、`num_cpu_blocks`、`cache_max_entry_count`、`window_size`、`num_reserved_gpu_blocks` 六个字段的默认值与含义。
2. **读工厂**：打开 [block_manager/\_\_init\_\_.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/__init__.py#L8-L26)，说明 `window_size` 如何在 `DefaultBlockManager` 与 `WindowBlockManager` 之间二选一。
3. **画映射链**：画出一条序列从「`seq.logical_blocks`（逻辑块号）→ `LogicalAllocator.phy_map`（逻辑→物理）→ `PhysicalAllocator`（物理池）→ `CacheEngine` 真实张量」的四层关系。
4. **测算**：选一个你熟悉的模型（查它的 `num_hidden_layers`、`num_key_value_heads`、`head_dim`），代入 4.3.4 的公式，估算「24 GB 显存里留给 KV 的部分（默认 0.8）能容纳多少并发 token」。
5. **验证思路**：若本地有 GPU，启动引擎后开 `LMDEPLOY_LOG_LEVEL=DEBUG`，在日志里找 `num_gpu_blocks` 的真实值，与你估算的块数对比；说明差异可能来自 `runtime_cache_size` 预留、`num_reserved_gpu_blocks`、量化等。

完成后再回到 [scheduler.py:L765-L779](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L765-L779)，你应该能一眼看懂那段「算需求 → 断言 → 驱逐 → 分配」的逻辑了。

## 6. 本讲小结

- KV cache 被 Paged Attention 切成固定大小的**块**：序列持「逻辑块号」，物理池存真实张量，二者由 `phy_map`（block table）映射。
- `LogicalTokenBlocks`（block.py）只是「带预分配计数的整数数组」，记录序列占用的逻辑块号；`append / reset` 对应增块 / 释放。
- `PhysicalAllocator` 用「数组 + 游标」做栈式分配/回收，`LogicalAllocator` 把 GPU/CPU 两个物理池缝进统一逻辑空间，并用 `ref_count` 实现「共享块只有归 0 才回收」。
- `BaseBlockManager` 定义抽象接口（`get_block_table` 有实现，其余抽象），`DefaultBlockManager` 给出默认实现：`num_required_blocks` 算差额、`allocate_msg` 增量补块、`free` 按引用计数回收、`try_swap_out/in` 做 GPU↔CPU 换页。
- **驱逐不是 BlockManager 的职责**——它由调度器 + `RecomputeEvictionHelper` 决策，通过 `seq.state.free()` → `block_manager.free(seq)` 触发回收。
- 每块 KV 字节数 ≈ `num_layers × 2 × block_size × (num_kv_heads/tp) × head_dim × element_size`，引擎启动时用它从剩余显存反推 `num_gpu_blocks`。

## 7. 下一步学习建议

- **u9-l3 Prefix 缓存与 BlockTrie**：本讲的 `ref_count` 共享机制是前缀缓存的物理基础，下一站去看 `BlockTrie` 如何在 token 级别匹配共享前缀、如何与 `block_manager.allocate` 协作。
- **u9-l4 张量并行**：本讲公式里的 `num_kv_heads / world_size` 体现了 TP 对 KV cache 的切分，结合 `distributed.py` 理解多卡下每卡分到多少块。
- **TurboMind 对照阅读**：可对照 `src/turbomind/engine/BlockManager.h`（u6-l1 提到），看 C++ 后端的块管理与本讲 Python 版的异同。
- **源码延伸**：若对换页细节感兴趣，可读 `window_block_manager.py`（滑动窗口注意力下的块管理）与 `executor/base.py` 的 `_update_num_gpu_blocks`，理解显存预算的完整求解过程。
