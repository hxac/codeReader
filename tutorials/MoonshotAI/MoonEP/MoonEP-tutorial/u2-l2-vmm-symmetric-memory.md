# u2-l2 CUDA VMM 与 NVLink 对称内存

## 1. 本讲目标

在 u1-l3 的代码地图里，我们说过 `moonep/buffer.py` 是 C++ 扩展 `moonep._C` 的唯一消费者；在 u2-l1 里，我们又看到 `_create_context` 预分配的 `hidden_buf` 与 `meta_buf` 是一切通信内核的「地基」。本讲就深入这块地基的实现：

1. 理解 CUDA VMM（Virtual Memory Management）的**分配-映射两步模型**：`cuMemCreate` 分配物理显存、`cuMemAddressReserve` + `cuMemMap` 搭建虚拟地址，两者可以解耦组合。
2. 理解**粒度对齐（granularity）**：为什么 `cuMemCreate`/`cuMemMap` 的大小必须是粒度的整数倍，以及 MoonEP 如何用 `pad_dim0_for_alignment` 把任意形状补齐。
3. 掌握 `nvl_dist_alloc` / `nvl_dist_map` 通过 pybind11 导出的 Python 接口，以及 `moonep/buffer.py` 如何编排它们。
4. 理解**对称内存（symmetric memory）**布局：每个 rank `cuMemCreate` 一段属于自己 GPU 的物理显存，再由所有 rank 把这 R 段物理内存按 rank 顺序映射成一块布局完全相同的连续虚拟地址——第 r 段物理上就驻留在 rank r 的 GPU 上。

## 2. 前置知识

### 2.1 虚拟地址（VA）与物理地址（PA）

现代 GPU 和 CPU 一样使用虚拟内存：内核代码里看到的指针是**虚拟地址**，硬件通过页表把它翻译成显存条上的**物理地址**。同一个物理页可以出现在多个虚拟地址上（多次映射），多个进程也可以把各自的虚拟地址指向同一块物理内存——这正是跨进程共享显存的基础。

### 2.2 `cudaMalloc` 与 CUDA Driver API 的区别

我们平时用的 `torch.empty(...)` 底层走 `cudaMalloc`：一步完成「要一块多大的显存」，返回一个可用指针，分配与映射绑定在一起。而 CUDA Driver API（`cu` 前缀的那族函数）提供了更细粒度的 VMM 接口，把这件事拆成两步：

- **分配物理内存**：`cuMemCreate` 返回一个不绑定任何地址的「物理内存句柄」；
- **映射到虚拟地址**：`cuMemAddressReserve` 先在虚拟地址空间里预留一段区间，`cuMemMap` 再把物理内存铺进去，`cuMemSetAccess` 授予某个设备对这段 VA 的读写权限。

两步分离带来一个关键能力：**一块物理内存可以被导出成句柄、传给别的进程、再映射进别的进程的虚拟地址空间**。`cudaMalloc` 做不到这一点（它只有配套的 IPC 专用 API），VMM 是更通用的机制。

### 2.3 句柄（handle）：fd 与 fabric handle

跨进程共享物理内存，需要先把 `cuMemCreate` 的结果「导出」成一个可传递的凭证，称为 shareable handle。MoonEP 支持两种（如何选择是 u2-l3 的主题，本讲只需知道有两种）：

| 句柄类型 | 载体 | 适用范围 |
| --- | --- | --- |
| POSIX 文件描述符（fd） | 一个 `int` | 同一节点内的进程之间 |
| fabric handle | 64 字节的不透明字节串 | 同一 NVLink fabric 域（可跨节点） |

### 2.4 NVLink 与对称内存

NVLink 是 GPU 之间的点对点高速互连。当一个 GPU 的内核访问映射了远端 GPU 物理内存的虚拟地址时，读写请求会由硬件透明地经 NVLink 送到远端显存——对内核代码完全无感，就像访问本地显存一样（只是延迟和带宽不同）。**对称内存**指的是一种约定：所有 rank 的虚拟地址布局完全一致，第 `i` 段永远对应 rank `i` 的物理内存，因此任何 rank 都能用同一套 `base + i * chunk_size + offset` 地址算术访问任何 rank 的数据。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的关键符号 |
| --- | --- | --- |
| [csrc/nvl_shared_buffer.cuh](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh) | VMM 头文件：全部 CUDA Driver API 的 C++ 封装 | `nvl_dist_alloc`、`nvl_dist_map`、`nvl_prepare`、`nvl_granularity_max`、`make_vmm_tensor`、`nvl_release_mem_handle` |
| [csrc/bindings.cu](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu) | pybind11 绑定层：把上述函数导出为 `moonep._C` 模块 | `PYBIND11_MODULE`、`get_vmm_granularity` |
| [moonep/buffer.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py) | Python 编排器：`_C` 的唯一消费者 | `pad_to_granularity`、`pad_dim0_for_alignment`、`create_nvl_dist_tensor`、`_map_nvl_dist_tensor` |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 上层调用点（本讲只看 `_create_context` 中的分配三行） | `NvS_padded`、`hidden_buf`、`meta_buf` |

多播（multicast）相关的函数也在这两个 C++ 文件里，但它们叠加在 meta 缓冲之上，是 u2-l4 的主题；本讲只在生命周期一节顺带交代它们对 `nvl_dist_alloc` 的影响。

## 4. 核心概念与源码讲解

### 4.1 分配-映射两步模型：nvl_dist_alloc

#### 4.1.1 概念说明

`nvl_dist_alloc` 是每个 rank 在自己 GPU 上执行的**本地**操作：分配一段物理显存，把它映射到本进程的虚拟地址，并导出一个可以发给其他进程的句柄。它回答的问题是「我捐出哪块物理内存参与全组共享」。

注意它**不涉及任何进程间通信**——句柄怎么送到别的 rank（all_gather？unix socket？）是 Python 侧 `buffer.py` 的事。C++ 只管「分配 + 导出」。

#### 4.1.2 核心流程

```text
nvl_dist_alloc(chunk_shape, dtype, use_fabric)
  │
  ├─ 1. nvl_prepare: 计算逻辑字节数 nbytes 与粒度对齐后的 allocated_size
  │
  ├─ 2. cuMemCreate(mem_handle, allocated_size, prop)
  │       分配一段未映射的物理显存，prop 指定「本设备 + PINNED + 可导出句柄类型」
  │
  ├─ 3. cuMemAddressReserve(&dptr, allocated_size)
  │       在本进程 VA 空间预留一段等长区间
  │
  ├─ 4. cuMemMap(dptr, allocated_size, 0, mem_handle, 0)
  │       把物理内存铺进预留的 VA
  │
  ├─ 5. cuMemSetAccess(dptr, allocated_size, {本设备, READWRITE})
  │       授予本设备读写权限（映射默认不可访问，必须显式授权）
  │
  ├─ 6. nvl_export_shareable(mem_handle, use_fabric)
  │       导出 fd（int64 标量张量）或 fabric handle（uint8[64] 张量）
  │
  └─ 7. make_vmm_tensor(...) 把 dptr 包装成 torch 张量（keepalive）
  │
  └─ 返回三元组 (keepalive, shareable, mem_handle)
```

第 2~5 步是 VMM 的标准「四连招」，读者应当把 **Reserve → Map → SetAccess** 这个顺序背下来：预留地址、铺内存、授权访问，缺一不可。

#### 4.1.3 源码精读

先看分配属性与四连招：

[csrc/nvl_shared_buffer.cuh:L235-L252](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L235-L252) 设置 `CUmemAllocationProp`（PINNED 设备内存 + 请求的句柄类型），然后依次执行 `cuMemCreate`（物理内存）、`cuMemAddressReserve`（预留 VA）、`cuMemMap`（映射）、`cuMemSetAccess`（授予本设备读写）。

再看导出与返回：

[csrc/nvl_shared_buffer.cuh:L254-L267](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L254-L267) 导出 shareable 句柄，把 VA 包装成 keepalive 张量，并返回三个值。注释里有一段关键的引用计数说明（L254-L258）：**这里故意不释放 owned `mem_handle`**——因为多播绑定（`cuMulticastBindMem`）还需要它；物理内存由 unicast 映射（keepalive）和（可选的）多播对象共同引用，只有 handle 被释放且所有映射解除后才真正回收。调用方负责在合适的时机调用 `nvl_release_mem_handle`。

[csrc/nvl_shared_buffer.cuh:L271-L273](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L271-L273) 就是这个释放函数，一行 `cuMemRelease`。

keepalive 张量的包装在 `make_vmm_tensor`：

[csrc/nvl_shared_buffer.cuh:L128-L134](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L128-L134) 自定义 deleter 先 `cudaStreamSynchronize` 确保没有在途内核还在使用这块内存，再按分配时的 `allocated_size` 执行 `cuMemUnmap` + `cuMemAddressFree`——正好是四连招的逆操作（Unmap → Free，没有对应 Create 的逆调用，那由 `cuMemRelease` 负责）。

[csrc/nvl_shared_buffer.cuh:L136-L149](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L136-L149) 用这个 deleter 构造 `c10::Storage` 和 `TensorImpl`，`set_sizes_contiguous(shape)` 设定逻辑形状。注意 **Storage 记录的是逻辑字节数 `nbytes`，而 deleter 释放的是对齐后的 `allocated_size`**——torch 只看见逻辑大小，padding 尾巴对上层完全透明。

#### 4.1.4 代码实践

**实践目标**：单进程直接调用 `nvl_dist_alloc`，亲眼看到三个返回值的形态，验证「分配一段 VA + 导出一个 fd」不依赖任何进程组。

**操作步骤**（需要已编译 `moonep._C` 的 GPU 环境）：

```python
# alloc_probe.py —— 示例代码：单进程探测 nvl_dist_alloc 的返回值
import torch
from moonep._C import nvl_dist_alloc, nvl_release_mem_handle

# chunk 形状任意：内部会向上取整到粒度
keepalive, shareable, owned_handle = nvl_dist_alloc(
    shape=[4096, 7168], dtype=torch.bfloat16, use_fabric=False)

print("keepalive:", keepalive.shape, keepalive.dtype, keepalive.device)
print("shareable:", shareable.shape, shareable.dtype,
      "fd =", int(shareable.item()))
print("owned_handle:", owned_handle)

keepalive.zero_()          # 正常的 CUDA 张量操作
del keepalive              # 触发 deleter：unmap + 释放 VA
nvl_release_mem_handle(owned_handle)   # 释放物理内存句柄
print("freed")
```

**需要观察的现象**：`keepalive` 是 `[4096, 7168]` 的 CUDA bf16 张量；`shareable` 是 0 维 int64 张量，值是一个真实打开的 fd 编号（正整数）；`owned_handle` 是一个 int64 数值。

**预期结果**：脚本正常打印并退出，无 CUDA 错误；`del keepalive` 后再访问指针会出错（内存已解除映射）。fd 编号每次运行可能不同。**待本地验证**（本讲义编写环境无 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：`nvl_dist_alloc` 的第 5 步 `cuMemSetAccess` 能省略吗？

**答案**：不能。VMM 映射建立后默认对所有设备都**不可访问**，必须显式 `cuMemSetAccess` 授权。省略它，内核一访问 `dptr` 就会得到非法地址错误。这也是 VMM 与 `cudaMalloc` 的一个重要差异：权限成为了一等公民。

**练习 2**：为什么 `make_vmm_tensor` 的 deleter 里要先 `cudaStreamSynchronize`？

**答案**：CUDA 是异步执行的，张量被 Python 释放时，可能还有已提交但未完成的内核在读写这块 VA。若直接 unmap，那些在途访问会命中已解除映射的地址导致崩溃。先同步当前流，保证流上所有工作完成后才解除映射。

**练习 3**：`nvl_dist_alloc` 为什么不自己释放 owned `mem_handle`？

**答案**：见 [csrc/nvl_shared_buffer.cuh:L219-L226](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L219-L226) 的注释：需要多播的缓冲（meta_buf）还要用这个 handle 执行 `cuMulticastBindMem`；不需要多播的缓冲则由 Python 侧立即释放。释放时机取决于调用方，所以 C++ 把决定权交出去。

### 4.2 粒度对齐：granularity 与 padding

#### 4.2.1 概念说明

VMM 的物理内存不是按任意字节分配的：`cuMemCreate` 的 size、`cuMemMap` 的地址与长度都必须是一个**粒度（granularity）**的整数倍——常见硬件上是 2 MiB。粒度可由 `cuMemGetAllocationGranularity` 查询，且**随句柄类型不同而不同**（fd 与 fabric 的粒度可能不一样）。

这带来两个工程问题，MoonEP 都要解决：

1. 用户想要的形状（如 `[NvS, H]` bf16）字节数几乎不可能是粒度的倍数 → 需要 padding；
2. padding 发生在「句柄类型确定」**之前**（调用顺序见 4.4.4），所以必须用两种句柄粒度的**最大值**做对齐基准，保证无论最终选哪种都有效。

#### 4.2.2 核心流程

对齐公式（向上取整到 g 的倍数）：

\[ \text{allocated\_size} = \left\lceil \frac{\text{nbytes}}{g} \right\rceil \times g \]

Python 侧 `pad_dim0_for_alignment` 只补第 0 维，保持其余维不动（这样 padded 张量仍是原布局的「行数变多」版本，切片语义不破坏）：

```text
pad_dim0_for_alignment(chunk_shape, dtype):
  inner = itemsize × ∏(shape[1:])          # 每行字节数
  nbytes = shape[0] × inner
  padded_bytes = ceil(nbytes / g) × g
  padded_dim0 = padded_bytes // inner      # 先粗除
  while (padded_dim0 × inner) % g != 0:    # 兜底：整除性可能被整除截断丢掉
      padded_dim0 += 1
  return padded_dim0
```

#### 4.2.3 源码精读

粒度查询取「两种句柄类型的最大值」：

[csrc/nvl_shared_buffer.cuh:L68-L80](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L68-L80) `nvl_granularity_for` 查询指定句柄类型的推荐粒度（`CU_MEM_ALLOC_GRANULARITY_RECOMMENDED`）。

[csrc/nvl_shared_buffer.cuh:L82-L90](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L82-L90) `nvl_granularity_max` 取 fd 粒度与（若支持）fabric 粒度的最大值——这是导出给 Python 的 `get_vmm_granularity` 背后的实现。

C++ 侧的对齐发生在 `nvl_prepare`：

[csrc/nvl_shared_buffer.cuh:L107-L117](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L107-L117) 逐维累乘算出 `size`，再按粒度向上取整得到 `allocated_size`，返回 `(size, allocated_size, device_id)` 三元组——`nvl_dist_alloc` 用它做 `cuMemCreate` 的参数。

Python 侧的镜像实现：

[moonep/buffer.py:L89-L92](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L89-L92) `pad_to_granularity`：字节级向上取整。

[moonep/buffer.py:L95-L110](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L95-L110) `pad_dim0_for_alignment`：按「每行字节数 inner」把字节级 padding 折算成第 0 维行数。末尾的 while 循环（L108-L110）是关键细节：`padded_bytes // inner_size` 向下取整可能丢掉余数，导致 `padded_dim0 * inner_size` 既小于必要字节数又不整除粒度，循环逐行补齐直到**精确整除**。

上层 `api.py` 的调用点：

[moonep/api.py:L305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L305) `NvS_padded = pad_dim0_for_alignment([NvS, H], torch.bfloat16)` —— u2-l1 提到的 `NvS_padded`（用户不可见的物理行数）就是在这里算出来的。meta 缓冲的对齐还叠加了多播粒度，那部分留给 u2-l4。

#### 4.2.4 代码实践

**实践目标**：写出 `pad_dim0_for_alignment` 的纯 Python 参考实现（不依赖 GPU），并验证数学性质。

**操作步骤**：

```python
# pad_ref.py —— 示例代码：pad_dim0_for_alignment 的纯逻辑复现
def pad_dim0_ref(dim0, inner_per_row, gran):
    nbytes = dim0 * inner_per_row
    padded = ((nbytes + gran - 1) // gran) * gran
    d0 = padded // inner_per_row
    while d0 * inner_per_row % gran != 0:
        d0 += 1
    return d0

gran = 2 * 1024 * 1024                    # 假设粒度为 2 MiB
cases = [
    (4096, 7168 * 2, "hidden_buf 风格 [NvS,H] bf16"),
    (1024, 4,         "int32 一维风格"),
    (1,   gran,       "恰好一行对齐"),
    (1,   gran + 2,   "inner 不整除粒度"),
]
for d0, inner, tag in cases:
    p = pad_dim0_ref(d0, inner, gran)
    assert p >= d0 and (p * inner) % gran == 0
    print(f"{tag}: dim0={d0}, inner={inner} -> padded={p}")
```

**需要观察的现象**：每个用例都满足两条断言——`padded >= dim0` 且 `padded * inner` 整除粒度；「恰好一行对齐」用例 padding 前后不变。

**预期结果**：四个用例全部通过。第四个用例能看到 while 循环确实做了额外补齐。此脚本不依赖 `moonep._C`，可直接运行；与真实 `get_vmm_granularity()` 的对拍在综合实践（第 5 节）完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_vmm_granularity` 要返回两种句柄粒度的最大值，而不是让 Python 先确定句柄类型再查对应粒度？

**答案**：因为 padding 必须发生在分配之前，而句柄类型由 `_use_fabric_for_group` 探测，探测需要组内通信，发生在 `nvl_dist_alloc` 调用时——也就是 padding 之后。用最大值对齐，无论后来选中 fd 还是 fabric，字节数都是两者粒度的公倍数，padding 都不会失效。

**练习 2**：`pad_dim0_for_alignment` 只补第 0 维。如果补在其他维（或整体 reshape）会有什么问题？

**答案**：补第 0 维等价于「多加几行」，张量前 `dim0` 行的内存布局与原形状完全一致，`buf[:dim0]` 即逻辑视图；若补在其他维，每一行的内容布局都会被破坏，上层切片语义（如 `hidden_buf[rank * NvS_padded : rank * NvS_padded + NvS]`）无法保持。

**练习 3**：`nvl_prepare` 里 `allocated_size` 是向上取整后的值，但 `make_vmm_tensor` 的 Storage 只登记 `nbytes`。这块 padding 尾巴里的内容会被谁读到吗？

**答案**：不会。torch 张量的形状只覆盖 `nbytes`，任何张量算子都访问不到 padding 区；它只是让 `cuMemCreate`/`cuMemMap` 满足粒度约束的「物理尾巴」。MoonEP 的内核也只按逻辑大小（如 `NvS` 行）索引。

### 4.3 pybind 绑定：从 C++ 函数到 moonep._C

#### 4.3.1 概念说明

u1-l2 讲过：`csrc/` 里的 C++ 代码由 `setup.py` 编译成 Python 扩展模块 `moonep._C`，`bindings.cu` 就是这层「胶水」——用 pybind11 声明每个 C++ 函数的 Python 签名（参数名、默认值、docstring）。理解这一层的意义在于：**Python 侧能调用什么、参数叫什么名字，完全由这张导出表决定**。

#### 4.3.2 核心流程

```text
C++ 函数 (nvl_shared_buffer.cuh)
        │  pybind11 m.def(...)，指定 arg 名与默认值
        ▼
moonep._C.* (bindings.cu 编译产物)
        │  moonep/buffer.py 顶部 import
        ▼
buffer.create_nvl_dist_tensor / create_nvl_dist_multicast_tensor
        │  api._create_context 调用
        ▼
ctx['hidden_buf'] / ctx['meta_buf'] / ctx['meta_mc']
```

#### 4.3.3 源码精读

[csrc/bindings.cu:L13-L28](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L28) 导出表的头部：`FABRIC_HANDLE_BYTES` 常量、`nvl_dist_alloc`（参数名 `shape`/`dtype`/`use_fabric`，默认 `False`）、`nvl_dist_map`、`nvl_fabric_supported`、`get_vmm_granularity`。docstring 明确写了两种句柄的适用范围（同节点 fd / 同 NVLink 域 fabric）。

[csrc/bindings.cu:L29-L52](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L29-L52) 其余导出：多播四件套（`create`/`import`/`add_device`/`bind_map`，u2-l4 详讲）与 `nvl_release_mem_handle`。注意 `nvl_multicast_bind_map` 的 docstring 点名它要消费 `nvl_dist_alloc` 返回的 owned handle——这是 4.1 中「不提前释放」的下游原因。

唯一带逻辑的绑定函数是粒度查询：

[csrc/bindings.cu:L7-L11](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L7-L11) `get_vmm_granularity` 取当前设备、返回 `nvl_granularity_max`（见 4.2.3），即 fd/fabric 粒度的最大值。

Python 侧的消费入口：

[moonep/buffer.py:L10-L23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23) 一次性 import 全部 12 个符号——这就是 u1-l3 说「buffer.py 是 `_C` 的唯一消费者」的具体形态。

[csrc/bindings.cu:L15-L19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L15-L19) 中 `nvl_dist_alloc` 的参数名是 `shape`，所以 Python 必须写 `nvl_dist_alloc(shape=..., dtype=..., use_fabric=...)`（buffer.py L233-L234 正是关键字调用）；`nvl_dist_map` 的参数名是 `chunk_shape`（buffer.py L187-L193 同样按名传递）。`at::ScalarType dtype` 由 pybind11 与 `torch.dtype` 自动互转，`std::vector<int64_t> shape` 接受 Python list。

#### 4.3.4 代码实践

**实践目标**：建立「导出表 ↔ Python 调用」的一一对应，学会用 `inspect` 查看绑定函数签名。

**操作步骤**（有编译产物时为可运行版本；否则退化为阅读对照）：

```python
# bindings_tour.py —— 示例代码
import moonep._C as C
import inspect
for name in ("nvl_dist_alloc", "nvl_dist_map", "get_vmm_granularity"):
    fn = getattr(C, name)
    print(f"{name}: doc={fn.__doc__!r}")
# 对照 csrc/bindings.cu 中每个 m.def 的 pybind11::arg 名单
```

无 `_C` 时，改为纯阅读实践：打开 [csrc/bindings.cu:L13-L53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L53) 与 [moonep/buffer.py:L10-L23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23)，手工填写一张「C++ 符号 → pybind 参数名 → buffer.py 使用处」三列表（本讲 3 节的地图就是范例）。

**需要观察的现象**：`__doc__` 输出与 bindings.cu 中的字符串一致；`inspect.signature` 对 pybind 函数可能拿不到参数名（pybind11 的 `__signature__` 支持有限），这本身就是个值得记录的坑。

**预期结果**：能对上 12 个导出符号与 buffer.py import 列表完全一致，无多余、无遗漏。**待本地验证**（运行版需要编译好的 `moonep._C`）。

#### 4.3.5 小练习与答案

**练习 1**：`nvl_dist_alloc` 在 C++ 里第一个参数叫 `chunk_shape` 还是 `shape`？`nvl_dist_map` 呢？

**答案**：`nvl_dist_alloc` 是 `shape`（[csrc/bindings.cu:L16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L16)），`nvl_dist_map` 是 `chunk_shape`（[csrc/bindings.cu:L21](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L21)）。两个名字不同是历史使然，用关键字参数调用时写错名字会直接 TypeError。

**练习 2**：为什么 `FABRIC_HANDLE_BYTES` 要作为模块常量导出，而不是在 Python 里硬编码 64？

**答案**：它来自 `sizeof(CUmemFabricHandle::data)`（[csrc/nvl_shared_buffer.cuh:L152-L153](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L152-L153)），由 CUDA 头文件定义。若 Python 硬编码，驱动更新导致长度变化时会出现静默截断。导出常量保证两侧永远一致——buffer.py 的 `_all_gather_shareables` 就用它构造 `[world_size, 64]` 张量。

**练习 3**：绑定层有一个函数包含真正的逻辑（而不是直接转发），是哪个？它做了什么？

**答案**：`get_vmm_granularity`（[csrc/bindings.cu:L7-L11](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L7-L11)）：先 `cudaGetDevice` 取当前设备，再调用 `nvl_granularity_max` 求两种句柄粒度的最大值。其余绑定都是一对一转发头文件函数。

### 4.4 对称拼接：nvl_dist_map 与全组连续虚拟地址

#### 4.4.1 概念说明

`nvl_dist_map` 是「对称」二字的核心。它把 **R 个 rank 各自导出的物理内存句柄**，按 rank 顺序映射到自己虚拟地址空间里一段连续区间上：

```text
本进程看到的张量（以 R=4 的 hidden_buf 为例，形状 [4×NvS_padded, H]）:

VA  ┌────────────────┬────────────────┬────────────────┬────────────────┐
    │  rank 0 的物理  │  rank 1 的物理  │  rank 2 的物理  │  rank 3 的物理  │
    │  显存 chunk     │  显存 chunk     │  显存 chunk     │  显存 chunk     │
    └────────────────┴────────────────┴────────────────┴────────────────┘
       在 GPU 0 上        在 GPU 1 上      在 GPU 2 上      在 GPU 3 上
```

因为每个 rank 都按同样的顺序（rank i 的 chunk 放在第 i 段）执行映射，**所有进程的 VA 布局完全一致**——这就是对称内存。任何一个 rank 的内核写 `buf[i * chunk + off]`：

- `i == 本 rank`：本地显存写，走正常显存带宽；
- `i != 本 rank`：硬件经 NVLink 远端写，落到 rank i 的物理显存上。

这也回答了本讲标题中的问题：**全组张量里第 r 段的物理内存，就是 rank r 当初 `cuMemCreate` 的那块**——dispatch 内核直写 `hidden_buf[dst_rank * NvS_padded + slot]` 时，token 的最终归宿物理上已经在接收方的接收缓冲里，接收端无需再搬运一次（零拷贝的物理基础）。`api.py` 里的本地视图 `hidden_buf_local = hidden_buf[rank * NvS_padded : rank * NvS_padded + NvS]`（[moonep/api.py:L432](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L432)）切出的正是纯本地访问段。

#### 4.4.2 核心流程

```text
nvl_dist_map(chunk_shape, dtype, shareables, local_rank, world_size, use_fabric)
  │
  ├─ 1. 校验句柄张量（形状/dtype/在 CPU 上）
  ├─ 2. nvl_prepare 重算 chunk 字节；断言 chunk_nbytes == chunk_allocated_size
  │       （chunk 必须已由 Python 精确 pad 到粒度倍数，不能有零头）
  ├─ 3. cuMemAddressReserve(total = chunk × world_size)
  ├─ 4. for i in range(world_size):
  │       import 句柄 i → cuMemMap 到第 i 段 → cuMemSetAccess(本设备 RW)
  │       → cuMemRelease(导入的句柄)     # 映射已持有引用，句柄即可释放
  └─ 5. 构造 [chunk_shape[0]×R, ...] 张量，deleter 逐段 unmap 后释放整段 VA
```

Python 编排（`create_nvl_dist_tensor`）在外面包了一圈：

```text
use_fabric = _use_fabric_for_group(group)          # 探测句柄类型
keepalive, shareable, owned_handle = nvl_dist_alloc(...)   # 4.1
_map_nvl_dist_tensor(...)                          # 分发句柄 + nvl_dist_map
finally: nvl_release_mem_handle(owned_handle)       # 无多播 → 立即释放
```

#### 4.4.3 源码精读

精确对齐断言：

[csrc/nvl_shared_buffer.cuh:L294-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L294-L303) 用 `nvl_prepare` 重算 chunk 字节并 `TORCH_CHECK(chunk_allocated_size == chunk_nbytes)`：如果 Python 传入的形状字节数不是粒度倍数，`nvl_dist_map` 拒绝工作。原因看 L302-L303：全组总大小按 `chunk_allocated_size * world_size` 拼接，而张量逻辑大小是 `chunk_nbytes * world_size`——两者不一致时段与段之间会出现「洞」，`full_shape` 与真实映射错位。所以**对齐责任完全压在 Python 侧的 `pad_dim0_for_alignment` 上**。

按 rank 映射的循环：

[csrc/nvl_shared_buffer.cuh:L308-L323](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L308-L323) 对每个 rank：`nvl_import_shareable` 导入句柄（fd 或 fabric blob），`chunk_va = dptr + i * chunk_allocated_size` 定位第 i 段，`cuMemMap` 铺入，`cuMemSetAccess` 授予**本设备**读写权限，最后立刻 `cuMemRelease` 导入句柄——映射本身持有物理内存引用，句柄用完即弃。L275-L283 的注释点名了与参考实现的差异：**所有段都给 RW**（不只是本 rank 的段），因为 dispatch 内核需要写远端 rank 的接收区。

析构路径：

[csrc/nvl_shared_buffer.cuh:L325-L339](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L325-L339) `full_shape[0] *= world_size` 得到全组张量形状；deleter 先同步流，再逐段 `cuMemUnmap`，最后 `cuMemAddressFree` 释放整段预留 VA——与构造严格互逆。

Python 编排与句柄分发：

[moonep/buffer.py:L217-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L217-L241) `create_nvl_dist_tensor` 的文档字符串写明契约：`chunk_shape` **必须**已经 pad 到粒度对齐（用 `pad_dim0_for_alignment`）。它先 alloc，再 map，`finally` 里立即释放 owned handle——普通（无多播）缓冲不需要保留它。

[moonep/buffer.py:L175-L214](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L175-L214) `_map_nvl_dist_tensor` 是句柄分发的分岔口：fabric 路径直接 `all_gather_into_tensor` 收集所有人的 64 字节句柄；fd 路径走 `_exchange_ipc_fds`（unix socket `SCM_RIGHTS`，u2-l3 详解）。两条路径最终都调 `nvl_dist_map`。注意 L213：`full_tensor._keepalive = keepalive`——把本 rank 自己的物理内存映射张量挂到全组张量上，防止其被垃圾回收（它是本 rank 物理内存存活的第一引用；全组张量存活 → keepalive 存活 → 物理内存存活）。

上层调用点：

[moonep/api.py:L361-L364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361-L364) `_create_context` 的分配三行：`hidden_buf` 用 `create_nvl_dist_tensor`（`[NvS_padded, H]` bf16），`meta_buf` 用 `create_nvl_dist_multicast_tensor`（多播版本，owned handle 保留到多播绑定完成后才释放——见 [moonep/buffer.py:L260-L274](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L260-L274)）。这就是 4.1 中「释放时机交给调用方」在 Python 侧的兑现。

#### 4.4.4 代码实践

**实践目标**：通过阅读把「对称布局」画出来，并理清 padding、句柄类型探测、分配三者的先后顺序。

**操作步骤**：

1. 画出 R=4 时**任意一个 rank** 进程内的 `hidden_buf` 布局图（0/1/2/3 段，标注每段的物理归属），再画第二个 rank 的图，对比验证两者**完全相同**——这就是对称性。
2. 在图上标出 `hidden_buf_local` 视图覆盖的区间（第 `rank` 段的前 `NvS` 行）。
3. 阅读以下三处，把调用顺序写成一行链：`pad_dim0_for_alignment`（[moonep/api.py:L305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L305)）→ `_use_fabric_for_group`（[moonep/buffer.py:L31-L57](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L31-L57)，在 `create_nvl_dist_tensor` 内第一步）→ `nvl_dist_alloc`。确认：**padding 时句柄类型尚未确定**，因此 4.2 练习 1 的「取最大粒度」是必须的。

**需要观察的现象**：图中每个 rank 的第 i 段都指向同一个 GPU i；两个 rank 的图逐字节同构。

**预期结果**：顺序链为 `pad（用 max 粒度）→ 探测 use_fabric → alloc（按选定句柄类型的粒度分配，此时 padding 已兼容）→ 分发句柄 → map`。此实践为纯阅读 + 画图，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：`nvl_dist_map` 循环里每个导入的句柄都被立刻 `cuMemRelease` 了，物理内存会不会因此被回收？

**答案**：不会。`cuMemMap` 建立的映射会持有物理内存的引用；`cuMemRelease` 只减少句柄一侧的引用。物理内存真正回收的条件是：所有者释放 owned handle（4.1）**且**所有映射被 `cuMemUnmap`。这组引用计数规则正是 VMM 两步模型的管理成本，也是 `buffer.py` 里 keepalive/`finally` 写法的依据。

**练习 2**：如果 `nvl_dist_map` 只给 `local_rank` 那一段 RW 权限、其余段只读（一些参考实现的做法），MoonEP 的哪个环节会坏？

**答案**：dispatch 内核会坏。它要把 token **写入**目的 rank 的接收段（`hidden_buf` 的远端行）；只读权限下这些写访问会触发非法访问。所以 [csrc/nvl_shared_buffer.cuh:L315-L320](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L315-L320) 给所有段都授予 RW——文件头注释（L277-L279）明确写了这一点。

**练习 3**：`create_nvl_dist_tensor` 为什么把 `nvl_release_mem_handle` 放在 `finally` 里，而不是 map 成功后顺序调用？

**答案**：`finally` 保证异常路径（如句柄分发失败、`nvl_dist_map` 抛 `TORCH_CHECK` 异常）也不会泄漏 owned handle。这是个一次性资源的标准防御写法；对照 `create_nvl_dist_multicast_tensor`（[moonep/buffer.py:L273-L274](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L273-L274)）同样用 `finally`，只是释放发生在多播绑定之后。

## 5. 综合实践

**任务**：编写单进程实验脚本 `vmm_granularity_lab.py`，调用 `moonep.buffer` 里的 `get_vmm_granularity` 与 `pad_dim0_for_alignment`，对多组 shape/dtype 系统性验证 VMM 粒度对齐约束；没有 GPU 时退化为「模拟粒度」模式验证同一套数学性质。

**操作步骤**：

```python
# vmm_granularity_lab.py —— 示例代码
import torch

# ---- 模式 A：有 GPU + 已编译 moonep._C ----
try:
    from moonep.buffer import get_vmm_granularity, pad_dim0_for_alignment
    gran = get_vmm_granularity()
    real = True
except Exception as e:                      # 无 _C / 无 CUDA
    real = False
    gran = 2 * 1024 * 1024
    from pad_ref import pad_dim0_ref        # 4.2.4 写的参考实现
    def pad_dim0_for_alignment(shape, dtype):
        inner = dtype.itemsize
        for d in shape[1:]:
            inner *= d
        return pad_dim0_ref(shape[0], inner, gran)

print(f"mode={'real' if real else 'simulated'}, granularity={gran} bytes")

# 覆盖 hidden_buf 与 meta_buf 两种真实用途 + 边界用例
cases = [
    ([8192, 7168], torch.bfloat16, "hidden_buf [NvS_padded,H] bf16"),
    ([6144, 7168], torch.bfloat16, "hidden_buf 另一配置"),
    ([3_000_000],  torch.int32,    "meta_chunk 一维 int32"),
    ([1],          torch.int32,    "最小张量（必然大量 padding）"),
]
ok = True
for shape, dtype, tag in cases:
    p0 = pad_dim0_for_alignment(shape, dtype)
    inner = dtype.itemsize
    for d in shape[1:]:
        inner *= d
    c1, c2 = p0 >= shape[0], (p0 * inner) % gran == 0
    ok &= c1 and c2
    print(f"{tag}: dim0={shape[0]} -> padded={p0}  "
          f"(>=dim0: {c1}, 整除粒度: {c2})")
print("ALL PASS" if ok else "FAILED")
```

**需要观察的现象**：

1. 真实模式下打印的粒度（常见为 2097152，即 2 MiB；具体值依驱动与设备而定）；
2. 每个用例两条断言均为 True，脚本输出 `ALL PASS`；
3. 「最小张量」用例被 padding 到整整一个粒度（`p0 * inner == gran`），直观感受粒度对齐的代价；
4. 反复运行，粒度值稳定不变（它由硬件/驱动决定，与运行次数无关）。

**预期结果**：真实模式下 `ALL PASS`（**待本地验证**，本讲义编写环境无 GPU）；模拟模式下若 `ALL PASS` 则证明 `pad_dim0_for_alignment` 的数学实现与 4.2 的公式一致，可在无 GPU 机器上先行验证逻辑。

**延伸**（可选）：在模拟模式中把 `gran` 改成非 2 的幂（如 3 MiB=3145728），观察哪些 `inner` 值会让 while 循环需要多次迭代，从而理解「粒度通常为 2 的幂」对收敛速度的意义。

## 6. 本讲小结

- CUDA VMM 把显存管理拆成**两步**：`cuMemCreate` 分配物理内存（返回可导出的句柄），`cuMemAddressReserve` + `cuMemMap` + `cuMemSetAccess` 搭建并授权虚拟地址映射；`nvl_dist_alloc` 是这四连招的完整封装，返回 (keepalive, shareable, owned_handle) 三元组。
- 所有 VMM 调用的大小必须对齐**粒度**（`cuMemGetAllocationGranularity` 查询，fd/fabric 两类句柄粒度可能不同）；Python 侧 `pad_dim0_for_alignment` 只补第 0 维，把字节数精确补到 `max(fd, fabric)` 粒度的倍数，padding 责任完全在调用方（`nvl_dist_map` 会断言拒绝未对齐的形状）。
- `bindings.cu` 用 pybind11 把 12 个符号导出为 `moonep._C`，`buffer.py` 是唯一消费者；参数名（`shape` vs `chunk_shape`）由导出表决定，必须按名传参。
- `nvl_dist_map` 把 R 个 rank 的物理内存句柄按 rank 顺序映射成一块**布局完全一致的连续 VA**——对称内存：第 r 段物理上就是 rank r 的显存，所有段对本地设备都是 RW（dispatch 要直写远端），VA 布局相同使任何 rank 能用同一套 `base + r*chunk + off` 算术访问全组数据。
- 生命周期靠引用计数管理：导入句柄在 `cuMemMap` 后即可释放；owned handle 因多播绑定需要而延迟释放（普通缓冲 `finally` 立即释放）；物理内存在 handle 释放且全部 unmap 后才真正回收，keepalive 张量挂载在全组张量上防止过早回收。

## 7. 下一步学习建议

本讲刻意跳过了两个问题：**句柄是怎么从一个进程送到另一个进程的**（`_all_gather_shareables` 与 `_exchange_ipc_fds` 的 SCM_RIGHTS 细节、`MOONEP_MEM_HANDLE_TYPE` 的探测与回退），以及 **meta_buf 之上的多播视图**（`nvl_multicast_*` 四件套如何让一次写广播到所有 rank）。它们分别就是下一讲 **u2-l3「跨进程共享：fabric 句柄与 POSIX fd」** 和 **u2-l4「组播视图与 meta_buf 共享布局」** 的主题；学完 u2-l4 再回看本讲的 `create_nvl_dist_multicast_tensor`，owned handle 的整个生命周期就完全闭环了。
