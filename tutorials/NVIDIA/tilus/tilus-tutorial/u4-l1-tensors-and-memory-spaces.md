# 张量与内存空间全景

## 1. 本讲目标

本讲是「布局系统（Layout System）」单元的第一篇。在 u3-l4 里，我们已经知道 Tilus IR 里有四种张量（`RegisterTensor` / `SharedTensor` / `GlobalTensor` / `TMemoryTensor`），也知道所有 IR 节点用身份相等。本讲要在这个基础上回答三个更具体的问题：

1. 这四种张量分别住在 GPU 的**哪一层内存**里？它们的生命周期和访问规则有什么不同？
2. 什么是 `optional_layout`？为什么有些张量可以「先创建、后绑定布局」（延迟绑定），有些却不能？
3. `shape` 和 `layout` 到底谁拥有谁？为什么 `GlobalTensor` 连一个 `shape` 字段都没有？

学完后，你应该能在读源码或写内核时，一眼判断某个张量住在哪层内存、它的布局是已经确定还是待推理、以及它的形状从哪里来。这是后续 u4-l2（RegisterLayout）、u4-l3（Shared/Global/TMemory Layout）、u4-l5（布局自动推理）的地基。

## 2. 前置知识

阅读本讲前，你需要大致了解以下几点（前几讲已建立）：

- **GPU 的内存层次**：一块 GPU 上，线程能访问的存储从快到慢大致是：寄存器（register，每个线程私有，最快最小）→ 共享内存（shared memory，片上 SRAM，整个线程块共享）→ 全局内存 / 显存（global memory / DRAM，所有线程块都能访问，最大但最慢）。本讲还会引入第四种：**张量内存（Tensor Memory, TMEM）**，它是 Blackwell（sm_100+）独有的、专供第五代张量核使用的片上存储。
- **线程块（thread block）视角**：Tilus 是 tile-level DSL，一个张量变量「属于整个线程块」（见 [docs/source/programming-guides/type-system/__init__.rst:10-11](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/type-system/__init__.rst#L10-L11)），即使它是标量。
- **不可变 IR 与身份相等**：所有 IR 节点是 `frozen dataclass`，`__eq__`/`__hash__` 基于对象 `id`（见 [python/tilus/ir/node.py:18-31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py#L18-L31)）。修改 IR 永远是「生成一个新对象」，这在布局延迟绑定时尤其关键。
- **四种张量的基本存在**（u3-l4）：四种 Tensor 都是 `Tensor` 的子类，每种对应一层内存。

一句话术语表：

| 术语 | 含义 |
|------|------|
| 寄存器张量 `RegisterTensor` | 分布在线程块各线程寄存器里的张量 |
| 共享张量 `SharedTensor` | 片上共享内存里的张量 |
| 全局张量 `GlobalTensor` | 显存（DRAM）里的张量 |
| 张量内存张量 `TMemoryTensor` | Blackwell TMEM 里的张量 |
| 布局 `Layout` | 描述「多维逻辑下标 → 物理存储位置」的映射 |

## 3. 本讲源码地图

本讲只围绕「张量类型」这一组数据结构展开，核心源码非常集中：

| 文件 | 作用 |
|------|------|
| [python/tilus/ir/tensor.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py) | 四种 `Tensor` 子类的定义，是本讲的主角 |
| [python/tilus/ir/node.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py) | `IRNode` 基类，定义身份相等 |
| [python/tilus/ir/layout/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/__init__.py) | 四种 Layout 的导出汇总 |
| [python/tilus/ir/layout/register_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py) | `RegisterLayout` 定义与 `local_size` |
| [python/tilus/ir/layout/global_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py) | `GlobalLayout` 定义（shape 可为符号表达式） |
| [python/tilus/lang/instructions/root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py) | 用户侧创建张量的指令：`register_tensor` / `shared_tensor` / `global_view` / `free_shared` |
| [docs/source/programming-guides/type-system/](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/type-system/__init__.rst) | 官方类型系统文档（每类张量一页） |
| [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py) | 一次同时用上 Global/Shared/Register 三种张量的真实内核 |

---

## 4. 核心概念与源码讲解

### 4.1 四种张量与四种内存空间

#### 4.1.1 概念说明

Tilus 把 GPU 的存储抽象成四种「张量类型」，**每种张量类型一一对应一层物理内存**。理解这一层映射，是读懂任何 Tilus 内核数据流的前提：

| 张量类型 | 物理内存 | 可见范围 | 谁能算术运算 | 架构要求 |
|----------|----------|----------|--------------|----------|
| `RegisterTensor` | 寄存器（register file） | **分布式**：分布在块内各线程的私有寄存器里 | ✅ 唯一支持 `+ - * /` 等 | 全部 |
| `SharedTensor` | 共享内存（shared memory SRAM） | 整个线程块共享 | ❌ 只能 load/store | 全部 |
| `GlobalTensor` | 全局内存（DRAM） | 所有线程块共享 | ❌ 只能 load/store | 全部 |
| `TMemoryTensor` | 张量内存（TMEM） | SM 的张量核私有 | ❌ 只能 load/store/copy | Blackwell（sm_100+） |

一个非常重要的设计原则（文档反复强调）：**只有寄存器张量能做算术**。共享、全局、TMEM 张量都只是「数据的载体」，要计算必须先把数据 `load_*` 进寄存器、算完再 `store_*` 回去（见 [shared-tensor.rst:54-57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/type-system/shared-tensor.rst#L54-L57) 与 [global-tensor.rst:36-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/type-system/global-tensor.rst#L36-L39)）。这就是为什么上一讲 naive matmul 里，`load_global` 之后非要在寄存器里做 `dot`，再 `store_global` 回去。

另一条关键差异是**生命周期**：

- `RegisterTensor`：用 `register_tensor` 创建即可，随用随弃，无需显式释放。
- `SharedTensor`：必须**显式分配 + 显式释放**（`shared_tensor` / `free_shared`），因为共享内存是稀缺资源，要回收给后续 tile 复用。
- `TMemoryTensor`：同样**必须显式分配 + 显式回收**，且文档明确「所有 TMEM 必须在内核退出前 dealloc」（见 [tmemory-tensor.rst:42-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/type-system/tmemory-tensor.rst#L42-L43)）。
- `GlobalTensor`：有两种来源——`global_view`（指针视图，生命周期由外部托管，通常是 torch 张量）和 `global_tensor`（运行时分配的 workspace，生命周期等于整次内核执行，自动释放）。

#### 4.1.2 核心流程

把一个 matmul 内核的数据流画出来，就能看清四种内存如何配合（这是经典的「global → shared → register」三级搬运）：

```
DRAM (GlobalTensor)            ← load_global 把切片搬进寄存器
   │
   ▼ store_shared              ← 先落到寄存器，再 store_shared 进共享内存
Shared Memory (SharedTensor)   ← 块内线程共享，复用 A/B tile
   │
   ▼ load_shared               ← 取进寄存器
Register (RegisterTensor)      ← 唯一能做 dot / add / cast 的地方
   │
   ▼ store_global              ← 算完写回显存
DRAM (GlobalTensor)
```

Hopper/Blackwell 上还会插入 `cp_async`（异步 global→shared）和 `wgmma`/`tcgen05`（直接吃 shared/TMEM 的张量核），但「只有寄存器做算术、其余只搬运」的大原则不变。

#### 4.1.3 源码精读

先看四种张量在源码里的「身份证」——它们的 dataclass 定义与 docstring 直接写明了各自住在哪：

**RegisterTensor：分布式寄存器**（[python/tilus/ir/tensor.py:81-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L81-L97)）

```python
@dataclass(frozen=True, eq=False)
class RegisterTensor(Tensor):
    """A tensor that resides in the register memory."""
    shape: tuple[int, ...]
    optional_layout: Optional[RegisterLayout] = None
```

注意它的 docstring 没说「每个线程一份」，而是「住在寄存器里」——真正的「分布方式」由 `optional_layout` 决定（这是 4.2 的重点）。也只有 `RegisterTensor` 重载了一堆算术运算符（`__add__`/`__mul__`/…，见 [tensor.py:222-266](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L222-L266)），并且这些运算符都只抛 `RuntimeError`——它们只是给转译器看的「类型提示」，真正的语义在 Tilus Script 转译阶段才被赋予（u3-l2）。

**SharedTensor：片上共享内存**（[python/tilus/ir/tensor.py:546-562](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L546-L562)）

```python
@dataclass(frozen=True, eq=False)
class SharedTensor(Tensor):
    """A tensor that resides in the shared memory."""
    shape: tuple[int, ...]
    optional_layout: Optional[SharedLayout]
```

它额外提供了 `size`、`nbytes`、`storage_nbytes` 三个属性（[tensor.py:605-630](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L605-L630)）。注意 `storage_nbytes` 可能**大于** `nbytes`——因为 swizzle 或非紧凑布局会带来 padding，这会影响共享内存的分配量。

**TMemoryTensor：Blackwell 张量内存**（[python/tilus/ir/tensor.py:658-679](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L658-L679)）

```python
@dataclass(frozen=True, eq=False)
class TMemoryTensor(Tensor):
    """A tensor that resides in tensor memory (TMEM).
    Tensor memory is a dedicated on-chip memory available on Blackwell (SM 10.0+) GPUs,
    private to the SM's tensor cores. ... organized as a 2D structure of lanes (rows)
    and columns, with each cell being 32 bits. The number of lanes (shape[0]) must be 32, 64, or 128.
    """
    shape: tuple[int, ...]
    optional_layout: Optional[TMemoryLayout]
```

docstring 把 TMEM 的硬件结构讲得很清楚：**128 行（lane）× 512 列，每格 32 bit**，按 32 列为单位分配。这也是为什么 `create` 强制校验 `shape[0]` 必须是 32/64/128（[tensor.py:700-708](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L700-L708)）。

**GlobalTensor：显存，且 layout 是必填字段**（[python/tilus/ir/tensor.py:764-777](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L764-L777)）

```python
@dataclass(frozen=True, eq=False)
class GlobalTensor(Tensor):
    """A tensor that resides in the global memory."""
    layout: GlobalLayout          # ← 注意：是 layout，不是 optional_layout！
```

这是四种张量里**唯一一个没有 `optional_layout`、没有 `shape` 字段的**——它的布局是必填的，形状从布局里推导（见 4.3）。

最后看一个把这三种张量同时用上的真实内核片段（[examples/matmul/matmul_v2.py:90-119](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L90-L119)）：

```python
ga = self.global_view(a_ptr, dtype=float16, shape=[m_size, k_size])   # GlobalTensor
gb = self.global_view(b_ptr, dtype=float16, shape=[k_size, n_size])   # GlobalTensor
sa = self.shared_tensor(dtype=float16, shape=[self.block_m, self.block_k])  # SharedTensor
sb = self.shared_tensor(dtype=float16, shape=[self.block_k, self.block_n])  # SharedTensor
acc = self.register_tensor(dtype=float32, shape=[self.block_m, self.block_n], init=0.0)  # RegisterTensor
...
    lda = self.load_global(ga, offsets=[...], shape=[...])   # global → register
    self.store_shared(sa, lda)                                # register → shared
    ...
    a = self.load_shared(sa)                                  # shared → register
    acc = self.dot(a, b, acc)                                 # 仅在 register 做算术
...
self.free_shared(sa); self.free_shared(sb)                    # shared 必须显式释放
```

这段代码是 4.1.2 那张数据流图的活样本：`ga/gb` 住 DRAM、`sa/sb` 住共享内存、`acc` 住寄存器，算术只发生在 `dot` 里，共享张量用完必须 `free_shared`。注意此例没有 `TMemoryTensor`，因为它是 Blackwell 专属，要等 `examples/blackwell_matmul` 才会出现。

#### 4.1.4 代码实践

**实践目标**：用源码阅读 + 对照表的方式，把「张量类型 ↔ 内存层次 ↔ 是否需显式释放」三者钉死。

**操作步骤**：

1. 打开 [python/tilus/ir/tensor.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py)，定位四个 `class` 定义行（81、546、658、764），阅读各自 docstring 的第一句。
2. 打开 [python/tilus/lang/instructions/root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py)，找到 `register_tensor`（294 行）、`shared_tensor`（394 行）、`global_view`（422 行）、`free_shared`（684 行），确认它们各自的「Thread group」说明。
3. 填写下面这张对应表（留空处自己补全）：

| 张量类型 | 内存层 | 创建指令 | 是否需显式释放 | 能否直接做算术 |
|----------|--------|----------|----------------|----------------|
| RegisterTensor | 寄存器 | `register_tensor` | 否 | ✅ |
| SharedTensor | 共享内存 | `shared_tensor` | ？ | ？ |
| GlobalTensor (view) | 显存 | `global_view` | ？ | ？ |
| GlobalTensor (workspace) | 显存 | `global_tensor` | 否（随内核自动释放） | ❌ |
| TMemoryTensor | TMEM | `tcgen05.alloc` | ？ | ❌ |

**需要观察的现象**：`register_tensor` 与 `shared_tensor` 的签名里都没有 `layout` 参数（[root.py:294-300](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L294-L300) 与 [root.py:394-399](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L394-L399)），而 `global_view` 有 `strides` 参数（[root.py:422-429](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L422-L429)）。这说明：用户在写 Tilus Script 时，**几乎从不直接指定寄存器/共享张量的布局**——这正是下一节「延迟绑定」的动机。

**预期结果**：你应当得出 SharedTensor 需显式释放（`free_shared`）、且三类非寄存器张量都不能直接做算术的结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Tilus 不允许直接对 `SharedTensor` 做 `sa + sb`，而非要先 `load_shared` 进寄存器？

> **参考答案**：GPU 的共享内存没有算术逻辑单元，寄存器才是离 ALU/张量核最近的存储。共享内存只支持 load/store，做运算必须先把数据搬进寄存器；这也是 `SharedTensor` 类上没有任何 `__add__` 等运算符重载、只有 `item_ptr`/`permute` 等访问方法的原因（见 [tensor.py:648-655](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L648-L655)）。

**练习 2**：`examples/matmul/matmul_v2.py` 里没有出现 `TMemoryTensor`，这是为什么？

> **参考答案**：`TMemoryTensor` 只在 Blackwell（sm_100+）上存在，由第五代张量核 `tcgen05` 使用。`matmul_v2` 是面向通用架构的示例，用的是通用 `dot` 指令（落在寄存器累加器上），所以用不到 TMEM。要看 TMEM 得读 `examples/blackwell_matmul`。

---

### 4.2 optional_layout：布局的延迟绑定

#### 4.2.1 概念说明

这是本讲最核心、也是 Tilus 最有特色的设计之一。

回到 4.1.3：`RegisterTensor`、`SharedTensor`、`TMemoryTensor` 的字段里都有个 `optional_layout`，类型是 `Optional[对应Layout]`，也就是**它可以是 `None`**。一个张量被创建时，它的布局（数据在物理内存里怎么排布）可以**暂时不知道**，留到后面由编译器的「布局自动推理」Pass 填上。这就是**延迟绑定（deferred binding）**。

为什么需要延迟绑定？因为一个张量的「最佳布局」往往不取决于它自己，而取决于**谁在读写它、用什么指令**。比如一个寄存器张量，如果接下来要喂给 MMA 张量核，它就得长成 MMA 要求的布局；如果要 `store_shared` 进共享内存，它的布局又要和共享内存的 swizzle 配合。创建张量那一刻，这些上下文可能还没出现。所以 Tilus 选择：**先只给定 `shape`（逻辑形状），布局留空，等指令都到齐了，再用一轮全局推理一次性决定所有张量的布局**（u4-l5 专门讲这个推理过程）。

而 `GlobalTensor` 是个例外——它的布局是**必填**的（`layout: GlobalLayout`，不是 `Optional`），因为全局内存的排布由外部数据（torch 张量）决定，编译期就已知，没有「推理」的余地。

#### 4.2.2 核心流程

延迟绑定在张量对象上表现为一套「三态」协议：

```
                  create(shape)              with_layout(layout)
   ┌──────────────┐  ──────────►  ┌──────────────┐  ──────────────►  ┌──────────────┐
   │ 张量尚未创建  │               │ optional_layout │                  │ optional_layout │
   └──────────────┘               │   = None        │                  │   = <Layout>    │
                                  │ has_layout()=F  │                  │ has_layout()=T  │
                                  └──────────────┘                  └──────────────┘
                                         │ 访问 .layout                    │ 访问 .layout
                                         ▼ 抛 ValueError                  ▼ 返回真实布局
```

- **未绑定态**：`optional_layout is None`，`has_layout()` 返回 `False`，此时访问 `.layout` 会**抛 `ValueError`**（提示「布局还没定义」）。
- **已绑定态**：通过 `with_layout(layout)` 生成一个**新对象**（remember：IR 不可变，永远不改原对象），它的 `optional_layout` 被填上，`has_layout()` 返回 `True`，`.layout` 能正常返回。

这套协议在三种张量上几乎完全对称（`has_layout` / `with_layout` / `layout` 三个方法），只是返回的 Layout 类型不同。布局推理 Pass 的工作，本质上就是「遍历所有张量，给每个 `optional_layout is None` 的张量调用 `with_layout`」。

#### 4.2.3 源码精读

以 `RegisterTensor` 为代表，看清这三个方法如何实现延迟绑定：

**`has_layout()`——查是否已绑定**（[python/tilus/ir/tensor.py:189-197](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L189-L197)）

```python
def has_layout(self) -> bool:
    return self.optional_layout is not None
```

**`layout`——未绑定时直接报错**（[python/tilus/ir/tensor.py:141-157](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L141-L157)）

```python
@cached_property
def layout(self) -> RegisterLayout:
    if self.optional_layout is None:
        raise ValueError("The layout of RegisterTensor is not defined yet.")
    return self.optional_layout
```

注意它是 `@cached_property`：一旦绑定后访问就缓存结果，避免重复判断。

**`with_layout()`——生成一个绑定后的新对象**（[python/tilus/ir/tensor.py:170-187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L170-L187)）

```python
def with_layout(self, layout: RegisterLayout) -> RegisterTensor:
    if not isinstance(layout, RegisterLayout):
        raise ValueError(...)
    if not same_list(self.shape, layout.shape):
        raise ValueError(f"Shape mismatch: ...")
    return dataclasses.replace(self, optional_layout=layout)
```

`dataclasses.replace(self, optional_layout=layout)` 是不可变对象「修改」的标准姿势：它复制 `self` 的所有字段，只把 `optional_layout` 换成新值，返回一个**全新的 `RegisterTensor`**（`shape` 和 `dtype` 都不变）。同时它会校验「layout 的 shape 必须和张量的 shape 一致」——这是 shape 与 layout 的一致性约束（4.3 会展开）。

`SharedTensor`（[tensor.py:585-641](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L585-L641)）和 `TMemoryTensor`（[tensor.py:710-753](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L710-L753)）几乎是这套逻辑的复制粘贴，只是把 `RegisterLayout` 换成 `SharedLayout` / `TMemoryLayout`。

而 `GlobalTensor` 完全是另一种风格——它的 `layout` 是个**普通字段**（不是 `optional_layout`），也没有 `has_layout()`：

```python
# GlobalTensor（tensor.py:777, 810-813）
layout: GlobalLayout   # 必填，永不为 None

def with_layout(self, layout: GlobalLayout) -> GlobalTensor:
    return dataclasses.replace(self, layout=layout)   # 直接替换，没有 None 判断
```

这就是「GlobalTensor 不参与延迟绑定」在源码层面的铁证。

最后再补一个容易踩坑的点：`RegisterTensor.__bool__` 永远返回 `True`（[tensor.py:206-210](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L206-L210)），这样代码里才能用 `if inst.output:` 来判断指令有没有产出张量（而不是被空的 `RegisterTensor` 误判成假值）。它和布局无关，但属于「张量作为值被使用」时的一个 quirk。

#### 4.2.4 代码实践

**实践目标**：用纯 Python（不需要 GPU）亲手演示「延迟绑定」的三态协议，确认 `optional_layout` 的行为。

**操作步骤**：

把下面这段「示例代码」存成 `check_optional_layout.py` 并运行（仅依赖 tilus 的 IR 层，不触发编译）：

```python
# 示例代码：演示 RegisterTensor / SharedTensor 的 optional_layout 延迟绑定
from tilus import float32
from tilus.ir.tensor import RegisterTensor, SharedTensor, GlobalTensor
from tilus.ir.layout import global_row_major

# 1) 创建时不给布局 → optional_layout 为 None
r = RegisterTensor.create(dtype=float32, shape=[32, 64])
print("has_layout:", r.has_layout())        # 预期 False
print("optional_layout:", r.optional_layout) # 预期 None

# 2) 未绑定时访问 .layout 应当抛 ValueError
try:
    _ = r.layout
    print("ERROR: 应当报错却没报")
except ValueError as e:
    print("未绑定时访问 .layout 报错:", e)

# 3) SharedTensor 同理
s = SharedTensor.create(dtype=float32, shape=[32, 64])
print("shared has_layout:", s.has_layout())  # 预期 False

# 4) GlobalTensor 没有 optional_layout / has_layout，布局必填，shape 从 layout 推
g = GlobalTensor.create(dtype=float32, layout=global_row_major(32, 64))
print("global shape (来自layout):", tuple(g.shape))   # 预期 (32, 64)
print("global 有 has_layout 吗:", hasattr(g, "has_layout"))  # 预期 False
```

**需要观察的现象**：

1. `r.has_layout()` 与 `s.has_layout()` 都为 `False`，`optional_layout` 为 `None`。
2. 访问未绑定的 `r.layout` 抛出 `ValueError: The layout of RegisterTensor is not defined yet.`
3. `GlobalTensor` 既没有 `has_layout` 方法，也没有 `optional_layout` 字段；它的 `shape` 是从 `layout` 推出来的。

**预期结果**：三态协议在前三种张量上成立，`GlobalTensor` 是例外。**若你尚未安装 tilus 或环境无 GPU，本实践的纯 IR 部分仍可在 CPU 上运行（不涉及 `build_program`）；若连导入都失败，则改为阅读 [tensor.py:141-197](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L141-L197) 复述其逻辑，标注「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `with_layout` 用 `dataclasses.replace` 返回新对象，而不是直接 `self.optional_layout = layout`？

> **参考答案**：所有 IR 节点是 `frozen dataclass`（[tensor.py:29](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L29)），字段不可变；而且张量用身份相等（`eq=False`，`__hash__` 即 `id`，见 [node.py:27-31](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/node.py#L27-L31)）。直接改字段既被 frozen 禁止，也会破坏「同一个张量对象在变换器 memo 里的身份稳定性」。所以必须复制出一个新对象，让变换器用新身份去追踪绑定后的张量。

**练习 2**：如果布局推理 Pass 漏掉了一个 `RegisterTensor`，没给它 `with_layout`，会在什么时候报错？

> **参考答案**：会在后续访问该张量 `.layout` 的地方抛 `ValueError("The layout of RegisterTensor is not defined yet.")`（[tensor.py:155-156](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L155-L156)）。典型场景是代码生成阶段（Emitter 要按布局展开每线程的地址），所以这类 bug 往往在 codegen 时才暴露。

---

### 4.3 shape 与 layout：谁拥有谁

#### 4.3.1 概念说明

最后一个最小模块，厘清 `shape`（逻辑形状）和 `layout`（物理排布）的关系。这是初学者最容易混淆的点。

直觉上：

- **shape** 回答「这个张量逻辑上是几维的、每维多大」——比如 `[128, 64]` 表示 128 行 64 列的逻辑矩阵。这是**数学含义**。
- **layout** 回答「这些逻辑元素在物理内存里怎么摆」——比如同样是 `[128, 64]`，它可以按行优先摆在显存里，也可以被切成小块分布到 128 个线程的寄存器里、每个线程拿若干元素。这是**物理含义**。

关键结论：**同一个 shape 可以对应无穷多种 layout**。shape 是「张量自己的属性」，而 layout 还取决于「线程怎么分工」。这就是为什么 Tilus 把 shape 和 layout 分成两个概念，并且允许 layout 暂时缺省（4.2）。

但四种张量在「shape 和 layout 谁是字段、谁推导谁」上，存在一个重要的**不对称**：

| 张量 | shape 从哪来 | layout 从哪来 | shape 的元素类型 |
|------|--------------|---------------|------------------|
| `RegisterTensor` | 是显式字段 `shape: tuple[int,...]` | `optional_layout`（可 None） | 纯整数 |
| `SharedTensor` | 是显式字段 `shape: tuple[int,...]` | `optional_layout`（可 None） | 纯整数 |
| `TMemoryTensor` | 是显式字段 `shape: tuple[int,...]` | `optional_layout`（可 None） | 纯整数 |
| `GlobalTensor` | **从 `layout.shape` 推导**（无字段） | 必填字段 `layout` | **可以是符号表达式 `Expr`** |

两个看点：

1. **前三种张量的 shape 是「主」，layout 是「宾」**（且可缺省）；**GlobalTensor 反过来，layout 是「主」，shape 是「宾」**（且 layout 必填）。这是 4.2「只有 GlobalTensor 不参与延迟绑定」的另一面。
2. **只有 GlobalTensor 的 shape 可以是符号**。前三种张量的 `shape` 都是 `tuple[int, ...]`（编译期必须确定的纯整数），而 `GlobalLayout.shape` 是 `tuple[Expr, ...]`——因为全局张量描述的是外部数据（其大小可能是运行时才知的内核参数 `m_size` 等），所以 shape 得用符号表达式表达。

#### 4.3.2 核心流程

shape 与 layout 的一致性由两道关卡保证：

```
   创建张量 (shape)                  绑定 layout (with_layout)
        │                                  │
        ▼                                  ▼
   shape 必须是合法正整数        ──►   校验 layout.shape == tensor.shape
   (RegisterLayout 还要求             (same_list 检查，不一致就 ValueError)
    shape 能被 mode_shape 整除)
```

第一道关卡在 `create` 与 Layout 的 `validate` 里；第二道关卡在每种张量的 `with_layout` 里（4.2.3 已见过 `same_list(self.shape, layout.shape)` 的断言）。换句话说，**shape 一旦定下，layout 的 shape 必须和它逐维相等**——layout 只决定「怎么摆」，不能改变「摆多少」。

对于 `RegisterTensor`，layout 还会派生出一个 shape 没有的关键信息：**每个线程实际持有多少元素**（`local_size`）。这是因为寄存器张量是分布式的——同一个 `[128,64]` 张量，在 4 warp 和 8 warp 下，每个线程拿到的元素数完全不同，这个信息只存在于 layout 里，shape 给不出。

#### 4.3.3 源码精读

先看前三种张量「shape 是字段」的证据（以 RegisterTensor 为例，[tensor.py:96-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L96-L97)）：

```python
class RegisterTensor(Tensor):
    shape: tuple[int, ...]                          # ← 显式字段，纯整数
    optional_layout: Optional[RegisterLayout] = None
```

而 `GlobalTensor` 完全相反（[tensor.py:783-794](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L783-L794)）——它没有 `shape` 字段，`shape` 是个 `@property`，直接返回 `self.layout.shape`：

```python
class GlobalTensor(Tensor):
    layout: GlobalLayout                            # ← 必填字段

    @property
    def shape(self) -> tuple[Expr, ...]:
        return self.layout.shape                    # shape 是从 layout 推导出来的
```

再看两种 Layout 各自的 shape 类型，差异一目了然：

**RegisterLayout.shape 是纯整数**（[python/tilus/ir/layout/register_layout.py:50-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L50-L53)）：

```python
class RegisterLayout(IRNode):
    shape: tuple[int, ...]          # 纯整数，编译期确定
    mode_shape: tuple[int, ...]
    spatial_modes: tuple[int, ...]
    local_modes: tuple[int, ...]
```

**GlobalLayout.shape 是符号表达式**（[python/tilus/ir/layout/global_layout.py:48-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L48-L51)）：

```python
class GlobalLayout(IRNode):
    shape: tuple[Expr, ...]         # 可以是 m_size 这类符号
    size: Expr
    axes: tuple[Var, ...]
    offset: Expr
```

这正是 `global_view` 能接受 `shape=[m_size, k_size]`（`m_size` 是运行时参数）的底层原因（[root.py:460-468](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L460-L468)）：当不指定 strides 时，它用 `global_row_major(*shape)` 构造一个行优先的 GlobalLayout，shape 带着符号一起进去。

最后看 `local_size`——shape 给不出、只有 layout 才知道的信息（[python/tilus/ir/layout/register_layout.py:98-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L98-L104)）：

```python
@cached_property
def local_shape(self) -> list[int]:
    return [self.mode_shape[i] for i in self.local_modes]

@cached_property
def local_size(self) -> int:
    return prod(self.local_shape)   # 每个线程持有的元素数
```

`local_size` 是 layout 把 `mode_shape` 按 `local_modes`（线程局部的那些维度）累乘得到的。`RegisterTensor` 也暴露了这个属性（[tensor.py:159-168](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L159-L168)），它是 Emitter 决定「每个线程生成几条标量指令」的依据。这再次说明：shape 只描述逻辑总量，而「每个线程分到多少」是 layout 独有的、更细粒度的信息。

#### 4.3.4 代码实践

**实践目标**：亲手验证「GlobalTensor 的 shape 来自 layout 且可为符号」，以及「前三种张量的 shape 是独立字段」。

**操作步骤**（纯 IR，无需 GPU）：

```python
# 示例代码：对比 shape 的来源
from tilus import float32
from tilus.ir.tensor import RegisterTensor, GlobalTensor
from tilus.ir.layout import global_row_major
from tilus.hidet.ir.expr import Var

# A) RegisterTensor：shape 是字段，是纯整数
r = RegisterTensor.create(dtype=float32, shape=[128, 64])
print("register shape 字段:", r.shape, "类型:", type(r.shape[0]).__name__)  # int

# B) GlobalTensor：shape 是 property，来自 layout
g = GlobalTensor.create(dtype=float32, layout=global_row_major(128, 64))
print("global shape 来自 layout:", tuple(g.shape))

# C) GlobalLayout 的 shape 可以是符号（用一个 Var 模拟运行时参数 m）
m = Var("m", "int32")
g2 = GlobalTensor.create(dtype=float32, layout=global_row_major(m, 64))
print("global shape 含符号:", [str(s) for s in g2.shape])  # 预期 ['m', '64']
```

**需要观察的现象**：

- `r.shape[0]` 是 Python `int`；`g.shape` 与 `g.layout.shape` 是同一个对象。
- 第三段里 `g2.shape` 含一个符号 `m`，说明全局张量的形状可以依赖运行时参数，而寄存器/共享/TMemory 张量做不到（它们的 shape 必须是编译期整数）。

**预期结果**：证实 4.3.1 表格中的不对称——前三种「shape 主、layout 宾且可缺省」，`GlobalTensor`「layout 主、shape 宾且必填、可符号」。若环境无法导入 tilus，请阅读 [tensor.py:783-808](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L783-L808) 与 [global_layout.py:48-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/global_layout.py#L48-L51) 复述结论，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RegisterTensor.shape` 必须是编译期整数，而 `GlobalTensor.shape` 可以是符号？

> **参考答案**：寄存器/共享/TMemory 张量描述的是**线程块内部的局部数据**，它的排布（layout）和每个线程分到的元素数（`local_size`）必须在编译期确定，否则无法生成确定的标量指令序列。全局张量描述的是**外部输入数据**，其大小（如 `m_size`）是运行时才知道的内核参数；它只用于计算地址偏移（GlobalLayout 的 `offset` 表达式），不需要在编译期固化每个元素，所以 shape 可以是符号 `Expr`。

**练习 2**：给定一个 `shape=[128, 64]` 的 `RegisterTensor`，能否仅凭 shape 算出每个线程持有多少元素？为什么？

> **参考答案**：不能。`shape` 只给出逻辑总量 \(128 \times 64 = 8192\)，但「每个线程持多少」取决于线程数和分布方式，这些信息只在 `layout.local_size` 里（[register_layout.py:102-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L102-L104)）。同样是 `[128,64]`，在 4 warp（128 线程）与 8 warp（256 线程）下 `local_size` 不同；甚至同样线程数下，分布方式（spatial vs local）不同也会改变 `local_size`。这正是 layout 不可缺省的根本原因之一。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这张「**张量—内存—布局**全景表」。请结合源码精读，自己补全所有问号格：

| 张量类型 | 物理内存 | shape 来源 | shape 元素类型 | 布局字段名 | 可否延迟绑定布局 | 是否需显式释放 | 创建指令 | 对应 Layout 类 |
|----------|----------|------------|----------------|------------|-------------------|----------------|----------|----------------|
| RegisterTensor | 寄存器（分布式） | 显式字段 | `int` | `optional_layout` | ✅ | 否 | `register_tensor` | `RegisterLayout` |
| SharedTensor | 共享内存 | ？ | ？ | ？ | ？ | ？ | ？ | `SharedLayout` |
| TMemoryTensor | TMEM（Blackwell） | ？ | ？ | ？ | ？ | ？ | `tcgen05.alloc` | `TMemoryLayout` |
| GlobalTensor | 显存（DRAM） | `layout.shape` | ？ | `layout`（必填） | ❌ | view 否 / workspace 自动 | `global_view`/`global_tensor` | `GlobalLayout` |

**进阶子任务**：

1. 打开 [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py)，给 `__call__` 里出现的每个张量（`ga/gb/sa/sb/acc/lda/ldb/a/b/casted_acc/gc`）标注它属于哪一行。
2. 用 4.2.4 的示例脚本，实际跑一次 `has_layout()` 与 `.layout` 的报错路径，把输出贴在表后作为证据。
3. 思考题：如果要让 Tilus 支持一种新的「片上 scratchpad 内存」，按本讲的模式，你需要为它定义哪些字段与方法？（提示：参考 `SharedTensor` 的三件套 `optional_layout` / `has_layout` / `with_layout`，以及一个对应的 `XxxLayout` 类。）

完成这张表后，你就建立了本单元后续所有讲义共同依赖的「张量全景图」。

## 6. 本讲小结

- Tilus 用四种张量类型一一映射 GPU 的四层内存：`RegisterTensor`（寄存器，分布式）、`SharedTensor`（共享内存）、`GlobalTensor`（显存）、`TMemoryTensor`（Blackwell TMEM）。
- **只有寄存器张量能做算术**；其余三种只是数据载体，运算前必须 `load_*` 进寄存器。`SharedTensor` 与 `TMemoryTensor` 还必须**显式释放**。
- `optional_layout` 实现了**布局延迟绑定**：`RegisterTensor`/`SharedTensor`/`TMemoryTensor` 创建时可不给布局（`None`），通过 `has_layout()`/`.layout`（未绑定抛错）/`with_layout()`（生成新对象）这套三态协议管理；`GlobalTensor` 是例外，布局必填、不参与延迟绑定。
- shape 与 layout 是「逻辑」与「物理」的分离：前三种张量里 shape 是主、layout 是宾且可缺省；`GlobalTensor` 里 layout 是主、shape 由 `layout.shape` 推导。
- 只有 `GlobalLayout.shape` 可以是符号表达式 `Expr`（描述运行时才知的输入尺寸），其余 Layout 的 shape 必须是编译期整数；`local_size` 是 layout 独有、shape 给不出的「每线程元素数」。
- 一切修改都遵循不可变 IR 范式：`with_layout` 用 `dataclasses.replace` 返回新对象，张量用身份相等（`id`）。

## 7. 下一步学习建议

本讲只建立了「张量类型与内存层次」的静态地图，**还没有真正进入任何一种 Layout 的内部结构**。建议按以下顺序继续：

1. **u4-l2 RegisterLayout：mode、spatial 与 local**——本讲反复提到的 `local_size`/`local_modes`/`spatial_modes` 到底怎么把一个 shape 切分到线程上，这是布局系统的第一块硬骨头，必读。
2. **u4-l3 SharedLayout、GlobalLayout 与 TMemoryLayout**——把本讲一笔带过的 swizzle、符号 offset、TMEM 的 lane/column 结构讲透。
3. **u4-l5 布局自动推理（Layout Inference）**——回答「`optional_layout` 最终是怎么被填上的」，把本讲的延迟绑定闭环。
4. 阅读源码时，可配合 `tilus.option.debug.dump_ir()`（u3-l1）观察某个真实内核在 `layout_inference` Pass 前后，张量的 `optional_layout` 如何从 `None` 变成具体布局。
