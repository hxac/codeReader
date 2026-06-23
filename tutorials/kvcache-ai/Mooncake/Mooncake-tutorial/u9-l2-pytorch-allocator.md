# PyTorch Allocator 集成：零拷贝显存

> 本讲对应讲义规格 `u9-l2`，依赖 [u5-l4 Python Store API](u5-l4-python-store-api.md)。
> 当前 HEAD：`1f7f71a18a9dc48e9901d8293c5c3625ba166939`。所有源码链接均基于此提交。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 PyTorch 的 **可插拔 Allocator（CUDAPluggableAllocator / NPUPluggableAllocator）** 接入机制：PyTorch 在缓存分配器之下调用用户提供的 C 函数来分配/释放显存。
2. 读懂 `NVLinkAllocator` 是如何通过 `ctypes` 动态加载 `.so`、调用 `mc_allocator_probe` 来**探测 fabric 内存支持**，并把设备归类为 `cudaMalloc` 或 `cuMemCreate` 后端。
3. 理解一个关键细节：**探测（probe）** 与**实际分配（allocate）** 是两条独立路径——`mc_allocator_malloc` 始终走 CUDA VMM（`cuMemCreate`）路径，其内部唯一的"回退"是 fabric handle 类型的降级，而不是退回 `cudaMalloc`。
4. 了解 Ascend NPU 的 `UBShmemAllocator` 如何用 `aclrtMallocPhysical` + `aclrtReserveMemAddress` + `aclrtMapMem` 实现一条等价的 fabric 分配路径。
5. 知道这些 `.so` 与 `.py` 是如何在构建期被有条件地装进 Python 包的。

## 2. 前置知识

### 2.1 PyTorch 的两层显存分配

PyTorch 默认用**缓存分配器（caching allocator）**管理显存：它一次性向驱动申请大块，再切分给每个 tensor，从而避免频繁的系统调用。但缓存分配器底层最终还是要调用驱动的"原始分配"接口。PyTorch 允许你替换这个最底层的原始分配器——这就是**可插拔 Allocator（Pluggable Allocator）**：

- 你提供一个编译好的动态库（`.so`），导出形如 `malloc(size, device, stream)` / `free(ptr, size, device, stream)` 的 C 函数。
- PyTorch 用 `CUDAPluggableAllocator(so_path, malloc_sym, free_sym)` 把它包成一个对象。
- 再用 `torch.cuda.memory.change_current_allocator(allocator)` 把它装进去（必须在任何 CUDA 分配之前调用）。

此后 PyTorch 创建 tensor 时，底层的显存就来自**你的** `.so`。

> ⚠️ 这一步必须在**第一次分配显存之前**完成，否则 PyTorch 会拒绝替换。这是新手最常踩的坑。

### 2.2 CUDA VMM 与 fabric 内存

普通 `cudaMalloc` 是一个"黑盒"分配，你拿不到内存的物理句柄。而 Mooncake 需要把这块显存**注册给传输引擎做零拷贝 RDMA/NVLink 传输**，因此必须用更底层的 **CUDA VMM（Virtual Memory Management）** API：

- `cuMemCreate`：创建一个物理内存句柄（`CUmemGenericAllocationHandle`）。
- `cuMemAddressReserve`：在虚拟地址空间预留一段。
- `cuMemMap`：把物理句柄映射到虚拟地址。
- `cuMemSetAccess`：设置哪些设备可以访问这段内存。

其中的 `CU_MEM_HANDLE_TYPE_FABRIC`（fabric handle）表示这块内存**可以被导出到 fabric（NVLink/InfiniBand）上供其他 GPU 或节点访问**——这正是跨节点 KV cache 零拷贝的基础。

### 2.3 与 u5-l4 的衔接

[u5-l4](u5-l4-python-store-api.md) 讲了 `MooncakeDistributedStore.put_tensor` / `register_buffer` 这类**零拷贝**接口：它们的前提是这块 GPU 内存能被传输引擎直接注册。本讲从另一头切入——**让 PyTorch tensor 自己的存储就诞生在 VMM/fabric 内存里**，从而免去"先 cudaMalloc、再迁移/注册"的拷贝。两端合起来才构成完整的零拷贝链路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mooncake-integration/allocator.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py) | Python 侧入口。定义 `MemoryBackend` 枚举、`NVLinkAllocator`（CUDA/NVLink）、`BarexAllocator`（阿里 Barex）。 |
| [mooncake-integration/allocator_ascend_npu.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py) | Ascend NPU 侧入口。定义 `UBShmemAllocator`，对接 `torch_npu` 的 `NPUPluggableAllocator`。 |
| [mooncake-integration/fabric_allocator_utils.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/fabric_allocator_utils.py) | 两个 allocator 共用的支撑层：定位 `.so` 路径、用 `ctypes` 调用探测函数。 |
| [mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp) | `nvlink_allocator.so` 的 C++ 源码：探测 fabric、VMM 分配/释放、导出 `mc_allocator_*` 符号。 |
| [mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp) | `ubshmem_fabric_allocator.so` 的 C++ 源码：Ascend aclrt 的 fabric 分配/释放。 |
| [mooncake-transfer-engine/include/cuda_alike.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/cuda_alike.h) | 厂商抽象头：根据 `USE_CUDA/USE_HIP/USE_MUSA/USE_UBSHMEM/...` 选择对应的 GPU 头文件。 |
| [mooncake-integration/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt) | 构建期决定是否把 `.so` 与 `.py` 安装进 Python 包。 |

## 4. 核心概念与源码讲解

### 4.1 PyTorch 可插拔 Allocator 机制

#### 4.1.1 概念说明

PyTorch 的可插拔 allocator 只替换"原始分配"这一层：上层仍然是 PyTorch 的缓存分配器，下层从 `cudaMalloc` 换成你 `.so` 里的函数。Mooncake 利用这一点，让 tensor 的存储直接来自可被 fabric 导出的 VMM 内存。

这套机制有两套并行实现：

- **CUDA 侧**：`torch.cuda.memory.CUDAPluggableAllocator`，函数签名带 `stream`：
  `void* malloc(ssize_t size, int device, cudaStream_t stream)`。
- **Ascend NPU 侧**：`torch_npu.npu.memory.NPUPluggableAllocator`，函数签名**不带** `stream`：
  `void* malloc(ssize_t size, int device)`。

这正是为什么两个 C++ 文件里 `mc_allocator_malloc` 的参数列表不同。

#### 4.1.2 核心流程

```text
用户代码
  │  torch.cuda.memory.change_current_allocator(allocator)
  ▼
PyTorch 缓存分配器（不变）
  │  需要一块新显存时
  ▼
PluggableAllocator 持有的 .so 符号
  │  mc_allocator_malloc(size, device, stream)
  ▼
Mooncake 的 VMM/fabric 分配（cuMemCreate / aclrtMallocPhysical ...）
  │  返回 void* 指针
  ▼
PyTorch 把这块内存交给 tensor
```

#### 4.1.3 源码精读

两个 allocator 类都把 `.so` 包成 PyTorch 的可插拔对象。CUDA 侧的构造在：

- [allocator.py:86-88](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L86-L88)：用 `nvlink_allocator.so` 里的 `mc_allocator_malloc` / `mc_allocator_free` 构造 `CUDAPluggableAllocator`。三个参数正好是"库路径、malloc 符号名、free 符号名"。

```python
cls._instances[device] = CUDAPluggableAllocator(
    so_path, "mc_allocator_malloc", "mc_allocator_free"
)
```

Ascend 侧对应 [allocator_ascend_npu.py:68-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L68-L70)，换成 `NPUPluggableAllocator`，符号名相同（实现来自 `ubshmem_fabric_allocator.so`）。

注意 `import` 来源不同，决定了签名差异：

- [allocator.py:10](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L10)：`from torch.cuda.memory import CUDAPluggableAllocator`。
- [allocator_ascend_npu.py:7](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L7)：`from torch_npu.npu.memory import NPUPluggableAllocator`。

#### 4.1.4 代码实践

**实践目标**：理解"替换必须在首次分配之前"这一约束，并掌握标准的接入调用。

**操作步骤**（下方为**示例代码**，仓库本身不含模型集成示例，消费方通常是 SGLang，见 `SGLANG_MOONCAKE_CUSTOM_MEM_POOL` 字样）：

```python
# 示例代码：把 Mooncake allocator 接入一个最小的 PyTorch 模型
import torch
from mooncake.allocator import NVLinkAllocator      # 包安装后的导入路径

# ① 必须在任何 cuda 分配之前完成替换
alloc = NVLinkAllocator.get_allocator(torch.device("cuda:0"))
torch.cuda.memory.change_current_allocator(alloc)

# ② 之后 tensor 的底层显存来自 mc_allocator_malloc
model = torch.nn.Linear(1024, 1024, device="cuda:0")
x = torch.randn(8, 1024, device="cuda:0")
y = model(x)
```

**需要观察的现象**：

- 若顺序正确（先 `change_current_allocator` 再建模型），程序正常运行；若顺序颠倒，PyTorch 会抛出 "cannot change allocator after allocations" 类错误。
- 若想确认 `mc_allocator_malloc` 真的被调用，可用 `ltrace -e mc_allocator_malloc python your_script.py` 观察。

**预期结果**：模型权重与激活所在的显存由 `nvlink_allocator.so` 通过 VMM 路径分配。

**待本地验证**：以上运行需要真实 CUDA 环境 + 安装了 `nvlink_allocator.so` 的 mooncake 包；当前无 GPU 环境时请作为"源码阅读型实践"理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `change_current_allocator` 必须在第一次分配显存之前调用？
**答案**：PyTorch 默认 allocator 一旦已经分配过内存，缓存分配器里就持有了指向旧 allocator 的指针与已分配块；中途替换底层 allocator 会让"释放旧块"找不到对应的 free 函数，因此 PyTorch 强制要求替换发生在任何分配之前。

**练习 2**：CUDA 版与 Ascend 版的 `mc_allocator_malloc` 函数签名差在哪？为什么？
**答案**：CUDA 版是 `void* mc_allocator_malloc(ssize_t size, int device, cudaStream_t stream)`，Ascend 版是 `void* mc_allocator_malloc(ssize_t size, int device)`（无 stream）。因为 `CUDAPluggableAllocator` 要求带 stream 的签名，而 `NPUPluggableAllocator` 不带。

---

### 4.2 公共支撑层：so 定位与后端探测（fabric_allocator_utils.py）

#### 4.2.1 概念说明

两个 allocator 类都要做两件重复的事：① 在已安装的 `mooncake` 包里找到那个 `.so`；② 用 `ctypes` 打开它、调用其中的探测函数。`fabric_allocator_utils.py` 把这两件事抽成通用函数，被 CUDA 侧与 Ascend 侧复用。

#### 4.2.2 核心流程

```text
get_mooncake_so_path("xxx.so", 报错文案)
  ├─ 先试 importlib.resources.path("mooncake", "xxx.so")
  ├─ 再退到 os.path.dirname(mooncake.__file__) + "/xxx.so"
  └─ 都没有 → raise ImportError(报错文案)

probe_allocator_backend(so_path, 符号名, 返回类型, 失败默认值)
  ├─ ctypes.CDLL(so_path)  动态加载
  ├─ getattr(lib, 符号名)   取函数
  ├─ 设置 argtypes=[c_int]、restype
  ├─ 调用 probe(0)
  └─ AttributeError / 其它异常 → 返回"失败默认值"
```

#### 4.2.3 源码精读

- [fabric_allocator_utils.py:12-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/fabric_allocator_utils.py#L12-L30)：`get_mooncake_so_path` 两段式查找——先用 `importlib.resources`（对打包进包的资源最规范），再用文件系统路径兜底，最后抛出带具体版本要求的 `ImportError`（如要求 `mooncake-transfer-engine >= 0.3.3.post2`）。
- [fabric_allocator_utils.py:33-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/fabric_allocator_utils.py#L33-L58)：`probe_allocator_backend` 是核心。它 dlopen 后用 `getattr` 取符号，设置参数为一个 `c_int`（设备 id），返回类型由调用方指定，然后 `probe_func(0)` 调用之。两类异常分别处理：找不到符号（`AttributeError`）说明这个 `.so` 不支持探测；其它异常（如 dlopen 失败）一并降级为"不支持"。返回值用泛型 `ProbeValue` 保留，使 CUDA 侧（返回 int 枚举）与 Ascend 侧（返回 bool）都能复用。

#### 4.2.4 代码实践

**实践目标**：用一段不依赖 PyTorch 的最小代码，亲手复现 `probe_allocator_backend` 的探测过程，理解 dlopen + 符号调用。

**操作步骤**（**示例代码**，可在有 `.so` 的环境直接跑）：

```python
# 示例代码：直接探测 nvlink_allocator.so 的 fabric 支持
import ctypes, os
so = os.path.join(os.path.dirname(__import__('mooncake').__file__),
                  "nvlink_allocator.so")
lib = ctypes.CDLL(so)
probe = lib.mc_allocator_probe
probe.argtypes = [ctypes.c_int]
probe.restype = ctypes.c_int
print("backend enum =", probe(0))   # 0=use_cudamalloc, 1=use_cumemcreate
```

**需要观察的现象**：返回 0、1、或抛 `AttributeError`（符号不存在）。

**预期结果**：在有 fabric 能力的 GPU 上应返回 `1`；在不支持的设备上返回 `0`；若 `.so` 未启用探测则抛 `AttributeError`。

**待本地验证**：返回值依赖真实 GPU 与 `.so` 构建。

#### 4.2.5 小练习与答案

**练习 1**：`probe_allocator_backend` 为什么要让调用方传入"失败默认值"而不是直接返回 `None`？
**答案**：因为 CUDA 侧的失败语义是"枚举值 `UNSUPPORTED = -2`"，Ascend 侧是 `False`。让调用方传入默认值，同一个通用函数就能适配两种类型系统，无需在工具层做类型判断。

---

### 4.3 NVLinkAllocator：探测 fabric 内存 + cudaMalloc/MemCreate 回退（Python 侧）

> 这是本讲的核心最小模块之一，也是规格指定的重点。

#### 4.3.1 概念说明

`NVLinkAllocator` 是面向 CUDA/NVLink 设备的 Python 包装类。它做两件事：

1. **探测（detect_mem_backend）**：用一次 `ctypes` 调用问 `.so`——"这台机器的 GPU 能不能用 fabric 内存？"，得到一个枚举值。
2. **构造 allocator（get_allocator）**：把 `.so` 包成 `CUDAPluggableAllocator`，按 device 缓存。

**关键认知**：探测结果只是一种"能力分类"，供宿主程序决策（要不要启用 fabric、要不要换 allocator）；它**不会**改变 `mc_allocator_malloc` 内部的实际分配逻辑。这点会在 4.4 展开。

`MemoryBackend` 枚举（[allocator.py:17-21](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L17-L21)）定义了四种状态：

| 枚举 | 值 | 含义 |
| --- | --- | --- |
| `USE_CUDAMALLOC` | 0 | 探测判定应走普通 cudaMalloc（设备不支持 fabric VMM） |
| `USE_CUMEMCREATE` | 1 | 探测判定可走 cuMemCreate 的 VMM/fabric 路径 |
| `UNKNOWN` | -1 | 探测返回了无法识别的值 |
| `UNSUPPORTED` | -2 | 探测本身失败（符号缺失/dlopen 失败） |

#### 4.3.2 核心流程

探测采用经典的**双重检查锁（double-checked locking）**，保证只探测一次且线程安全：

```text
detect_mem_backend()
  ├─ _probe_done? ──yes──► 直接返回 _supports_fabric
  └─ no ──► 加锁
            ├─ 再查 _probe_done?（防止等锁期间已被其它线程填好）
            ├─ _probe_fabric_memory_support(_get_so_path())
            │     └─ probe_allocator_backend(..., "mc_allocator_probe", c_int, UNSUPPORTED)
            │           └─ 调用 C 的 mc_allocator_probe(0)
            ├─ 异常 → UNSUPPORTED
            └─ _probe_done = True；返回 _supports_fabric
```

#### 4.3.3 源码精读

整个类见 [allocator.py:24-89](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L24-L89)。逐段看：

- **定位 `.so`**（[allocator.py:30-35](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L30-L35)）：`_get_so_path` 委托给 4.2 的 `get_mooncake_so_path`，目标是 `nvlink_allocator.so`。报错文案里点明这是 SGLang 的 `SGLANG_MOONCAKE_CUSTOM_MEM_POOL` 所需，且要求 `mooncake-transfer-engine >= 0.3.3.post2`——这是真实的环境前置条件。

- **翻译探测结果**（[allocator.py:37-59](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L37-L59)）：拿到 C 返回的 int 后，用 `MemoryBackend(supported_type)` 把它转成枚举。这里有个**容易被忽略的细节**——C++ 的 `MemoryBackendType` 枚举是 `{use_cudamalloc=0, use_cumemcreate=1, unknown=2}`，而 Python 的 `UNKNOWN=-1`。当 C 返回 2（unknown）时，Python 侧 `MemoryBackend(2)` 找不到对应成员会抛 `ValueError`，于是 `try/except` 把它归为 `UNKNOWN`。这就是 [allocator.py:45-49](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L45-L49) 那段 `try/except ValueError` 存在的原因。

- **双重检查锁探测**（[allocator.py:61-79](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L61-L79)）：外层无锁快查 `_probe_done`；进锁后再查一次；探测过程用 `try/except Exception` 兜底，任何意外都记日志并降级为 `UNSUPPORTED`，最后置 `_probe_done=True`。

- **构造并缓存 allocator**（[allocator.py:81-89](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L81-L89)）：`_instances` 是按 `torch_device` 字典缓存，避免重复构造；`get_allocator` **不看**探测结果——无论 fabric 是否支持，它都把同样的 `mc_allocator_malloc`/`mc_allocator_free` 接给 PyTorch。回退的真正决策点在 C++ 分配函数内部（4.4）。

> 旁注：[allocator.py:92-144](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L92-L144) 还有一个结构类似的 `BarexAllocator`（面向阿里 Barex，在系统路径 `/usr/lib[64]/libaccl_barex.so` 或包内查找），接入 `u2mm_alloc_wrapper_with_stream` / `u2mm_free_wrapper_with_stream`。它没有探测逻辑，读者可自行对照阅读。

#### 4.3.4 代码实践

**实践目标**：在不接入 PyTorch 的前提下，调用 `detect_mem_backend()` 查看当前设备的 fabric 能力分类，并解释返回值含义。

**操作步骤**：

1. 阅读上述源码，画出"`detect_mem_backend()` → `_probe_fabric_memory_support()` → `probe_allocator_backend()` → C 的 `mc_allocator_probe(0)`"这条调用链。
2. 在已安装 mooncake（含 `nvlink_allocator.so`）的环境运行：

```python
# 示例代码
from mooncake.allocator import NVLinkAllocator, MemoryBackend
b = NVLinkAllocator.detect_mem_backend()
print(b)            # 例如 <MemoryBackend.USE_CUMEMCREATE: 1>
print(int(b))       # 0 / 1 / -1 / -2
```

**需要观察的现象**：

- 返回 `USE_CUMEMCREATE(1)`：设备支持 fabric VMM，适合接入。
- 返回 `USE_CUDAMALLOC(0)`：设备不支持 fabric，宿主通常就**不替换** allocator，让 PyTorch 用默认的（基于 cudaMalloc 的）缓存分配器。
- 返回 `UNSUPPORTED(-2)`：探测失败，看日志确认是符号缺失还是 dlopen 失败。

**预期结果**：在有 fabric 能力的 GPU 上为 `1`；否则为 `0` 或 `-2`。

**待本地验证**：依赖真实 GPU 与 `.so`；无环境时按"调用链跟踪"完成本实践即可。

#### 4.3.5 小练习与答案

**练习 1**：`get_allocator()` 完全不参考 `detect_mem_backend()` 的结果，这是设计缺陷吗？
**答案**：不是缺陷，而是分层。探测（`detect_mem_backend`）是给**宿主程序**用的能力门控：宿主先问"支持吗？"，再决定要不要调用 `change_current_allocator`。`get_allocator` 只负责"把 `.so` 包成 PyTorch 可用对象"这一件事，二者职责分离。

**练习 2**：若 C 探测函数返回 `2`（C++ 的 `unknown`），Python 侧最终得到什么？走哪条代码路径？
**答案**：`MemoryBackend(2)` 抛 `ValueError`，被 [allocator.py:45-49](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator.py#L45-L49) 捕获，记 "Unknown Backend error" 日志并返回 `MemoryBackend.UNKNOWN(-1)`。

**练习 3**：双重检查锁里，进入 `with cls._lock` 之后为什么还要再判一次 `_probe_done`？
**答案**：防止等锁期间另一个线程已经完成探测并置位 `_probe_done`，避免重复探测与重复日志。

---

### 4.4 深入 C++：CUDA VMM fabric 分配与"回退"的真相

> 本节回答规格里的核心问题："`cudaMalloc` 与 `CUDA MemCreate` 间的回退"到底发生在哪里。

#### 4.4.1 概念说明

读 C++ 源码后会发现一个重要事实：**真正的分配函数 `mc_allocator_malloc` 始终走 VMM（`cuMemCreate`）路径，里面并没有调用 `cudaMalloc`。** 所谓"cudaMalloc vs MemCreate 的回退"，准确地说是两层：

1. **探测层**（`ProbeAllocatorBackend`）：判定设备属于哪一类。如果设备不支持 fabric 或 `cuMemCreate` 失败，就归类为 `use_cudamalloc`（值 0），把"这台机器该用普通 cudaMalloc"这个信息**告诉宿主**。宿主据此决定不替换 allocator。
2. **分配层**（`AllocateFabricMemory`）：恒为 VMM 路径。它内部唯一的"回退"是 `cuMemCreateTryFabric`——当请求 `CU_MEM_HANDLE_TYPE_FABRIC` 失败时，**清掉 fabric handle 位再试一次 `cuMemCreate`**，即"从可导出的 fabric 分配降级为普通 VMM 分配"，而不是降级到 `cudaMalloc`。

换句话说：`cudaMalloc` 那一支是**能力分类的结果**，不是运行时分配函数里的分支。

#### 4.4.2 核心流程

**探测**（[nvlink_allocator.cpp:24-53](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L24-L53)）：

```text
ProbeAllocatorBackend(device_id)
  ├─ cuDeviceGet(dev)
  ├─ 查 CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_FABRIC_SUPPORTED
  │     └─ 不支持 / 查询失败 ──► use_cudamalloc
  ├─ 用 4096 字节 + FABRIC handle 试 cuMemCreate
  │     ├─ 成功 → cuMemRelease ──► use_cumemcreate
  │     └─ 失败 ──► use_cudamalloc
```

**分配**（[nvlink_allocator.cpp:55-135](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L55-L135)）：

```text
AllocateFabricMemory(size, device, stream)
  ├─ cuDeviceGet → 查 fabric_supported → 决定是否置 FABRIC handle 位
  ├─ 查 GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED → 决定 gpuDirectRDMACapable
  ├─ cuMemGetAllocationGranularity → 得到最小粒度 g（2 的幂）
  ├─ size 向上对齐到 g 的倍数
  ├─ cuMemCreateTryFabric(handle, size, prop)     # 唯一的"回退"在此
  │     └─ 若 FABRIC 位被拒(NOT_PERMITTED/NOT_SUPPORTED) → 清 FABRIC 位再试 cuMemCreate
  ├─ cuMemAddressReserve → 预留虚拟地址
  ├─ cuMemMap → 物理句柄映射到虚拟地址
  └─ cuMemSetAccess → 让所有可见 device 都可读写
```

对齐那段用的是位掩码技巧：

```cpp
size = (size + granularity - 1) & ~(granularity - 1);
```

它把 `size` 向上取整到 `granularity` 的整数倍。因为 `granularity` 来自 `cuMemGetAllocationGranularity(..., CU_MEM_ALLOC_GRANULARITY_MINIMUM)` 且必为 2 的幂，等价于：

\[
\text{size}' = \left\lceil \frac{\text{size}}{g} \right\rceil \cdot g,\qquad g=2^k
\]

#### 4.4.3 源码精读

- **fabric handle 降级**（[nvlink_allocator.cpp:7-18](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L7-L18)）：`cuMemCreateTryFabric` 注释里注明参考了 NCCL 的 allocator 实现。当请求 `CU_MEM_HANDLE_TYPE_FABRIC` 却得到 `CUDA_ERROR_NOT_PERMITTED` 或 `CUDA_ERROR_NOT_SUPPORTED` 时，清掉 fabric 位再 `cuMemCreate` 一次——这就是"在 VMM 内部从 fabric 降级到非 fabric"的回退点。

- **探测分类**（[nvlink_allocator.cpp:24-53](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L24-L53)）：先问设备属性 `CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_FABRIC_SUPPORTED`，不支持就直接 `use_cudamalloc`；再用一小块（4096 字节）真刀真枪试一次 `cuMemCreate`（带 fabric handle），成功才认定 `use_cumemcreate`。注意 C++ 枚举 [nvlink_allocator.cpp:20](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L20) 的 `unknown=2`，正是 4.3 里 Python 侧 `ValueError` 的来源。

- **真正的分配**（[nvlink_allocator.cpp:55-135](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L55-L135)）：依次完成"算粒度→对齐→创建句柄→预留地址→映射→设访问权限"。其中 [nvlink_allocator.cpp:84-98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L84-L98) 还会查 GPUDirect RDMA 能力，若支持则置 `gpuDirectRDMACapable`——这是让传输引擎能直接 RDMA 这块内存的关键。[nvlink_allocator.cpp:118-126](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L118-L126) 用 `cudaGetDeviceCount` 拿到设备数，给每个设备都加上读写访问描述符后 `cuMemSetAccess`。

- **释放**（[nvlink_allocator.cpp:137-156](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L137-L156)）：先 `cuMemRetainAllocationHandle` 取回句柄，`cuMemGetAddressRange` 取回 size，再 `cuMemUnmap` → `cuMemAddressFree` → `cuMemRelease`，与分配严格对称。

- **导出符号**（[nvlink_allocator.cpp:166-181](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L166-L181)）：`extern "C"` 导出 `mc_allocator_probe`（返回 int 枚举）、`mc_allocator_malloc`、`mc_allocator_free`，以及同义的 `mc_nvlink_malloc`/`mc_nvlink_free` 别名。这三个名字正是 Python 侧 `CUDAPluggableAllocator(so, "mc_allocator_malloc", "mc_allocator_free")` 与 `probe_allocator_backend(..., "mc_allocator_probe", ...)` 引用的符号。

#### 4.4.4 代码实践

**实践目标**：通过修改一处"探测策略"来验证"探测结果不会影响实际分配路径"这一论断。

**操作步骤**（**源码阅读 + 思想实验**，不要真的改源码——本 worker 禁止修改源码）：

1. 阅读 `AllocateFabricMemory`（L55-135），确认其中**没有任何 `cudaMalloc` 调用**。
2. 假设把 `ProbeAllocatorBackend` 改成永远返回 `use_cudamalloc(0)`：Python 的 `detect_mem_backend()` 会返回 `USE_CUDAMALLOC`，但若宿主仍强行 `change_current_allocator`，分配仍走 `cuMemCreate`。
3. 反过来，即使探测返回 `use_cumemcreate(1)`，`cuMemCreateTryFabric` 仍可能在运行时因权限把 fabric 位降级。

**需要观察的现象**（思想实验结论）：探测值与实际分配路径**解耦**——探测是"能力声明"，分配是"尽力而为的 VMM"。

**预期结果**：能用自己的话讲清楚"为什么 `MemoryBackend` 枚举里有 `USE_CUDAMALLOC`，但 C++ 分配函数里却没有 `cudaMalloc`"。

**待本地验证**：本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`cuMemCreateTryFabric` 的回退，是从什么降级到什么？
**答案**：从"带 `CU_MEM_HANDLE_TYPE_FABRIC`、可被 fabric 导出的 VMM 分配"降级为"不带 fabric handle 的普通 VMM 分配"（仍是 `cuMemCreate`，不是 `cudaMalloc`）。

**练习 2**：`AllocateFabricMemory` 为什么要 `cuMemSetAccess` 给所有 device？
**答案**：VMM 映射出来的虚拟地址默认没有被任何设备访问授权；要让它能被其它 GPU 读写（跨设备 P2P / 多卡场景），必须显式为每个 device 添加 `CU_MEM_ACCESS_FLAGS_PROT_READWRITE` 的访问描述符。

---

### 4.5 Ascend NPU UBShmemAllocator：aclMallocPhysical fabric 路径

#### 4.5.1 概念说明

Ascend NPU 没有 CUDA，对应概念如下：

| CUDA VMM | Ascend aclrt |
| --- | --- |
| `cuMemCreate`（物理句柄） | `aclrtMallocPhysical` |
| `cuMemAddressReserve`（预留虚拟地址） | `aclrtReserveMemAddress` |
| `cuMemMap`（映射） | `aclrtMapMem` |
| `cuMemRetainAllocationHandle` | `aclrtMemRetainAllocationHandle` |
| `cuMemUnmap` / `cuMemAddressFree` / `cuMemRelease` | `aclrtUnmapMem` / `aclrtReleaseMemAddress` / `aclrtFreePhysical` |

`UBShmemAllocator` 是 `NVLinkAllocator` 的 Ascend 镜像，但探测结果用一个 `bool`（而不是枚举），因为 Ascend 侧只关心"fabic 物理内存能不能分配"。它对接的是 `torch_npu` 的 `NPUPluggableAllocator`。

#### 4.5.2 核心流程

```text
UBShmemAllocator.detect_mem_backend()
  └─ probe_allocator_backend(..., "mc_allocator_probe", c_int, 0)
        └─ C 的 mc_allocator_probe(device) → ProbeAllocatorBackend → bool

ProbeAllocatorBackend (C++)
  ├─ aclrtSetDevice(device)
  ├─ aclrtMallocPhysical(2MB, ACL_HBM_MEM_HUGE)
  │     ├─ 成功 → aclrtFreePhysical → true
  │     └─ 失败 → false

AllocateFabricMemory (C++)
  ├─ 对齐到 2MB
  ├─ aclrtMallocPhysical(handle, size, ACL_HBM_MEM_HUGE)
  ├─ aclrtReserveMemAddress(ptr, size, ...)
  └─ aclrtMapMem(ptr, size, handle)
```

#### 4.5.3 源码精读

Python 侧（[allocator_ascend_npu.py:14-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L14-L71)）与 NVLink 版结构几乎一致，差别在三处：

- [allocator_ascend_npu.py:20-25](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L20-L25)：目标是 `ubshmem_fabric_allocator.so`，报错文案要求 `USE_UBSHMEM` 启用。
- [allocator_ascend_npu.py:27-41](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L27-L41)：探测返回值直接 `bool(...)`，因为 C 侧 `mc_allocator_probe` 返回 `1`/`0`。
- [allocator_ascend_npu.py:63-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/allocator_ascend_npu.py#L63-L71)：构造的是 `NPUPluggableAllocator`，且 `get_allocator` 的函数符号仍是 `mc_allocator_malloc`/`mc_allocator_free`（只是这次来自 `ubshmem_fabric_allocator.so`，签名不带 stream）。

C++ 侧探测（[ubshmem_fabric_allocator.cpp:8-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp#L8-L30)）：`aclrtSetDevice` 后，用 2MB、`ACL_HBM_MEM_HUGE`（大页 HBM）试一次 `aclrtMallocPhysical`，成功即 `aclrtFreePhysical` 并返回 true。注意它**不查设备属性**，而是直接"试分配"——比 CUDA 版更直接。

C++ 侧分配（[ubshmem_fabric_allocator.cpp:32-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp#L32-L70)）：固定按 2MB 对齐（`alignment = 2 * 1024 * 1024`，与 huge 页匹配），然后 `aclrtMallocPhysical` → `aclrtReserveMemAddress` → `aclrtMapMem`，任一步失败都会回滚已分配的句柄/地址。

C++ 侧释放（[ubshmem_fabric_allocator.cpp:72-87](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp#L72-L87)）：`aclrtMemRetainAllocationHandle` 取回句柄后，依次 `aclrtUnmapMem` → `aclrtReleaseMemAddress` → `aclrtFreePhysical`。

导出符号（[ubshmem_fabric_allocator.cpp:97-109](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp#L97-L109)）：`mc_allocator_probe` 返回 `int`（`true?1:0`），`mc_allocator_malloc` / `mc_allocator_free` 签名**不带 stream**，匹配 `NPUPluggableAllocator`。

#### 4.5.4 代码实践

**实践目标**：对照 CUDA 版与 Ascend 版，找出两套实现的"同构"与"差异"。

**操作步骤**：

1. 并排打开 [nvlink_allocator.cpp:24-53](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp#L24-L53) 与 [ubshmem_fabric_allocator.cpp:8-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp#L8-L30)。
2. 填一张对照表：探测方式（查属性+试分配 vs 纯试分配）、对齐粒度来源（查 granularity vs 固定 2MB）、是否有 fabric handle 降级。

**需要观察的现象**：Ascend 版没有"fabric handle 降级"这一步（`ACL_MEM_HANDLE_TYPE_NONE`），也没有 `cuMemSetAccess` 的多设备授权环节。

**预期结果**：能说明为什么 Ascend 版比 CUDA 版更简洁（aclrt 的物理内存接口语义更"一站式"）。

**待本地验证**：源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：Ascend 探测为什么直接 `aclrtMallocPhysical` 试一下，而不像 CUDA 那样先查设备属性？
**答案**：Ascend aclrt 没有"是否支持 fabric 物理内存"这种细粒度设备属性可直接查询，最可靠的判定方式就是真分配一小块 huge 页 HBM（2MB）试一下，成功即支持、失败即不支持。

**练习 2**：Ascend 版 malloc/free 为什么没有 `stream` 参数？
**答案**：因为对接的是 `torch_npu.npu.memory.NPUPluggableAllocator`，其约定的 C 签名是 `void* malloc(ssize_t, int)` / `void free(void*, int)`，不含 stream；这与 CUDA 的 `CUDAPluggableAllocator` 约定不同。

---

### 4.6 构建与打包：`.so` 如何进入 Python 包

#### 4.6.1 概念说明

这些 allocator 不是默认就装好的——它们依赖特定硬件后端，因此构建期用 CMake 选项门控：CUDA/NVLink 用 `USE_MNNVL`，Ascend 用 `USE_UBSHMEM`。只有开启对应选项，`.so` 与 `.py` 才会被 `install` 进 Python 包目录。

#### 4.6.2 源码精读

- **门控安装**（[mooncake-integration/CMakeLists.txt:79-102](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt#L79-L102)）：定义 `ALLOCATOR_SO_PATH`（`nvlink_allocator.so`）与 `UBSHMEM_ALLOCATOR_SO_PATH`（`ubshmem_fabric_allocator.so`）；`USE_MNNVL` 时安装 nvlink `.so`，`USE_UBSHMEM` 时安装 ubshmem `.so`，目标目录是 `${PYTHON_SYS_PATH}/mooncake`。
- **Python 文件安装**（[mooncake-integration/CMakeLists.txt:125-143](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt#L125-L143)）：`allocator.py` 仅在 `USE_MNNVL` 装；`allocator_ascend_npu.py` 仅在 `USE_UBSHMEM` 装；`fabric_allocator_utils.py` 在二者任一启用时装。这正是 `from mooncake.allocator import ...` 之所以能成立的前提。
- **`.so` 怎么编译出来**：nvlink 由 [nvlink-allocator/build.sh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/build.sh) 编译，[build.sh:41-60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/build.sh#L41-L60) 支持 `--use-nvcc/hipcc/mcc/maca` 等多后端（CUDA / ROCm-HIP / Moore Threads MUSA / MACA）；ubshmem 由 [ubshmem-allocator/build.sh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/build.sh) 用 `g++` + Ascend `libascendcl` 编译。两者都经 [fabric_allocator.cmake](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/fabric_allocator.cmake) 的 `add_fabric_allocator_build_target` 包装成自定义构建目标。
- **厂商抽象**：两个 `.cpp` 都 `#include "cuda_alike.h"`，它根据 `USE_CUDA/USE_HIP/USE_MUSA/USE_UBSHMEM/...` 选择对应厂商头文件（[cuda_alike.h:1-31](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/cuda_alike.h#L1-L31)），从而让同一套 allocator 思路覆盖多种加速卡。

#### 4.6.3 代码实践

**实践目标**：确认本机安装的 mooncake 包里是否带有 allocator 相关文件。

**操作步骤**：

```bash
python -c "import mooncake, os; print(os.path.dirname(mooncake.__file__))"
ls <上面输出的目录> | grep -E 'allocator|nvlink|ubshmem|fabric_allocator_utils'
```

**需要观察的现象**：

- 若用 CUDA 且开了 `USE_MNNVL`：能看到 `allocator.py`、`fabric_allocator_utils.py`、`nvlink_allocator.so`。
- 若用 Ascend 且开了 `USE_UBSHMEM`：能看到 `allocator_ascend_npu.py`、`fabric_allocator_utils.py`、`ubshmem_fabric_allocator.so`。
- 若都没有：说明 wheel 是按非 fabric 配置打包的，需要换对应变体重新安装。

**预期结果**：能判断当前 wheel 是否具备 allocator 集成能力。

**待本地验证**：输出依赖实际安装的 wheel 变体。

## 5. 综合实践

**任务**：把"探测 → 决策 → 接入 → 验证"串成一条完整的零拷贝接入流程，并解释它与 [u5-l4](u5-l4-python-store-api.md) 零拷贝接口的衔接。

**步骤**（**示例代码**，需 CUDA + 带 `USE_MNNVL` 的 mooncake wheel）：

```python
# 示例代码：完整接入流程
import torch
from mooncake.allocator import NVLinkAllocator, MemoryBackend

# ① 探测：先问设备能力
backend = NVLinkAllocator.detect_mem_backend()
print("fabric backend:", backend)

if backend == MemoryBackend.USE_CUMEMCREATE:
    # ② 决策：支持 fabric，才替换 allocator（必须在首次分配前）
    alloc = NVLinkAllocator.get_allocator(torch.device("cuda:0"))
    torch.cuda.memory.change_current_allocator(alloc)
    print("已切换到 Mooncake VMM allocator")
else:
    print("不支持 fabric，保留 PyTorch 默认 allocator（cudaMalloc）")

# ③ 验证：建一个最小模型，其权重显存来自 mc_allocator_malloc
w = torch.randn(2048, 2048, device="cuda:0")
print("权重指针:", w.data_ptr())
```

**要求你回答**：

1. 第①步返回 `USE_CUMEMALLOC` 时，为什么第②步要跳过 `change_current_allocator`？（提示：能力门控——避免在不支持 fabric 的设备上把 PyTorch 切到一个会失败的 allocator。）
2. 第③步的 `w` 所在的显存，相比普通 `torch.randn` 有什么不同？它对 [u5-l4](u5-l4-python-store-api.md) 的 `put_tensor`/`register_buffer` 意味着什么？（提示：这块内存来自 VMM 且可能带 fabric/RDMA 能力，可被传输引擎直接注册，省去一次拷贝。）
3. 用 `ltrace -e mc_allocator_malloc python script.py` 观察是否真的命中了 `.so` 里的分配函数。

**待本地验证**：实际运行结果依赖 GPU 与 wheel 配置；无环境时请完成"回答三个问题"作为阅读型实践。

## 6. 本讲小结

- Mooncake 用 PyTorch 的**可插拔 Allocator** 把 tensor 底层显存换成自己 `.so` 里的 `mc_allocator_malloc`/`mc_allocator_free`；CUDA 侧用 `CUDAPluggableAllocator`（带 stream），Ascend 侧用 `NPUPluggableAllocator`（不带 stream）。
- `fabric_allocator_utils.py` 提供两件共用能力：在 `mooncake` 包里定位 `.so`，以及用 `ctypes` dlopen + 调用探测函数。
- `NVLinkAllocator` 用双重检查锁做**一次性探测**，把设备归类为 `USE_CUDAMALLOC`/`USE_CUMEMCREATE`/`UNKNOWN`/`UNSUPPORTED`；C++ 与 Python 枚举值错位（C++ `unknown=2`）正是 `try/except ValueError` 的由来。
- **关键认知**：探测只是能力门控；真正的 `mc_allocator_malloc` 始终走 CUDA VMM（`cuMemCreate`+`AddressReserve`+`Map`+`SetAccess`），其内部唯一的"回退"是 `cuMemCreateTryFabric` 把 fabric handle 降级为非 fabric 的 VMM 分配，而非退回 `cudaMalloc`。
- Ascend 的 `UBShmemAllocator` 是同构镜像：探测用"试 `aclrtMallocPhysical`"，分配走 `aclrtMallocPhysical` → `aclrtReserveMemAddress` → `aclrtMapMem`，按 2MB huge 页对齐。
- 构建期用 CMake 选项门控：`USE_MNNVL` 装 nvlink 三件套，`USE_UBSHMEM` 装 ubshmem 三件套，`cuda_alike.h` 提供跨厂商的头文件抽象。

## 7. 下一步学习建议

- **回到传输链路**：本讲让显存"诞生在 fabric/VMM 内存"里，下一步可读 [u3-l2 RDMA 传输](u3-l2-rdma-transport.md) 与 [u2-l4 多传输](u2-l4-multi-transport.md)，看这块内存如何被 NVLink/RDMA transport 直接注册并搬运。
- **Store 侧零拷贝**：结合 [u5-l4 Python Store API](u5-l4-python-store-api.md) 的 `put_tensor`/`register_buffer`，体会"同一块显存既被 PyTorch tensor 持有、又被 Store 注册"的零拷贝闭环。
- **Store 自身的分配器**：若想知道 Mooncake Store 内部如何管理 buffer 池，可读 [u6-l1 Store Allocator](u6-l1-store-allocator.md)，与本讲的"宿主侧 allocator"形成对照（一个是 PyTorch 进程内，一个是 Store 服务内）。
- **深入 C++ 细节**：直接阅读 [nvlink_allocator.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/nvlink-allocator/nvlink_allocator.cpp) 与 [ubshmem_fabric_allocator.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/ubshmem-allocator/ubshmem_fabric_allocator.cpp)，并对照 NCCL 的 `allocator.cc`（源码注释里有引用）理解 fabric handle 降级的工业界惯例。
