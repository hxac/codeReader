# IPC 张量与 tilescale_ext 内存管理

## 1. 本讲目标

本讲聚焦 TileScale 分布式栈里最底层的一块「胶水」——`tilescale_ext` 这个 C++ 扩展，以及它在 Python 侧的封装。学完后你应该能够：

1. 说清 `tensor_from_ptr` 如何把一个裸 GPU 指针「包装」成 `torch.Tensor`，以及 `take_ownership` 标志如何决定**谁负责 `cudaFree`**。
2. 理解 CUDA IPC（进程间通信）handle 的创建与同步流程：两个进程如何通过一段 64 字节的 handle 共享对方的显存。
3. 掌握 `BaseAllocator` 这个「大块 `cudaMalloc` + 线性 bump 分配」的内存池设计，以及它如何把「远程基址表」注入编译好的 kernel。
4. 把这三者串起来，画出「两个进程通过 IPC handle 共享一段 GPU 显存」的完整流程图，并准确指出释放显存的责任方。

本讲是 u6-l4（分布式运行时）的延续：u6-l4 讲的是「device 用哪族原语 → host 做哪套运行时准备」，而本讲回答的是「那套运行时准备里，跨进程共享显存的底层机制到底是什么」。

## 2. 前置知识

阅读本讲前，你需要大致了解（不要求精通）：

- **`torch.Tensor` 与 `data_ptr`**：每个 tensor 内部持有一段显存，`tensor.data_ptr()` 返回这段显存起始地址的整数形式。普通 `torch.empty(...)` 用的是 PyTorch 的 **caching allocator**（缓存分配器），它并不直接对应一次 `cudaMalloc`。
- **`cudaMalloc` / `cudaFree`**：CUDA 运行时最原始的显存分配/释放调用。本讲会频繁通过 `ctypes` 直接调用它。
- **`at::from_blob`**：LibTorch 提供的接口，用一个已有指针构造 tensor，**不拷贝数据**，并可挂一个 `deleter`——tensor 引用计数归零时调用的回调。这是「指针 → tensor」零拷贝包装的关键。
- **进程间通信（IPC）**：同一台机器上，两个独立进程默认各自拥有独立地址空间，无法直接访问对方内存。CUDA IPC 提供了一种受控的共享机制。
- **PE / rank**：见 u6-l1、u6-l4。本讲中「进程」「PE」「rank」基本同义——`launch.sh` 用 torchrun 每卡拉一个进程，每个进程就是一个 PE。

一个贯穿全讲的核心问题：**分布式 device kernel 要访问「别的 PE 的显存」，第一步必须让本进程能拿到对方显存的一个本地可用地址。** 本讲讲的就是这一步在 host 侧如何完成。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 语言 | 职责 |
| --- | --- | --- |
| `tilescale_ext/__init__.py` | Python | 从编译产物 `tilescale_ext._C` 重新导出 5 个底层函数 |
| `tilelang/utils/ts_ext/tensor.cpp` | C++ | `tensor_from_ptr`（指针→tensor，带所有权语义）、`create_host_device_tensor`（pinned 映射） |
| `tilelang/utils/ts_ext/ipc_ops.cpp` | C++ | `create_tensor`（裸 `cudaMalloc`）、`create_ipc_handle`、`sync_ipc_handles` |
| `tilelang/utils/ts_ext/ts_ext_bindings.cpp` | C++ | pybind11 绑定，把 C++ 函数注册为 `tilescale_ext._C.*` |
| `tilelang/utils/allocator.py` | Python | `BaseAllocator`：大块 `cudaMalloc` + bump 分配 + 远程基址表 |
| `tilelang/utils/tensor.py` | Python | 用户态入口 `tilelang.tensor(...)`，按是否传 `allocator` 分流 |
| `tilelang/distributed/utils.py` | Python | `create_tensor` / `get_local_ipc_handle` / `create_dist_tensor` 等 host 侧 IPC 辅助 |
| `tilelang/jit/kernel.py` | Python | `JITKernel.initialize(allocator=...)`：把基址表注入 kernel |

记住一条总链路（本讲就是把它逐段拆开）：

```
用户 tilelang.tensor(...)
   → BaseAllocator._allocate_tensor(...)         # bump 分配切片
      → tensor_from_ptr(ptr, ..., take_ownership=False)   # 指针→tensor，不接管所有权
   → BaseAllocator._init_table(...)              # 分布式：交换 IPC handle、构建基址表
      → _create_ipc_handle / _sync_ipc_handles   # 跨进程共享显存
   → kernel.initialize(allocator=...)            # 把表交给 device kernel
```

## 4. 核心概念与源码讲解

### 4.1 tensor_from_ptr 与 take_ownership 所有权模型

#### 4.1.1 概念说明

在 TileScale 的内存模型里，显存的「分配」和「tensor 的构造」被刻意拆成两件事：

- **分配**：由 allocator 或裸 `cudaMalloc` 完成，产生一个裸指针 `void*`。
- **构造 tensor**：由 `tensor_from_ptr` 完成，把这个裸指针包装成 `torch.Tensor`，**不拷贝任何数据**。

这种拆分的好处是：同一段显存可以同时被「拥有它的内存池」和「若干个临时 tensor 视图」引用，而释放责任不会乱。决定释放责任的就是 `take_ownership` 这个布尔参数：

- `take_ownership=True`：tensor **接管**这段显存，当 tensor 被垃圾回收时，它的 deleter 会调用 `cudaFree`。
- `take_ownership=False`（默认）：tensor 只是一个**非拥有视图**，deleter 是空操作；真正的释放由「分配它的那一方」（通常是 allocator）负责。

这是一个典型的 **RAII + 所有权转移** 设计：通过给 `at::from_blob` 挂不同的 deleter，同一段构造代码就能产出「拥有型」或「视图型」两种 tensor。

#### 4.1.2 核心流程

`tensor_from_ptr` 的执行流程：

1. 校验指针非空，把 `uint64_t` 整数还原成 `void*`。
2. 把 dtype 字符串（如 `"float32"`）映射为 `at::ScalarType`。
3. 根据 `shape` 计算元素数 `nelems`（带溢出保护）。
4. **按 `take_ownership` 选择 deleter**：
   - `True`：构造一个捕获了原指针的 lambda，lambda 内调 `cudaFree`。
   - `False`：构造一个空 lambda。
5. 用 `at::from_blob(data_ptr, shape, deleter, options)` 产出 tensor。`nelems == 0` 时直接返回 `torch::empty`，避免包装空指针。

关键认知：`at::from_blob` 默认**不持有**内存所有权，所有权完全由你传入的 deleter 决定。

#### 4.1.3 源码精读

先看 Python 侧的导出。`tilescale_ext/_C` 是 pybind11 编译出的扩展模块，`tilescale_ext/__init__.py` 只是把 5 个函数重新导出：

[tilescale_ext/__init__.py:1-7](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilescale_ext/__init__.py#L1-L7) —— 从 `_C` 导入 `tensor_from_ptr`、`_create_tensor`、`_create_ipc_handle`、`_sync_ipc_handles`、`create_host_device_tensor`。

> 注意：`tilescale_ext/__init__.py`（产物侧）与 `tilelang/utils/ts_ext/__init__.py`（源码侧）都导出同一组函数。前者是安装到顶层 `tilescale_ext/` 的包，后者是开发树里的镜像，二者内容一致。这是 u1-l4 讲过的「源码与产物分离」设计。

绑定层把 Python 名 `_create_tensor` 映射到 C++ 的 `create_tensor`（注意名字不同）：

[tilelang/utils/ts_ext/ts_ext_bindings.cpp:12-23](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ts_ext_bindings.cpp#L12-L23) —— `tensor_from_ptr` 直接绑定；`_create_tensor` 用 lambda 把 Python dtype 对象转成 `c10::ScalarType` 后转调 `create_tensor`。

核心函数 `tensor_from_ptr`：

[tilelang/utils/ts_ext/tensor.cpp:49-87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L49-L87) —— 把 `uint64_t` 指针值还原为 `void*`，按 shape 算元素数，再据 `take_ownership` 选 deleter，最后 `at::from_blob` 产出 tensor。

重点看 deleter 的分流（这是所有权模型的全部精髓）：

[tilelang/utils/ts_ext/tensor.cpp:67-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L67-L80) —— `take_ownership=True` 时 lambda 捕获 `saved_ptr` 并在析构时 `cudaFree`；`False` 时是空操作。注意第 69 行**按值捕获** `saved_ptr`，避免原指针变量失效后悬垂。

最后一步是无拷贝包装：

[tilelang/utils/ts_ext/tensor.cpp:84-86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L84-L86) —— `at::from_blob(data_ptr, shape, deleter, options)`，tensor 直接复用传入指针，不复制数据。

> 旁支：同一文件里的 `create_host_device_tensor`（[tensor.cpp:89-113](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L89-L113)）用 `cudaHostAlloc(..., cudaHostAllocMapped)` 分配一段** pinned 并映射到设备地址空间**的内存，返回一对 `(host_tensor, device_tensor)`，二者底层是同一块物理内存。它是 host/device 零拷贝通信的辅助，与 IPC 无直接关系，了解即可。

#### 4.1.4 代码实践

**实践目标**：直观感受 `take_ownership` 对释放责任的影响。

**操作步骤**（源码阅读型 + 待本地验证的运行型）：

1. 阅读 [tensor.cpp:67-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L67-L80)，确认两种 deleter 的差异。
2. 若本地有可用 GPU 且已装好 `tilescale_ext`，运行下面的示例代码（**示例代码**，非仓库原有）：

```python
# 示例代码：观察 take_ownership 的作用
import torch
from tilescale_ext import _create_tensor, tensor_from_ptr

# _create_tensor 内部用裸 cudaMalloc 分配（见 4.2）
t = _create_tensor([1024], torch.float32)
ptr = t.data_ptr()
print("owner tensor ptr:", hex(ptr))

# 视图型包装：take_ownership=False，不接管；释放仍由 t 负责
view = tensor_from_ptr(ptr, [1024], "float32", device=0, take_ownership=False)
assert view.data_ptr() == ptr
del view   # 不会 cudaFree
print("view deleted, owner still alive")

# 接管型包装：take_ownership=True，析构时会 cudaFree(ptr)
owning = tensor_from_ptr(ptr, [1024], "float32", device=0, take_ownership=True)
del owning  # 触发 cudaFree(ptr)
print("owning deleted, cudaFree called on", hex(ptr))
# 此时原 t 的底层显存已被释放，再访问 t 属于 use-after-free
```

**需要观察的现象**：

- `view` 删除后，`t` 仍可正常读写。
- `owning` 删除后，`t` 的底层指针已被 `cudaFree`，再访问 `t` 会出现非法访问或数据损坏。

**预期结果 / 待本地验证**：上述断言 `view.data_ptr() == ptr` 成立；接管型删除后日志不报 `cudaFree failed`。若运行环境不具备，则改为源码阅读：在 [tensor.cpp:70-77](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L70-L77) 处注释「`saved_ptr` 按值捕获、析构 `cudaFree`」，确认你对所有权流向的理解。

> ⚠️ 注意：实践中**不要**对同一段显存既保留 owner 又用 `take_ownership=True` 包装，删除接管型 tensor 后原 owner 会变成悬垂指针。TileLang 内部用 `take_ownership=False` 来避免这种双重所有权。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tensor_from_ptr` 的 deleter 要按值捕获 `saved_ptr`，而不是引用？

**参考答案**：因为 `saved_ptr` 是函数局部变量，函数返回后栈帧销毁；若按引用捕获，tensor 析构时调用的 deleter 会访问一个已失效的引用，形成悬垂引用。按值捕获把指针值复制进 lambda，生命周期与 lambda（即 tensor）一致，安全。

**练习 2**：`nelems == 0` 时函数返回 `torch::empty(shape, options)` 而不是 `from_blob`，为什么？

**参考答案**：零元素 tensor 不应持有任何真实显存指针；`from_blob(nullptr, ...)` 既无意义又危险。直接返回一个标准的空 tensor 更安全，也让 deleter（可能含 `cudaFree`）不会被错误触发。

---

### 4.2 IPC handle 创建/同步（跨进程共享显存）

#### 4.2.1 概念说明

CUDA IPC 让同一台机器上的两个进程共享 GPU 显存：进程 A `cudaMalloc` 一段显存后，可以生成一个**不透明的 64 字节 handle**；进程 B 拿到这个 handle 后 `cudaIpcOpenMemHandle`，就能在自己的地址空间里得到一个**指向 A 那段显存的本地指针**。此后 B 通过这个本地指针读写，实际访问的是 A 的显存。

TileScale 用这套机制实现「跨 PE 共享显存」的底层寻址：每个 PE 把自己 slab 的起始地址做成 handle 广播出去，所有 PE 各自 open 之后，就拥有了「每个 peer 的基址在本地的映射」。device kernel 之后就能用 `peer_base + offset` 访问远程数据（见 u6-l3 的远程寻址公式）。

这里有一个**极其重要的约束**（源码里有显式注释）：IPC handle 只对**裸 `cudaMalloc` 分配的整段显存**有效，对 PyTorch caching allocator 切出来的块**不可靠**。原因在于 caching allocator 用虚拟内存与分块复用，分配出的指针未必对应一个可被 IPC 的连续物理段。因此 TileScale 特意提供了 `_create_tensor`（走裸 `cudaMalloc`）而不是直接用 `torch.empty`。

#### 4.2.2 核心流程

跨进程共享一段显存的完整流程：

1. **各自分配**：每个 PE 用 `_create_tensor`（→ `create_tensor` → 裸 `cudaMalloc`）分配自己的本地 slab。
2. **创建 handle**：每个 PE 调 `_create_ipc_handle(local_ptr)`，内部 `cudaIpcGetMemHandle`，得到 64 字节 handle。
3. **交换 handle**：用 `torch.distributed.all_gather_object` 在 CPU 侧把所有 PE 的 handle 收集到每个 PE（这一步是普通 CPU 通信）。
4. **打开 peer handle**：每个 PE 调 `_sync_ipc_handles(rank, device_ids, buffer_ptrs_gpu, all_handles, ...)`，对每个非自己的 handle 调 `cudaIpcOpenMemHandle`，得到一批本地指针，每个指向一个 peer 的 slab 起始。
5. **写入 GPU 表**：把这一批本地指针 `cudaMemcpy` 到一个 device 端 `uint64` 数组 `buffer_ptrs_gpu`，供 device kernel 查表。

关键释放规则：**只有原始分配者（owner）负责 `cudaFree`；open 的一方绝不能 free 它 open 出来的指针。** `cudaIpcOpenMemHandle` 得到的指针由 CUDA 运行时在 IPC 体系内管理。

#### 4.2.3 源码精读

先看裸 `cudaMalloc` 版的 `create_tensor`（这是 IPC 能工作的前提）：

[tilelang/utils/ts_ext/ipc_ops.cpp:39-61](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L39-L61) —— 在当前设备上 `cudaMalloc(bytes)`，再用 `at::from_blob` 包装；deleter 调 `cudaFree`，即此 tensor 自己拥有这段显存。注意它与 `tensor.cpp` 里的 `tensor_from_ptr` 不同：这里**自己分配、自己拥有**。

Python 侧有一条带著名注释的封装：

[tilelang/distributed/utils.py:100-102](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L100-L102) —— `create_tensor` 直接转调 `_create_tensor`，注释点明「IPC 只对显式 `cudaMalloc` 分配的 tensor 有效」。**这就是 TileScale 在需要 IPC 的场景坚持用 `_create_tensor` 而非 `torch.empty` 的根因。**

handle 创建：

[ilelang/utils/ts_ext/ipc_ops.cpp:63-68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L63-L68) —— `cudaIpcGetMemHandle(&handle, ptr)` 生成 handle，把内部 `reserved[64]` 拷成一个 `py::bytearray` 返回。（链接前缀以实际为准，文件位于 `tilelang/utils/ts_ext/ipc_ops.cpp`。）

handle 同步（最核心的一步）：

[tilelang/utils/ts_ext/ipc_ops.cpp:70-98](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L70-L98) —— 遍历所有收集到的 handle，**跳过自己那一个**（`offset + i != rank`），对其余每个调 `cudaIpcOpenMemHandle(..., cudaIpcMemLazyEnablePeerAccess)` 得到本地指针存入 `buffer_ptrs[i]`；最后把整组指针 `cudaMemcpy(HostToDevice)` 到 device 端数组 `buffer_ptrs_gpu`。

要点解读：

- 第 76 行 `rdma_rank = 0` 与第 83 行 `offset = rdma_rank * num`：当前实现按节点 0 为基准偏移收集 handle，适配 all_gather 后的扁平排列。
- 第 87 行跳过自己的 handle：自己的 slab 已有本地指针，无需也不应 `cudaIpcOpenMemHandle` 自己。
- 第 89 行 `cudaIpcMemLazyEnablePeerAccess`：懒启用 peer 访问（P2P），需要时才真正打通 GPU 间直连。

host 侧把它们串起来的辅助函数：

[tilelang/distributed/utils.py:106-129](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L106-L129) —— `get_local_ipc_handle` 取 `data_ptr()` 做 handle；`create_dist_tensor` 用 `all_gather_object` 交换 device_id 与 handle，再调 `_sync_ipc_handles` 把 peer 指针写进 device 端 `buffer_ptrs_gpu`。

#### 4.2.4 代码实践

**实践目标**：阅读 `tensor.cpp` 与 `ipc_ops.cpp`，画出两个进程通过 IPC handle 共享一段 GPU 显存的流程图，并标注释放责任。

**操作步骤**（源码阅读型）：

1. 阅读 [ipc_ops.cpp:63-98](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L63-L98) 与 [utils.py:106-129](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L106-L129)。
2. 画出如下时序（文字流程图）：

```
PE0 (rank 0)                         PE1 (rank 1)
─────────────                        ─────────────
buf0 = cudaMalloc(N)                 buf1 = cudaMalloc(N)
h0 = cudaIpcGetMemHandle(buf0)       h1 = cudaIpcGetMemHandle(buf1)
        \                              /
         all_gather_object([h0,h1]) ──  (CPU 侧交换 handle)
        /                              \
_sync_ipc_handles:                   _sync_ipc_handles:
  open h1 → local ptr p1               open h0 → local ptr p0
  (p1 指向 PE1 的 buf1)                (p0 指向 PE0 的 buf0)
  写 buffer_ptrs_gpu[1]=p1            写 buffer_ptrs_gpu[0]=p0
```

3. 在图上标注：**`buf0` 由 PE0 的 `cudaFree` 释放**（通过 `_create_tensor` 返回 tensor 的 deleter），**PE1 open 得到的 `p0` 绝不能 free**，反之亦然。

**需要观察的现象 / 预期结果**：流程图应清楚体现「handle 是单向凭证、open 出的指针不归 opener 释放」这一所有权边界。如果你能在本地多卡环境跑 `tilelang/distributed/testing/sync/test_barrierall_sys.py`（见 4.3.4），可在其中加一句打印 `allocator._buffer_ptrs`，验证 device 端指针表确实含每个 peer 的本地映射指针。

> 待本地验证：`buffer_ptrs_gpu` 的实际值依赖驱动与 P2P 能力，单测里不便断言具体地址，只验证其非零且数量等于 `num_ranks`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_sync_ipc_handles` 在循环里用 `if (offset + i != rank)` 跳过自己？

**参考答案**：自己的 slab 已经有本地指针（就是自己 `cudaMalloc` 的返回值），不需要也不应该对自己调 `cudaIpcOpenMemHandle`——那是给「别人的 handle」用的；对自己 open 既无意义还可能报错。

**练习 2**：如果把 `_create_tensor` 换成 `torch.empty` 来分配要被 IPC 的显存，会发生什么？

**参考答案**：`torch.empty` 走 caching allocator，分配的指针未必对应一段可被 `cudaIpcGetMemHandle` 处理的整段显存，IPC handle 可能创建失败或 open 后行为异常。这正是 [utils.py:101](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L101) 注释所警告的。

---

### 4.3 allocator 与 tensor Python 封装

#### 4.3.1 概念说明

`BaseAllocator` 是 TileScale 自带的简易显存池：它一次性 `cudaMalloc` 一大段（slab），之后每次分配只是在 slab 上**线性 bump（碰头推进）一个指针**，把对应切片用 `tensor_from_ptr(..., take_ownership=False)` 包成 tensor 返回。整段 slab 的生命周期由 allocator 管理，子 tensor 不释放——这就是 4.1 里 `take_ownership=False` 的用武之地。

在分布式模式下，allocator 还承担**构建远程基址表**的职责：它把自己的 slab 起址做成 IPC handle，与同组 PE 交换并 open（复用 4.2 的机制），得到每个 peer 的本地基址映射，再组装成一张小表 `[local_rank, num_ranks, peer0_base, peer1_base, ...]`。这张表通过 `kernel.initialize(allocator=...)` 注入编译好的 kernel，device 侧的远程拷贝原语（`put_block`/`get_block` 等，见 u6-l3）据此把「本地地址」翻译成「peer 的远程地址」。

用户态入口是 `tilelang.tensor(shape, dtype, allocator=...)`：传 `allocator` 就走 bump 分配，不传就退化为普通 `torch.empty`。`tilelang.get_allocator(...)` 是 allocator 的工厂函数。

#### 4.3.2 核心流程

**初始化**（`BaseAllocator.__init__` → `_alloc`，可选 `_init_table`）：

1. `cudaSetDevice(device)`。
2. `cudaMalloc(&base_ptr, size)` 分配整段 slab；`_ptr` 初始指向 `base_ptr`。
3. 若 `is_distributed`，调 `_init_table`：交换 device_id、交换 IPC handle、`_sync_ipc_handles` 得到 peer 基址、组装基址表。

**单次分配**（`_allocate_tensor`，bump）：

设当前已用偏移 \(o = \text{ptr} - \text{base}\)，请求字节数 \(b\)，对齐 \(a\)（默认 256），则对齐后字节数为

\[
b_{\text{alloc}} = \text{align\_up}(b, a) = a \cdot \lceil b / a \rceil
\]

1. 校验 \(o + b_{\text{alloc}} \le \text{size}\)，否则抛 `MemoryError`。
2. 用 `tensor_from_ptr(ptr, shape, dtype_str, device, take_ownership=False)` 把当前指针包成 tensor。
3. 把 `_ptr` 推进 \(b_{\text{alloc}}\)：\(\text{ptr} \leftarrow \text{ptr} + b_{\text{alloc}}\)。
4. （可选）`return_peers=True` 时，额外为每个 peer 构造一个视图：`tensor_from_ptr(peer_base + o, ...)`，指向「同一偏移处」的 peer 显存——这就是对称堆风格的「每 PE 同偏移」访问。

**注入 kernel**（`JITKernel.initialize`）：把 `allocator.table.data_ptr()` 与 `table_size` 传给 runtime 的 `init_table`，完成 device 侧远程基址表注册。

#### 4.3.3 源码精读

`tilelang.__init__` 把这两个名字作为**可选**分布式扩展导出，缺失时为 `None`：

[tilelang/__init__.py:151-158](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L151-L158) —— `try` 导入 `tensor` 与 `get_allocator`；`ImportError`（即 `tilescale_ext` 未装）时置 `None`。这与 u1-l2 讲的「`tilescale_ext` 可选」一致。

用户态分流入口：

[tilelang/utils/tensor.py:38-66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L38-L66) —— 传 `allocator` 则校验已初始化、设备一致，转调 `allocator._allocate_tensor(shape, dtype, return_peers)`；否则 `torch.empty`。

`BaseAllocator.__init__` 与表结构约定：

[tilelang/utils/allocator.py:73-112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L73-L112) —— 构造参数含 `size/device/is_distributed/local_rank/num_local_ranks/group/align`；分布式分支断言三者齐备且 `group.size()==num_local_ranks`。注释第 94-98 行说明了基址表的内存布局：`[local_rank, num_local_ranks, buffer_ptr × num_local_ranks]`。

slab 分配（通过 `ctypes` 直接调 libcudart）：

[tilelang/utils/allocator.py:118-128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L118-L128) —— `cudaSetDevice` 后 `cudaMalloc(&base_ptr, size)`；`_ptr` 与 `base_ptr` 同值起始。第 56-67 行预先用 `ctypes` 绑好了 `cudaMalloc/cudaFree` 等签名。

分布式基址表构建（复用 4.2 的 IPC 机制）：

[tilelang/utils/allocator.py:139-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L139-L161) —— `all_gather_object` 交换 device_id；`_create_ipc_handle(base_ptr)` 做 handle 并 all_gather；`_sync_ipc_handles` 把 peer 指针写进 device 端 `buffer_ptrs`；置自身槽 `buffer_ptrs[local_rank]=base_ptr`；最后组装 CPU 端 `self._table = [local_rank, num_ranks, *buffer_ptrs]`。

bump 分配主体（注意 `take_ownership=False`）：

[tilelang/utils/allocator.py:166-226](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L166-L226) —— 计算对齐字节数、校验余量、用 `tensor_from_ptr(..., take_ownership=False)` 包出当前切片 tensor（第 199 行），随后推进 `_ptr`。`return_peers` 分支（第 201-218 行）为每个 peer 在「同偏移」处构造视图，并有一段关于 peer tensor 设备号不一致的 workaround 注释。

`get_allocator` 工厂：

[tilelang/utils/allocator.py:245-255](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L245-L255) —— 默认 `size=2**30`（1 GiB）、`is_distributed=True`，转发给 `BaseAllocator`。

把表注入 kernel：

[tilelang/jit/kernel.py:465-495](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L465-L495) —— `initialize` 按 `execution_backend` 分流：`tvm_ffi` 直接调 `adapter.init_table(table.data_ptr(), table_size, stream)`；cython/nvrtc 走 `adapter.lib.init_table(...)`。返回非 0 即抛错。

一个真实用例（仓库测试）：

[tilelang/distributed/testing/sync/test_barrierall_sys.py:43-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/testing/sync/test_barrierall_sys.py#L43-L50) —— `get_allocator(size=2**20, is_distributed=True, local_rank, num_local_ranks, group)` → `kernel.initialize(allocator=allocator)` → 用 `tilelang.tensor([...], torch.int32, allocator=allocator)` 从同一 slab 分配 `A` 与 `barrier`。

#### 4.3.4 代码实践

**实践目标**：验证 bump 分配的「连续切片」性质与基址表结构。

**操作步骤**（运行型，待本地验证；无 GPU 则改为源码阅读）：

1. 在单进程下用 allocator 分配两个 tensor（**示例代码**）：

```python
# 示例代码：观察 bump 分配的偏移连续性
import torch, tilelang
alloc = tilelang.get_allocator(size=2**22, device="cuda",
                               is_distributed=False)  # 单进程：不建基址表
a = tilelang.tensor([1024], torch.float32, allocator=alloc)
b = tilelang.tensor([1024], torch.float32, allocator=alloc)
print("a offset:", a.data_ptr() - alloc.ptr + (alloc.ptr - a.data_ptr()))  # 仅示意
gap = b.data_ptr() - a.data_ptr()
print("gap bytes:", gap)  # 期望 ≈ 1024*4 向 256 对齐 = 4096
```

2. 若有多卡与 `tilescale_ext`，参考 [test_barrierall_sys.py:43-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/testing/sync/test_barrierall_sys.py#L43-L50)，在 `initialize` 后打印 `allocator.table`（CPU 张量），确认其形状为 `2 + num_ranks`、前两元素为 `local_rank` 与 `num_ranks`。

**需要观察的现象**：

- 单进程下 `b.data_ptr() - a.data_ptr()` 等于第一个 tensor 对齐后占用的字节数（`1024×4=4096`，已对齐）。
- 分布式下 `allocator.table` 形如 `[rank, num_ranks, base0, base1, ...]`。

**预期结果 / 待本地验证**：偏移差应为 4096 的整数倍；表中 `num_ranks` 项等于 `group.size()`。若环境不具备，改为阅读 [allocator.py:166-226](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L166-L226) 并手算一次 `_align_up(1024*4, 256)`。

#### 4.3.5 小练习与答案

**练习 1**：allocator 用 `take_ownership=False` 包出每个子 tensor，那整段 slab 何时被释放？

**参考答案**：由 allocator 自己负责。`BaseAllocator.__del__` 调 `_free()`，对 `_base_ptr` 调 `cudaFree`（见 [allocator.py:130-137](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L130-L137) 与第 240-242 行析构）。子 tensor 的 deleter 是空操作，不重复释放。

**练习 2**：`return_peers=True` 时返回的 peer tensor，其 `data_ptr` 与本地 tensor 有什么关系？为什么这样设计？

**参考答案**：peer tensor 的指针是 `peer_base + current_offset`，即「该 peer 的 slab 上与本轮分配同偏移处」。这样设计让每个 PE 拿到一组「对称」视图：同一逻辑位置在各 PE 上的偏移一致，device kernel 便能以「同偏移」语义访问任意 PE 的对应数据，等价于一个手工对称堆。

**练习 3**：为什么 `initialize` 必须在 `kernel(...)` 调用之前执行？

**参考答案**：`initialize` 把远程基址表注册进 runtime，device kernel 的远程拷贝原语依赖这张表把本地地址翻译成 peer 远程地址（`remote = peer_base + (addr - local_base)`）。若跳过 `initialize`，表中基址未知，远程访问会读到无效地址。

## 5. 综合实践

把本讲三个模块串起来，完成下面的「端到端流程梳理 + 流程图」任务：

**任务**：以 `tilelang/distributed/testing/sync/test_barrierall_sys.py` 为对象，写一份「从 `get_allocator` 到 device 远程 `put_warp`」的端到端说明，要求：

1. **画一张跨两 PE 的完整流程图**，至少包含以下节点（用文字/箭头描述即可）：
   - 每个 PE 的 `cudaMalloc(size)`（谁触发？——`BaseAllocator._alloc`）。
   - `_create_ipc_handle` + `all_gather_object` + `_sync_ipc_handles`（handle 如何从 PE0 流到 PE1 并被 open）。
   - `allocator.table` 的组装与 `kernel.initialize` 的注入。
   - `tilelang.tensor(..., allocator=allocator)` 的 bump 切片（`take_ownership=False`）。
   - device kernel 内 `T.put_warp` 如何用基址表把 `dst_pe` 的目标地址算出来（呼应 u6-l3）。
2. **在图上用三种颜色/标记区分三类显存释放责任**：
   - allocator 持有、由 `BaseAllocator.__del__ → cudaFree` 释放的 slab。
   - 经 `cudaIpcOpenMemHandle` 得到的 peer 映射指针（**不释放**）。
   - 临时 tensor 视图（deleter 空操作，**不释放**）。
3. **回答关键问题**：若 PE0 的 allocator 先被析构、PE1 仍在通过 open 出来的指针访问 PE0 的 slab，会发生什么？请基于本讲源码给出判断。

**参考思路**（不是唯一答案）：

- 触发链：`get_allocator` → `_alloc`（[allocator.py:118-128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L118-L128)）→ `_init_table`（[allocator.py:139-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L139-L161)，内部用 `_create_ipc_handle`/`_sync_ipc_handles`）→ `kernel.initialize`（[kernel.py:465-495](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L465-L495)）→ `tilelang.tensor`（[tensor.py:38-66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L38-L66)）→ device `put_warp`。
- 释放：slab 由各自 allocator 析构释放；peer 映射指针与临时视图不释放。
- 关于「PE0 先析构」：PE0 的 `cudaFree` 会释放 slab 物理显存，PE1 经 IPC open 出来的指针随即指向已释放显存，继续访问属 use-after-free，可能得到脏数据或报错。因此分布式程序里所有 PE 的 allocator 生命周期必须对齐（通常靠 `dist.barrier` 同步、统一退出）。

## 6. 本讲小结

- `tensor_from_ptr` 用 `at::from_blob` 把裸 GPU 指针零拷贝包成 `torch.Tensor`；`take_ownership` 决定 deleter 是否调 `cudaFree`，从而把「分配」与「tensor 构造」、把「拥有」与「视图」解耦。
- CUDA IPC 通过 64 字节 handle + `cudaIpcOpenMemHandle` 让一个进程获得指向另一进程显存的本地指针；`_create_ipc_handle`/`_sync_ipc_handles` 是其 C++ 落地，且**只对裸 `cudaMalloc` 显存可靠**，故 TileScale 在 IPC 场景坚持用 `_create_tensor` 而非 `torch.empty`。
- `BaseAllocator` 是「一次 `cudaMalloc` + 线性 bump + `take_ownership=False` 视图」的显存池；分布式模式下它还构建远程基址表 `[local_rank, num_ranks, peer_base...]` 并经 `kernel.initialize` 注入，供 device 远程原语做地址翻译。
- 释放责任三分明：slab 归 allocator 析构释放、peer 映射指针不释放、临时视图不释放；任何对同一段显存的重复所有权都会导致 double-free 或 use-after-free。
- 用户态入口 `tilelang.tensor(shape, dtype, allocator=...)` 与 `tilelang.get_allocator(...)` 是这一整套机制的门面，作为可选分布式扩展在 `tilelang.__init__` 中导出。

## 7. 下一步学习建议

- **下一讲 u6-l6（DeepEP 集成）** 会用到本讲的 IPC/allocator 机制来铺底专家路由的 all-to-all 通信，可对照观察 DeepEP 示例里如何复用对称堆与基址表。
- **横向对比 u6-l4（pynvshmem）**：pynvshmem 路线用 NVSHMEM 的对称堆自动寻址，本讲的 IPC/allocator 路线则用 CUDA IPC + 显式基址表——两条路线解决的是同一问题（跨 PE 寻址），对照阅读能加深理解。
- 若想继续下探 device 侧，可阅读 `src/op/remote_copy.cc` 与 runtime 模块里 `init_table` 的 C++ 实现，看基址表如何被 device 线程查表使用（呼应 u6-l3 的 `get_remote_base_ptr`）。
