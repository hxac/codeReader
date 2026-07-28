# IPC 张量与 tilescale_ext 内存管理

## 1. 本讲目标

本讲是分布式单元（Unit 6）的第五篇。在 u6-l4 中，我们已经知道 CP-engine 路线需要「`init_dist` + `get_allocator` + `kernel.initialize(allocator=...)`」三件套，但当时把 `get_allocator` 当作黑盒。本讲要拆开这个黑盒，讲清楚：

1. `tilescale_ext` 这个 C++ 扩展到底提供了哪些「从裸指针构造 torch 张量」「跨进程共享显存」的原语。
2. `tensor_from_ptr` 的 `take_ownership` 参数如何决定「谁负责 `cudaFree`」——这是整条分布式内存链路的所有权基石。
3. CUDA IPC handle 如何让一个进程直接读写另一个进程的 GPU 显存。
4. `BaseAllocator` 如何用一次 `cudaMalloc` + IPC handle 同步，搭出一张「远程基址表」，再由 `kernel.initialize` 注入到 device 的 `__constant__` 内存。

学完后，你应当能画出两个进程通过 IPC handle 共享一段 GPU 显存的完整流程图，并准确回答「谁负责 `cudaFree`」。

## 2. 前置知识

在进入源码前，先建立四个直觉。

### 2.1 torch 张量只是一层「视图」

一个 `torch.Tensor` 并不必然「拥有」它底下的显存。PyTorch 提供 `at::from_blob(ptr, shape, deleter, options)`：它用一段**已存在**的内存 `ptr` 构造一个张量，**不拷贝数据**；当这个张量被垃圾回收时，会调用你给的 `deleter`。如果 `deleter` 什么都不做，那么这块显存就「不属于」这个张量——张量只是一个临时视图。本讲的核心原语 `tensor_from_ptr` 就是 `from_blob` 的薄封装。

### 2.2 CUDA IPC：跨进程共享 GPU 显存

CUDA IPC（Inter-Process Communication）允许一个进程把它 `cudaMalloc` 出来的显存「暴露」给另一个进程，让对方拿到一个**可直接 load/store** 的本地指针，走 NVLink/PCIe P2P 访问，无需 host 中转。两个关键 API：

- `cudaIpcGetMemHandle(handle, ptr)`：进程 A 对自己的一块显存生成一个不透明的 64 字节 handle（可序列化、可通过任意通道传给进程 B）。
- `cudaIpcOpenMemHandle(&local_ptr, handle, flags)`：进程 B 用收到的 handle 换取一个**在本进程地址空间里有效的**指针 `local_ptr`，之后对该指针的访问就是访问 A 的那块显存。

> 重要前提：IPC 只对**由 `cudaMalloc` 直接分配**的内存有效。PyTorch 默认的 caching allocator 走的是另一套分配路径，IPC 行为不可靠——这正是本讲要专门绕开它的原因。

### 2.3 对称堆与远程基址表

在 u6-l2 / u6-l3 我们见过「对称寻址」：每个 PE（进程）分配一块同构显存堆，远程地址 = 目标 PE 的堆基址 + 本地偏移。要让 device 代码能算出远程地址，就需要一张表，记录「rank → 该 rank 堆在本进程的本地指针」。本讲讲的就是这张表是怎么搭起来、怎么传到 device 的。

### 2.4 所有权（ownership）问题

跨进程共享显存时最易踩的坑是**重复释放**：如果 A 和 B 都以为自己「拥有」这块显存，析构时都 `cudaFree`，就会双重释放崩溃。因此必须明确：只有真正调用 `cudaMalloc` 的那一方负责 `cudaFree`；另一方只是「打开了一个映射」，不释放。本讲的 `take_ownership` 参数就是用来表达这个约定的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilescale_ext/__init__.py` | 编译产物 `tilescale_ext._C` 的 Python 入口，导出 5 个原语 |
| `tilelang/utils/ts_ext/tensor.cpp` | `tensor_from_ptr`（从裸指针构造张量 + 可选所有权）、`create_host_device_tensor`（pinned 映射张量）|
| `tilelang/utils/ts_ext/ipc_ops.cpp` | `create_tensor`（cudaMalloc + 自有 deleter）、`create_ipc_handle`、`sync_ipc_handles`（打开 peer handle 并写回 GPU 表）|
| `tilelang/utils/ts_ext/ts_ext_bindings.cpp` | pybind11 绑定：把 C++ 函数注册成 Python 可调用的 `tilescale_ext._C.*` |
| `tilelang/utils/allocator.py` | `BaseAllocator`：一次 cudaMalloc + IPC 同步 + bump 分配 + 远程基址表 |
| `tilelang/utils/tensor.py` | `tilelang.tensor(...)`：根据是否传 allocator 选择 allocator 分配或 `torch.empty` |
| `tilelang/distributed/utils.py` | `create_dist_tensor` 等高层封装，演示 IPC handle 在进程组里的同步用法 |
| `tilelang/jit/kernel.py` / `src/tl_templates/cuda/distributed.h` | `kernel.initialize` 把表拷进 device `__constant__ meta_data`，device 侧 `get_remote_base_ptr` 读表 |

> 注意一个容易混淆的点：`tilescale_ext` 的**源码**在 `tilelang/utils/ts_ext/`，但它的**构建产物包**却装到顶层 `tilescale_ext/` 目录（见 u1-l4）。所以你 `import tilescale_ext` 拿到的是产物，改源码要去 `ts_ext/`。

## 4. 核心概念与源码讲解

### 4.1 tensor_from_ptr：从裸指针构造张量与 take_ownership 所有权

#### 4.1.1 概念说明

`tensor_from_ptr` 是整个 `tilescale_ext` 的基石。它解决一个问题：**我手里只有一个 device 指针（一个整数），怎么把它变成一个可以在 Python 里当 `torch.Tensor` 用的对象，而且不拷贝数据、不重复释放？**

它有两个关键设计：

1. **零拷贝**：用 `at::from_blob` 直接复用传入指针指向的显存，不分配新显存、不搬数据。
2. **可选所有权**：参数 `take_ownership` 决定这个张量析构时是否调用 `cudaFree`。`true` = 「我接管，析构时 free」；`false` = 「我只是个视图，别找我 free」。

这两点合起来，支撑了分布式内存的两种用法：

- **allocator 模式**：分配器用一次 `cudaMalloc` 拿到大块显存并独占所有权；从其中切出的每个张量都用 `take_ownership=false`，绝不各自 free。
- **独立分配模式**（`_create_tensor`）：张量自己 `cudaMalloc`、自己 `cudaFree`（见 4.2）。

#### 4.1.2 核心流程

```
tensor_from_ptr(ptr, shape, dtype, device, take_ownership)
  ├─ 1. 校验 ptr != 0，把 uint64 还原成 void*
  ├─ 2. dtype 字符串 → at::ScalarType
  ├─ 3. 计算 nelems = prod(shape)（含溢出检查）
  ├─ 4. 构造 deleter：
  │      take_ownership=true  → 析构时 cudaFree(saved_ptr)
  │      take_ownership=false → 空操作 deleter
  └─ 5. at::from_blob(data_ptr, shape, deleter, CUDA options)  # 零拷贝
```

要点：被 `from_blob` 包出来的张量与原指针**指向同一块显存**，改一边另一边立即可见（同 device、同 stream 下）。`deleter` 的差异是所有权的唯一来源。

#### 4.1.3 源码精读

函数定义在 [tilelang/utils/ts_ext/tensor.cpp:49-87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L49-L87)，其中指针还原与 dtype 解析如下（把 Python 传进来的 `uint64` 整数还原成 C++ `void*`，并按字符串名查 `ScalarType`）：

```cpp
void *data_ptr = reinterpret_cast<void *>(static_cast<uintptr_t>(ptr_val));
at::ScalarType st = dtype_from_string(dtype);
auto options = torch::TensorOptions().dtype(st).device(torch::kCUDA, static_cast<int>(device));
```

所有权的核心——deleter 的二选一，在 [tilelang/utils/ts_ext/tensor.cpp:67-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L67-L80)：

```cpp
std::function<void(void *)> deleter;
if (take_ownership) {
  uint64_t saved_ptr = ptr_val;                 // 按值捕获，防止悬空
  deleter = [saved_ptr](void *) {
    void *p = reinterpret_cast<void *>(static_cast<uintptr_t>(saved_ptr));
    cudaError_t cerr = cudaFree(p);             // 析构时释放
    ...
  };
} else {
  deleter = [](void *) {};                       // 空操作：不释放
}
```

最后用 `from_blob` 落地（[tensor.cpp:82-86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L82-L86)）：`nelems == 0` 时直接 `torch::empty`，否则 `at::from_blob(data_ptr, shape, deleter, options)`。

Python 侧的导出在 [tilescale_ext/__init__.py:1-7](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilescale_ext/__init__.py#L1-L7)，绑定签名（含默认值 `take_ownership=false`）在 [tilelang/utils/ts_ext/ts_ext_bindings.cpp:12-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ts_ext_bindings.cpp#L12-L14)。

#### 4.1.4 代码实践（单卡可运行）

**实践目标**：验证 `tensor_from_ptr` 是「零拷贝视图」，并直观感受 `take_ownership` 的作用。

**操作步骤**（示例代码，需有 CUDA 环境）：

```python
# 示例代码：演示 tensor_from_ptr 的零拷贝与所有权
import torch
from tilescale_ext import tensor_from_ptr

src = torch.arange(16, dtype=torch.float32, device="cuda")
# take_ownership=False：t 是 src 的视图，不持有所有权
t = tensor_from_ptr(src.data_ptr(), list(src.shape), "float32", 0, False)

t[0] = 999.0                       # 改视图
print(src[0].item())               # 预期 999.0：同一块显存

del t                              # 视图析构：deleter 是空操作，不会 free
print(src[0].item())               # 仍正常：src 仍持有显存
```

**需要观察的现象**：修改 `t` 后 `src` 同步变化（证明零拷贝）；删除 `t` 后 `src` 仍可用（证明 `take_ownership=False` 不释放）。

**预期结果**：两次打印分别为 `999.0`、`999.0`。若把 `False` 改成 `True`，`del t` 会触发 `cudaFree`，之后访问 `src` 即为已释放内存（未定义行为）。

> 若本地无 `tilescale_ext` 编译产物，此步标「待本地验证」；可仅阅读上面源码确认 deleter 分支即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `take_ownership=true` 的 deleter 要 `uint64_t saved_ptr = ptr_val;` 按值捕获，而不是捕获 `ptr_val` 的引用？

**参考答案**：因为 `ptr_val` 是函数参数（局部变量），函数返回后即销毁；若按引用捕获，deleter 在张量析构时（远晚于函数返回）会访问已失效的栈变量。按值拷贝一份到堆上的 lambda 闭包里，才能在析构时安全还原指针。

**练习 2**：allocator 从大块显存里切张量时，应该用 `take_ownership=true` 还是 `false`？为什么？

**参考答案**：`false`。因为大块显存的所有权属于 allocator（它调的 `cudaMalloc`），切出来的张量只是视图。若用 `true`，每个视图析构都会 `cudaFree` 同一块基址，导致重复释放崩溃。

---

### 4.2 IPC handle 创建与同步：跨进程共享显存

#### 4.2.1 概念说明

`tensor_from_ptr` 只解决「单进程内把裸指针包成张量」。分布式场景下，进程 B 要访问进程 A 的显存，必须先用 CUDA IPC 建立「跨进程映射」。这一步由两个原语完成：

- `_create_ipc_handle(ptr)`：对本进程的一块 `cudaMalloc` 显存生成 IPC handle（64 字节，可序列化）。
- `_sync_ipc_handles(rank, device_ids, buffer_ptrs_gpu, handles, ...)`：把收到的所有 peer handle 用 `cudaIpcOpenMemHandle` 打开，换取本进程可用的本地指针，并把这些指针写进一块 GPU 缓冲。

注意：这条路径**绕开了 NVSHMEM**。u6-l2 讲的对称堆由 NVSHMEM 运行时自动建立；而本讲的 IPC 路线是「手搓对称堆」——用 CUDA IPC + `torch.distributed` 的 `all_gather_object` 手动交换 handle、手动建立映射，最终得到与 NVSHMEM 等价的「rank → 远程堆本地指针」表。这条路线不依赖 `pynvshmem`，是 CP-engine 路线的运行时基础。

#### 4.2.2 核心流程

一次完整的「进程 A 暴露显存 → 进程 B 拿到本地指针」流程：

```
进程 A:  buf = cudaMalloc(N)                       # 必须是 cudaMalloc
         handle_A = cudaIpcGetMemHandle(buf)        # 生成 handle
         ── 通过 torch.distributed.all_gather_object 把 handle 广播给所有 peer ──>

进程 B:  收到 handle_A
         local_ptr = cudaIpcOpenMemHandle(handle_A) # 打开：得到本进程可用的指针
         # 此后 B 对 local_ptr 的访问 = 直接读写 A 的 buf（走 NVLink/PCIe P2P）
```

`_sync_ipc_handles` 在进程组里对**每个 peer**（自己之外的）都做一次 `OpenMemHandle`，把结果填进一个长度为 `num_ranks` 的指针数组，再 `cudaMemcpy` 到 GPU。这样 device 代码就能查表得到任意 peer 堆的本地基址。

#### 4.2.3 源码精读

**生成 handle** 在 [tilelang/utils/ts_ext/ipc_ops.cpp:63-68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L63-L68)，直接调用 `cudaIpcGetMemHandle`，把 64 字节 handle 包成 `py::bytearray` 返回：

```cpp
py::bytearray create_ipc_handle(void *ptr) {
  cudaIpcMemHandle_t handle{};
  CUDA_CHECK(cudaIpcGetMemHandle(&handle, ptr));
  return py::bytearray(reinterpret_cast<const char *>(handle.reserved),
                       CUDA_IPC_HANDLE_SIZE);
}
```

**同步（打开 peer handle + 写回 GPU）** 在 [tilelang/utils/ts_ext/ipc_ops.cpp:70-98](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L70-L98)，核心循环：

```cpp
for (int i = 0, offset = rdma_rank * num; i < num; ++i) {
  // 把 bytearray 还原成 handle 字节
  std::memcpy(ipc_handles[i].reserved, s.data(), CUDA_IPC_HANDLE_SIZE);
  if (offset + i != rank) {                       // 跳过自己：自己的基址无需 IPC
    CUDA_CHECK(cudaIpcOpenMemHandle(&buffer_ptrs[i], ipc_handles[i],
                                    cudaIpcMemLazyEnablePeerAccess));
  }
}
// 把解析出的指针数组拷到 GPU，供 device 查表
CUDA_CHECK(cudaMemcpy(buffer_ptrs_gpu, buffer_ptrs.data(),
                      sizeof(void *) * buffer_ptrs.size(), cudaMemcpyHostToDevice));
```

两点说明：

- `rdma_rank = 0`、`offset = rdma_rank * num` 表明当前实现是**单节点假设**（offset 恒为 0，handle 数组按节点内 rank 线性排列）；多节点 RDMA 分段是预留扩展位，尚未启用。
- `cudaIpcMemLazyEnablePeerAccess`：打开时按需启用 P2P 访问（NVLink/PCIe），无需显式 `cudaDeviceEnablePeerAccess`。

绑定层把 Python 传入的 `uintptr_t` 还原成 `void**`，见 [ts_ext_bindings.cpp:36-48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ts_ext_bindings.cpp#L36-L48)。

**为什么必须用 `cudaMalloc`？** 同文件里还有一个 `create_tensor`（[ipc_ops.cpp:39-61](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L39-L61)），它显式 `cudaMalloc(&ptr, bytes)` 再 `from_blob`，且 deleter 里 `cudaFree(p)`——**始终自带所有权**。Python 侧 `tilelang/distributed/utils.py:100-102` 的 `create_tensor` 正是调用它，并留有注释「IPC only works with tensors explicitly allocated by `cudaMalloc` somehow」。这就是 2.2 节前提的代码佐证。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

**实践目标**：画出两个进程通过 IPC handle 共享一段 GPU 显存的完整流程图，并回答谁负责 `cudaFree`。

**操作步骤**：

1. 阅读 [ipc_ops.cpp:63-98](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L63-L98) 与 [tensor.cpp:49-87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/tensor.cpp#L49-L87)。
2. 阅读 Python 高层封装 [tilelang/distributed/utils.py:106-129](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L106-L129)（`get_local_ipc_handle` + `create_dist_tensor`），看 handle 是如何用 `dist.all_gather_object` 在进程组里交换的。
3. 画出如下时序图（两个 rank，rank0 暴露显存，rank1 访问）：

```
rank0 (owner)                          rank1 (peer)
─────────────                          ────────────
buf = cudaMalloc(N)  ──────────────>   (尚未持有)
h0 = _create_ipc_handle(buf)
all_gather_object([h0,h1])  <────────> all_gather_object([h0,h1])
                       └────────────>  local = cudaIpcOpenMemHandle(h0)
                                       t = tensor_from_ptr(local, ..., take_ownership=False)
                                       # rank1 通过 t 直接读写 rank0 的 buf
```

**需要观察的现象 / 预期结果**：

- 流程图中应清晰体现：`cudaMalloc` 只在 rank0 发生一次；rank1 只做 `OpenMemHandle`（映射）+ `tensor_from_ptr(take_ownership=False)`（视图）。
- **谁负责 `cudaFree`**：只有 rank0（owner）。rank0 用 allocator 或 `create_tensor` 分配并持有所有权；rank1 的张量 `take_ownership=False`，deleter 为空，绝不 free。
- 若本地有多卡 + 已编译 `tilescale_ext`，可参考 `examples/distributed/` 用 `tilelang/distributed/launch.sh` 起两个进程实际跑通；否则标「待本地验证」。

> 关于 `cudaIpcCloseMemHandle`：当前 `_sync_ipc_handles` 只 `Open` 不显式 `Close`，依赖进程退出时驱动回收。这是已知简化，阅读时留意即可。

#### 4.2.5 小练习与答案

**练习 1**：`sync_ipc_handles` 里为什么有 `if (offset + i != rank)` 这个判断？如果删掉会怎样？

**参考答案**：它跳过「自己」。自己的堆基址无需也不能用 `cudaIpcOpenMemHandle` 打开（handle 是别的进程生成的，自己没有自己的 handle）。自己的基址由 Python 侧直接填入（`buffer_ptrs[local_rank] = self._base_ptr.value`）。若删掉判断、对自己的 handle 也 Open，会因 handle 不匹配而报错。

**练习 2**：IPC 路线和 NVSHMEM 路线（u6-l2）都能建立「跨 PE 可寻址显存」，二者在运行时准备上最大的区别是什么？

**参考答案**：NVSHMEM 路线靠 `pynvshmem.init_nvshmem_by_uniqueid` 由 NVSHMEM 运行时自动建立对称堆，device 侧用 `get_pe` / `putmem` 等 `nvshmem*` API；IPC 路线靠 `torch.distributed.all_gather_object` 手动交换 CUDA IPC handle、手动 `cudaIpcOpenMemHandle`，device 侧用 `get_rank` / `put_block` 等模板直接 load/store 远程地址（不调 `nvshmem*`）。前者依赖 `pynvshmem`，后者依赖 `tilescale_ext`。

---

### 4.3 allocator 与 tensor Python 封装：搭出远程基址表

#### 4.3.1 概念说明

有了 4.1 的「裸指针 → 张量」和 4.2 的「跨进程映射」，还缺一个把这些粘起来的编排者——`BaseAllocator`（`tilelang/utils/allocator.py`）。它做三件事：

1. **一次性 `cudaMalloc` 一大块显存**（满足 IPC 前提），自己独占所有权。
2. **用 IPC 同步出每个 peer 堆在本进程的基址**，拼成一张「远程基址表」（rank、num_ranks、各 peer 基址）。
3. **bump 分配**：从大块显存里顺序切出小张量（`take_ownership=False`），并支持「同偏移切出所有 peer 的对称张量」（`return_peers=True`）。

而 `tilelang.tensor(...)`（`tilelang/utils/tensor.py`）是面向用户的分配入口：传 `allocator` 走上面的分布式分配，不传则退化为 `torch.empty`。

这张「远程基址表」正是 u6-l3 中 `remote_addr = get_remote_base_ptr(peer) + (addr − base[me])` 里 `get_remote_base_ptr` 所查的那张表。

#### 4.3.2 核心流程

`BaseAllocator(is_distributed=True)` 的初始化与分配：

```
__init__
  ├─ _alloc(): cudaMalloc(size) → _base_ptr（基址），_ptr（bump 游标）
  └─ _init_table():                                      # 仅分布式
       ├─ all_gather_object(device_ids, local_rank)      # 收集各 peer 的 device id
       ├─ h = _create_ipc_handle(_base_ptr)              # 本进程基址的 handle
       ├─ all_gather_object(ipc_handles, h)              # 全交换 handle
       ├─ buffer_ptrs = empty(num_ranks, uint64, cuda)
       ├─ _sync_ipc_handles(...)                         # 打开 peer handle → buffer_ptrs[i]
       ├─ buffer_ptrs[local_rank] = _base_ptr            # 自己的基址直接填
       └─ table = [local_rank, num_ranks, *buffer_ptrs]  # CPU uint64 表

_allocate_tensor(shape, dtype, return_peers, take_ownership=False)
  ├─ 对齐(256) + 越界检查
  ├─ t = tensor_from_ptr(_ptr, shape, dtype, device, False)   # 视图，不 free
  ├─ if return_peers: 对每个 peer i，用 buffer_ptrs[i]+offset 切出对称视图
  └─ 推进 _ptr（除非 take_ownership=True）
```

表的布局（`table_size = 2 + num_ranks`）：

| 下标 | 含义 |
| --- | --- |
| `table[0]` | 本进程 rank |
| `table[1]` | 进程组大小（num_ranks）|
| `table[2 + i]` | peer i 的堆在本进程的本地基址（自己即 `_base_ptr`）|

这张表随后由 `kernel.initialize` 拷进 device 的 `__constant__` 内存。

#### 4.3.3 源码精读

**一次 `cudaMalloc`**（注意它通过 ctypes 直接调 CUDA Runtime，不走 torch caching allocator）在 [tilelang/utils/allocator.py:118-128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L118-L128)：

```python
rc = _libcudart.cudaMalloc(ctypes.byref(self._base_ptr), ctypes.c_size_t(self.size))
...
self._ptr.value = self._base_ptr.value   # _ptr 是 bump 游标，初始 = 基址
```

**IPC 同步 + 拼表** 在 [tilelang/utils/allocator.py:139-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L139-L161)，关键几句（交换 handle、打开、填自己的基址、拼 CPU 表）：

```python
local_ipc_handle = _create_ipc_handle(self._base_ptr.value)
dist.all_gather_object(ipc_handles, local_ipc_handle, self._group)
buffer_ptrs = torch.empty(self._group.size(), dtype=torch.uint64, device="cuda")
_sync_ipc_handles(self._local_rank, device_ids,
                  ctypes.c_void_p(buffer_ptrs.data_ptr()).value, ipc_handles, None)
buffer_ptrs[self._local_rank] = self._base_ptr.value      # 自己的基址不走 IPC
...
self._table[0] = self._local_rank
self._table[1] = self._group.size()
self._table[2:] = buffer_ptrs                              # 各 peer 基址
```

**bump 分配 + 对称 peer 张量** 在 [tilelang/utils/allocator.py:166-226](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L166-L226)。切本进程视图用 `take_ownership=False`（第 199 行）：

```python
t = tensor_from_ptr(cur_ptr_val, shape, dtype_str, self._device, take_ownership)
```

切 peer 的对称视图时，用**同一个偏移** `current_offset` 加到各 peer 基址上（第 207 行）——这就是对称寻址的来源：

```python
peer_ptr_val = int(self._buffer_ptrs[i]) + current_offset
peer_t = tensor_from_ptr(peer_ptr_val, shape, dtype_str, peer_device, False)
```

**析构释放**（唯一一次 `cudaFree`）在 [allocator.py:130-137](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L130-L137) 与 [allocator.py:240-242](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L240-L242)：`__del__` 调 `_free()` → `cudaFree(_base_ptr)`。所有切出的张量因 `take_ownership=False` 都不释放——所有权收口在 allocator 一处。

**用户入口 `tilelang.tensor`** 在 [tilelang/utils/tensor.py:38-66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L38-L66)：传 `allocator` 则调 `allocator._allocate_tensor`，否则 `torch.empty`。工厂 `get_allocator` 在 [allocator.py:245-255](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L245-L255)。

#### 4.3.4 表如何到达 device（闭环）

allocator 产出的 CPU 表，经 `kernel.initialize` 注入 device（[tilelang/jit/kernel.py:465-495](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L465-L495)）：

```python
def initialize(self, allocator, stream=None):
    result = self.adapter.init_table(
        allocator.table.data_ptr(), allocator.table_size, stream_val)
```

adapter 把 host 表 `cudaMemcpyToSymbolAsync` 到 device `__constant__` 符号 `meta_data`（[tilelang/jit/adapter/wrapper.py:58-78](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/wrapper.py#L58-L78)）。device 侧的查表函数在 [src/tl_templates/cuda/distributed.h:5-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/distributed.h#L5-L14)：

```cpp
extern "C" extern __constant__ uint64_t meta_data[1024];
TL_DEVICE uint64_t get_rank()                         { return meta_data[0]; }
TL_DEVICE uint64_t get_num_ranks()                    { return meta_data[1]; }
TL_DEVICE uint64_t get_remote_base_ptr(uint64_t rank) { return meta_data[2 + rank]; }
```

下标 `0 / 1 / 2+rank` 与 4.3.2 的表布局**完全对齐**——这正是 allocator 与 device 查表之间的隐式契约。于是 device 端的远程寻址公式成立：

\[
\text{remote\_addr} = \text{get\_remote\_base\_ptr}(\text{peer}) + (\text{addr} - \text{base}[\text{me}])
\]

其中 \(\text{base}[\text{me}]\) 即本进程 allocator 的 `_base_ptr`，\(\text{addr}-\text{base}[\text{me}]\) 就是 4.3.2 里的 `current_offset`。device 代码据此算出任意 peer 上「同一逻辑偏移」处的远程地址，直接 load/store（即 u6-l3 的 CP-engine 路线）。

#### 4.3.5 代码实践（源码阅读型）

**实践目标**：把「allocator → kernel.initialize → device meta_data」这条数据通路在源码里走一遍，确认表的字段契约一致。

**操作步骤**：

1. 在 [allocator.py:156-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L156-L161) 确认 `table[0]=rank, table[1]=num_ranks, table[2:]=基址`。
2. 在 [kernel.py:479-483](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L479-L483) 确认它把 `table.data_ptr()` 与 `table_size` 传给 `init_table`。
3. 在 [distributed.h:8-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/distributed.h#L8-L14) 确认 device 读 `meta_data[0/1/2+rank]`。
4. 对照 [examples/distributed/example_gemm_rs_overlapped.py:130-134](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_gemm_rs_overlapped.py#L130-L134)，看真实示例如何 `get_allocator(...)` 后立刻 `gemm_func.initialize(allocator=allocator)`。

**需要观察的现象 / 预期结果**：三处下标完全对齐，证明契约一致；示例里 allocator 创建后必须先 `initialize` 再调用 kernel，否则 `meta_data` 未初始化、`get_remote_base_ptr` 返回垃圾值。实际运行需多卡 + `tilescale_ext`，标「待本地验证」。

#### 4.3.6 小练习与答案

**练习 1**：allocator 切出的张量用 `take_ownership=False`，那这块显存最终由谁释放？如果 allocator 还没析构、进程就结束了会怎样？

**参考答案**：由 allocator 的 `__del__` → `_free()` → `cudaFree(_base_ptr)` 统一释放。allocator 持有 `_base_ptr`，所有视图只是借用。若进程在 allocator 析构前结束，CUDA 驱动会在 context 销毁时回收该进程所有显存，不会泄漏（但正常的 `cudaFree` 链路未走完）。

**练习 2**：`return_peers=True` 时，peer 张量的指针是 `buffer_ptrs[i] + current_offset`。为什么用 `current_offset` 而不是各自的独立游标？

**参考答案**：因为对称寻址要求「同一逻辑偏移在所有 peer 上指向逻辑同一位置」。`current_offset` 是本进程刚切出的那块在大块里的偏移；由于各 peer 的大块是同构对称堆（同 `size`、同 bump 顺序），用同一偏移就能定位到 peer 上对应的那块。若各用独立游标，偏移会错位，远程寻址公式失效。

**练习 3**：为什么 allocator 用 ctypes 直接调 `cudaMalloc`，而不是 `torch.empty`？

**参考答案**：两个原因。其一，IPC 要求显存由 `cudaMalloc` 直接分配（见 4.2.3 的注释），torch caching allocator 的内存不保证可 IPC。其二，allocator 要精确控制基址与 bump 游标、并独占所有权，绕开 caching allocator 的复用 / 池化语义更可控。

## 5. 综合实践

把本讲三个模块串起来：**写一段最小化的「两个进程、一个 allocator、一次远程写」说明文档（含流程图与所有权标注）**。

要求：

1. 用自己的话画出从 `init_dist` → `get_allocator`（内部 `cudaMalloc` + IPC 同步 + 拼表）→ `kernel.initialize`（表注入 `meta_data`）→ device `get_remote_base_ptr` 查表 → 远程 load/store 的**端到端数据通路**。
2. 在图上用三种颜色/标注区分：
   - 谁调了 `cudaMalloc`（allocator，唯一 owner）；
   - 谁调了 `cudaIpcGetMemHandle` / `cudaIpcOpenMemHandle`（每个 rank 各一次 get、对每个 peer 各一次 open）；
   - 谁调了 `cudaFree`（仅 allocator 析构时一次）。
3. 验证你的图与源码一致：参考 [allocator.py:118-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L118-L161)、[ipc_ops.cpp:63-98](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/ts_ext/ipc_ops.cpp#L63-L98)、[distributed.h:5-14](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/distributed.h#L5-L14)。
4. 用一句话回答练习核心问题：「**谁负责 `cudaFree`？**」——答：**只有持有 `_base_ptr` 的 allocator**；所有 `tensor_from_ptr(take_ownership=False)` 视图与所有 `cudaIpcOpenMemHandle` 映射都不释放。

> 若本地有多卡环境，可进一步用 `examples/distributed/example_gemm_rs_overlapped.py`（依赖 `tilelang/distributed/launch.sh`）实际跑通，把流程图与真实日志对应起来；无环境则交付源码阅读版的图与说明即可。

## 6. 本讲小结

- `tilescale_ext` 是一个 C++ 扩展（产物 `tilescale_ext._C`，源码在 `tilelang/utils/ts_ext/`），提供 5 个原语：`tensor_from_ptr`、`_create_tensor`、`create_host_device_tensor`、`_create_ipc_handle`、`_sync_ipc_handles`。
- `tensor_from_ptr` 用 `at::from_blob` 零拷贝地把裸指针包成 torch 张量；`take_ownership` 决定 deleter 是否 `cudaFree`，是整条链路的所有权开关。
- CUDA IPC（`cudaIpcGetMemHandle` + `cudaIpcOpenMemHandle`）让一个进程直接读写另一个进程的 `cudaMalloc` 显存；`_sync_ipc_handles` 批量打开 peer handle 并把解析出的基址写进 GPU 缓冲。IPC 必须基于 `cudaMalloc`，故 allocator 与 `_create_tensor` 都绕开 torch caching allocator。
- `BaseAllocator` 一次 `cudaMalloc` 独占所有权，用 IPC 同步出各 peer 基址，拼成表 `[rank, num_ranks, *基址]`，并按 bump + 对称偏移切出无所有权视图；`tilelang.tensor(allocator=...)` 是用户入口。
- 这张表经 `kernel.initialize` → `cudaMemcpyToSymbolAsync` 注入 device `__constant__ meta_data`，device 侧 `get_rank / get_num_ranks / get_remote_base_ptr` 按 `[0/1/2+rank]` 读取，与 allocator 的表布局严格对齐。
- 所有权收口在 allocator：**只有它 `cudaFree` 一次**；所有视图（`take_ownership=False`）与 IPC 映射都不释放。

## 7. 下一步学习建议

- **u6-l6（DeepEP 集成）**：DeepEP 的 `EPBuffer` 正是用 `tilelang.get_allocator` 预分配对称堆（见 `examples/distributed/deepseek_deepep/buffer.py`），是本讲 allocator 模式的大规模实战，建议接着读它如何在一块对称堆上切出十几个通信缓冲。
- **u6-l7（分布式实战）**：`example_summa.py` / `example_gemm_rs_overlapped.py` 会把本讲的「远程基址表 + CP-engine 远程拷贝」与计算 overlap 结合，是检验你是否真正理解表注入的最好材料。
- **回看 u6-l3 / u6-l4**：现在再读 u6-l3 的「`kernel.initialize(allocator=...)` 注入基址表」与 u6-l4 的「CP-engine 运行时准备」，应能对应到本讲的 `meta_data` 表与 IPC 同步细节，把黑盒彻底打通。
- **若对扩展机制感兴趣**：可对照 `tilelang/utils/ts_ext/setup.py` 与 `ts_ext_bindings.cpp`，理解一个独立的 torch C++ extension 是如何用 `CUDAExtension` + pybind11 注册并装到 `tilescale_ext` 包里的，为后续自定义 device 原语打基础。
