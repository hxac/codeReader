# 内存分配器：nvcv alloc 与自定义 Allocator

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 nvcv 分配器抽象的四层结构：C ABI 函数指针结构体 → 公开 C++ 包装类 → priv 接口（NVI）→ 默认/自定义两个实现。
2. 解释 `DefaultAllocator` 如何用 `cudaMalloc`/`cudaHostAlloc`/`operator new` 服务 Tensor、ImageBatchVarShape、TensorBatch、Array 的内存分配，以及"全局唯一默认分配器"从哪里来。
3. 写出一个统计分配次数与字节数的自定义 C++ Allocator，注入 `nvcv::Tensor` 构造函数并验证它确实被调用。
4. 解释 `Requirements`（需求协商）在"算清楚要多少内存"与"实际分配"之间扮演的角色。
5. 厘清 Python 对象缓存（u4-l2）与这层 C++ allocator 的关系：谁负责"不分配"，谁负责"怎么分配"。

## 2. 前置知识

- **分配器（Allocator）**：一个"给我 size 字节、按 align 对齐"的抽象工厂。把分配动作抽象出来，上层容器（Tensor 等）就不关心内存来自 `cudaMalloc` 还是来自内存池。
- **三种内存**：CUDA 世界里常打交道的三类内存——host 内存（CPU 普通 malloc）、CUDA 显存（`cudaMalloc`，CPU 不能直接解引用）、host pinned 内存（`cudaHostAlloc`，页锁定、CPU 和 GPU 都可访问，DMA 传输更快）。
- **对齐（alignment）**：地址必须是某数的整数倍（且为 2 的幂）。GPU 纹理访问、向量加载都依赖对齐；u2-l1 已讲过 Tensor 行距默认对齐到设备的纹理对齐属性（通常 512 字节）。
- **NVI（Non-Virtual Interface）惯用法**：公开函数是非虚的，做参数校验后调用私有的纯虚 `doXxx`。本讲 `IAllocator` 就是这个模式。
- **句柄与 CoreResource**：u6-l1 讲过 `NVCVAllocatorHandle` 是不透明指针，公开 C++ 类 `Allocator` 是引用计数句柄的 RAII 薄包装；`Allocator{nullptr}` 表示"空句柄"，最终会被解释为默认分配器。
- **前置讲义**：u6-l1（C/C++ 双 API 与句柄生命周期）、u2-l1（Tensor 的 stride 与对齐）、u4-l2（Python 对象缓存）。

## 3. 本讲源码地图

> 说明：规划中的 `src/nvcv/src/priv/Allocator.cpp` 在当前仓库中**不存在**。分配器的实现实际分布在下面 4 个真实文件中（外加公开头文件），本讲按真实路径讲解。

| 文件 | 作用 |
|------|------|
| [src/nvcv/src/include/nvcv/alloc/Allocator.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.h) | 纯 C 契约：`NVCVResourceType`、`NVCVMemAllocFunc`/`NVCVMemFreeFunc` 函数指针、`NVCVResourceAllocator` 结构体、`nvcvAllocatorConstructCustom` 等 C API |
| [src/nvcv/src/include/nvcv/alloc/Allocator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp) | 公开 C++ 包装：`ResourceAllocator`/`MemAllocator`/`Allocator`、自定义分配器助手 `CustomMemAllocator`/`CustomAllocator` |
| [src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp) | 上述包装的内联实现，含"lambda 如何被编组成 C 函数指针 + ctx"的核心魔法 |
| [src/nvcv/src/include/nvcv/alloc/Requirements.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Requirements.hpp) | `Requirements` 需求协商类：`addBuffer`/`numBlocks`/`CalcTotalSizeBytes` |
| [src/nvcv/src/priv/IAllocator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.hpp) / [IAllocator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.cpp) | priv 层 NVI 接口 + 参数校验 + `GetDefaultAllocator()`/`GetAllocator()` 解析 |
| [src/nvcv/src/priv/DefaultAllocator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.hpp) / [DefaultAllocator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.cpp) | 默认实现：三对 alloc/free + `doGet` 生成 C 描述符 |
| [src/nvcv/src/priv/CustomAllocator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/CustomAllocator.hpp) / [CustomAllocator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/CustomAllocator.cpp) | 自定义实现：按资源类型存函数指针表，未定制类型**回填默认** |
| [src/nvcv/src/Allocator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Allocator.cpp) | 分配器 C API 落点：`nvcvAllocatorConstructCustom` 等 |
| [src/nvcv/src/priv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp) | Tensor 消费分配器的现场：需求计算 → `allocCudaMem` |
| [tests/nvcv_types/system/TestAllocatorCpp.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/system/TestAllocatorCpp.cpp) | 官方自定义分配器测试，本讲实践的依据 |

## 4. 核心概念与源码讲解

本讲的最小模块：

1. 分配器的公共契约：`NVCVResourceAllocator` 与三种内存
2. 默认分配器 `DefaultAllocator`：cudaMalloc 背后的真身
3. Tensor 如何通过分配器拿显存：Requirements 协商
4. 自定义分配器：`CustomMemAllocator` 与缺省回填

### 4.1 分配器的公共契约：NVCVResourceAllocator 与三种内存

#### 4.1.1 概念说明

nvcv 中一切需要内存的对象（Tensor、Image、ImageBatchVarShape、TensorBatch、Array）都不直接调用 `cudaMalloc`，而是通过"分配器"间接获取。为了让 C 和 C++ 都能用、且能跨过 C ABI 边界（u6-l2 讲过异常不能穿越 `extern "C"`），最底层的契约是一个纯 C 结构体：**一对函数指针 + 一个不透明的上下文指针 + 一个清理函数指针**。

分配器按"资源类型"分频道，目前共三种：

| 资源类型 | 含义 | 默认 malloc / free |
|---|---|---|
| `NVCV_RESOURCE_MEM_HOST` | CPU 可访问 | `operator new` / `operator delete`（文档表格写 malloc/free，实现见 4.2） |
| `NVCV_RESOURCE_MEM_CUDA` | GPU 显存 | `cudaMalloc` / `cudaFree` |
| `NVCV_RESOURCE_MEM_HOST_PINNED` | CPU+GPU 都可访问的页锁定内存 | `cudaHostAlloc` / `cudaFreeHost` |

#### 4.1.2 核心流程

一次"通过 C 契约分配显存"的流程：

```text
调用方（如 priv::Tensor）
   │  allocCudaMem(bufSize, alignBytes)
   ▼
priv::IAllocator::allocCudaMem        ← 非虚公开函数：校验 size/align
   │  doAllocCudaMem(size, align)     ← 私有纯虚
   ▼
DefaultAllocator::doAllocCudaMem      ← cudaMalloc + 对齐复查
   或 CustomAllocator::doAllocCudaMem ← 取出用户函数指针转发
```

C 契约层面等价于：`fnAlloc(ctx, size, align)` 返回 `NVCVMemoryBuffer`（即 `void*`），`fnFree(ctx, ptr, size, align)` 释放；`ctx` 原样透传，这就是"把 this 或用户状态塞进 C 回调"的通道。

#### 4.1.3 源码精读

三种资源类型与函数指针签名定义在 C 头文件中：

- [src/nvcv/src/include/nvcv/alloc/Allocator.h:L77-L96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.h#L77-L96) — 定义 `NVCVMemAllocFunc`（返回 `NVCVMemoryBuffer`）与 `NVCVMemFreeFunc`（约定 `ptr` 为 NULL 时必须无操作成功），以及三种 `NVCVResourceType` 枚举，`NVCV_NUM_RESOURCE_TYPES == 3`。
- [src/nvcv/src/include/nvcv/alloc/Allocator.h:L123-L137](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.h#L123-L137) — `NVCVResourceAllocatorRec` 结构体本体：`ctx`（透传给回调的用户上下文）、`resType`、`res`（函数指针联合体）、`cleanup`（销毁描述符时回收 ctx 的钩子）。
- [src/nvcv/src/include/nvcv/alloc/Allocator.h:L36-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.h#L36-L40) — 文档中"默认分配函数"的权威表格。

公开 C++ 层在 C 结构体之上做了类型化包装：

- [src/nvcv/src/include/nvcv/alloc/Allocator.hpp:L140-L172](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp#L140-L172) — `MemAllocator` 把 `res.mem.fnAlloc`/`fnFree` 包成 `alloc(size, align)` / `free(ptr, size, align)`，并用 `IsCompatibleKind` 限定它只接受三种内存资源类型之一。
- [src/nvcv/src/include/nvcv/alloc/Allocator.hpp:L198-L218](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp#L198-L218) — 三个带类型的别名 `HostMemAllocator` / `HostPinnedMemAllocator` / `CudaMemAllocator`，各自只认自己的 `kResourceType`，拿错了类型构造时会抛异常（见 AllocatorImpl.hpp L214-221 的校验）。
- [src/nvcv/src/include/nvcv/alloc/Allocator.hpp:L229-L272](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp#L229-L272) — `Allocator` 本体：继承 `CoreResource<NVCVAllocatorHandle, Allocator>`（引用计数句柄 + RAII，析构 `reset()`），并提供 `hostMem()`/`hostPinnedMem()`/`cudaMem()`/`get(resType)` 取出对应频道的类型化分配器。

priv 层的接口采用 NVI 惯用法，公开函数集中做校验：

- [src/nvcv/src/priv/IAllocator.hpp:L29-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.hpp#L29-L55) — `IAllocator` 声明三对公开非虚方法 + 对应私有纯虚 `doAlloc*/doFree*` + `doGet`；它同时经 `ICoreObjectHandle` 挂上句柄管理体系。
- [src/nvcv/src/priv/IAllocator.cpp:L88-L109](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.cpp#L88-L109) — `allocCudaMem` 的三道校验：size ≥ 0、align 是 2 的幂、size 必须是 align 的整数倍。这就是自定义分配器"拿到手即合法"的保证。
- [src/nvcv/src/priv/IAllocator.hpp:L57-L80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.hpp#L57-L80) — 贴心工具 `AllocHostObj`/`FreeHostObj`：用分配器给的内存做 placement new，构造 C++ 对象失败时自动归还内存。priv 内部的宿主侧对象也走分配器。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认"C 契约 → C++ 包装 → priv 接口"三层是同一份信息的三种形态。
2. **操作步骤**：打开上述三组链接，对照抄下 `NVCVMemAllocFunc` 的参数列表、`MemAllocator::alloc` 的转发语句、`IAllocator::allocCudaMem` 的校验条件。
3. **需要观察的现象**：三层的参数名几乎逐字相同（`size`/`align`、`ptr`）；校验只发生在 priv 公开层，C 函数指针层假定输入已合法（Allocator.h L67-76 的 doxygen 注释写明"保证 ≥0 且为整数倍"）。
4. **预期结果**：能口头复述"校验在 NVI 公开层、执行在 do 层、C 描述符只是运输格式"。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NVCVResourceAllocator` 里需要一个 `ctx` 指针？
**答案**：C 函数指针无法携带状态。调用方把任意用户上下文（典型如 C++ 对象的 `this`，见 4.2.3 的 `DefaultAllocator::doGet`，或自定义分配器捕获的 lambda 环境，见 4.4.3）塞进 `ctx`，nvcv 在回调时原样回传，从而让无状态的 C 指针调用有状态的 C++ 逻辑。

**练习 2**：`Allocator` 公开类与 `CudaMemAllocator` 是什么关系？
**答案**：`Allocator` 是"整台工厂"的引用计数句柄（跨三种资源频道）；`CudaMemAllocator` 是从它身上 `get<NVCV_RESOURCE_MEM_CUDA>()` 拆出来的"单频道视图"，只暴露 `alloc`/`free` 两个动作，且构造时校验描述符的 `resType` 匹配。

**练习 3**：如果自定义分配器收到的 `align=0` 会怎样？
**答案**：到不了用户回调。`IAllocator::allocCudaMem` 先检查 `IsPowerOfTwo(align)`，0 不是 2 的幂，直接抛 `NVCV_ERROR_INVALID_ARGUMENT` 异常（[IAllocator.cpp:L96-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.cpp#L96-L99)）。

### 4.2 默认分配器 DefaultAllocator：cudaMalloc 背后的真身

#### 4.2.1 概念说明

`DefaultAllocator` 是不传分配器时 everyone 默认用到的那份实现——Python 里 `cvcuda.Tensor(...)`、C++ 里 `nvcv::Tensor(shape, dtype)` 最终都落到它。它回答一个问题："不定制时，内存从哪来？" 答案就是三个直白的后端：对齐 `operator new`、`cudaHostAlloc`（WriteCombined+Mapped）、`cudaMalloc`。

它还有一个常被忽略的职责：`doGet`——把自己的成员函数包装成 C 描述符，供"部分定制"场景回填（见 4.4）。

#### 4.2.2 核心流程

- host：`::operator new(size, align_val_t)` / `::operator delete`（带对齐版本的 C++ 全局 new/delete）。
- pinned：`cudaHostAlloc(ptr, size, cudaHostAllocWriteCombined | cudaHostAllocMapped)`，然后**手动复查**对齐，不满足则释放并抛内部错误。
- cuda：`cudaMalloc(&ptr, size)`，同样复查对齐。
- 进程内唯一实例：`GlobalContext()` 持有一个 `m_allocDefault` 成员，`GetDefaultAllocator()` 返回它；任何 `nullptr` 分配器句柄都会解析到它。

对齐复查的原因：`cudaMalloc` 只保证 256 字节对齐，而 Tensor 可能要求更粗的对齐（如 512 字节纹理对齐，u2-l1）。默认实现无法满足时选择**快速失败**而不是静默降级——代码里的 `REVISIT: can we do better than this?` 注释表明这是已知取舍。对齐需求的合成公式为：

\[ \text{alignBytes} = \operatorname{roundUpPow2}\left(\operatorname{lcm}(\text{纹理对齐属性}, \text{rowAlign})\right) \]

#### 4.2.3 源码精读

- [src/nvcv/src/priv/DefaultAllocator.hpp:L25-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.hpp#L25-L38) — 类声明：`final`，继承 `CoreObjectBase<IAllocator>`，只重写 7 个 `do*` 虚函数，无成员变量——它是纯策略对象。
- [src/nvcv/src/priv/DefaultAllocator.cpp:L63-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.cpp#L63-L84) — CUDA 频道：`cudaMalloc` 后若 `ptr % align != 0` 则 `cudaFree` 并抛 `NVCV_ERROR_INTERNAL`。free 侧忽略 size/align 直接 `cudaFree`。
- [src/nvcv/src/priv/DefaultAllocator.cpp:L41-L61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.cpp#L41-L61) — pinned 频道：`cudaHostAlloc` 带 `cudaHostAllocWriteCombined | cudaHostAllocMapped` 标志，同样做对齐复查；释放用 `cudaFreeHost`。
- [src/nvcv/src/priv/DefaultAllocator.cpp:L86-L145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.cpp#L86-L145) — `doGet(resType)`：把 `this` 塞进 `custAllocator.ctx`，用 `static` lambda 转发回 `self->allocHostMem(...)` 等成员，按三种资源类型分别装配出一对函数指针。这正是"this 穿越C 回调"的标准姿势。
- [src/nvcv/src/priv/Context.hpp:L39-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Context.hpp#L39-L44) — 全局上下文按依赖顺序持有 `m_allocDefault`（`DefaultAllocator` 直接作为成员对象，进程生命周期内常驻）。
- [src/nvcv/src/priv/IAllocator.cpp:L116-L131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.cpp#L116-L131) — 两个解析函数：`GetDefaultAllocator()` 返回 `GlobalContext().allocDefault()`；`GetAllocator(handle)` 在 `handle == nullptr` 时返回默认分配器，否则把句柄还原为 `IAllocator&`。**"传 NULL = 用默认"这条规则就定义在这里。**

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证"Python 创建张量 → 默认分配器 → cudaMalloc"这条链。
2. **操作步骤**：
   - 阅读上述 `GetAllocator` 与 `Context.hpp` 链接；
   - 回顾 u4-l2 的结论：Python 侧非包装张量只在缓存未命中时才真正创建；
   - 在 [src/nvcv/src/priv/DefaultAllocator.cpp:L66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DefaultAllocator.cpp#L66) 处想象加一行 `fprintf(stderr, "cudaMalloc %ld\n", size);`（只读练习，不要真改仓库源码；如需实操请在自己的 fork 中进行）。
3. **需要观察的现象**：Python 中循环 `cvcuda.Tensor((1,3,4,4), u8)` 同形状多次创建，理论上只应触发极少数次分配（缓存命中则零次）。
4. **预期结果**：能解释"为什么加日志后看到的 cudaMalloc 次数远少于 Tensor 构造次数"——因为 Python 对象缓存（u4-l2）挡在了 allocator 之前。实际日志输出**待本地验证**（需 GPU 环境自行编译）。

#### 4.2.5 小练习与答案

**练习 1**：默认分配器为什么在 `cudaMalloc` 之后还要检查一次对齐？
**答案**：`cudaMalloc` 只保证其自身的对齐承诺（典型 256 字节），而 Tensor 的 `alignBytes` 可能是 lcm(纹理对齐, rowAlign) 向上取整到 2 的幂（如 512）。不满足时继续用会导致 kernel 寻址与纹理访问出错，所以立刻释放并抛 `NVCV_ERROR_INTERNAL`，把问题暴露在分配点而非渲染点。

**练习 2**：`DefaultAllocator` 有几个实例？
**答案**：作为进程级全局上下文的成员，正常只有一个（`GlobalContext().allocDefault()`）。注意它本身不做任何缓存或池化——每次 `allocCudaMem` 都是一次真实的 `cudaMalloc`；池化/复用要么靠上层（Python 对象缓存、WorkspaceCache），要么靠你自定义分配器。

**练习 3**：pinned 内存默认带 `cudaHostAllocWriteCombined | cudaHostAllocMapped` 两个标志，分别意味着什么？
**答案**：WriteCombined 表示写合并（CPU 写快、读慢，适合"CPU 只写、GPU 只读"的单向传输缓冲）；Mapped 表示该内存同时映射进 GPU 地址空间，设备端可直接访问。

### 4.3 Tensor 如何通过分配器拿显存：Requirements 协商

#### 4.3.1 概念说明

容器与分配器之间需要一个"中间语言"：容器先声明**我需要什么**（大小+对齐的清单，即 `Requirements`），分配器再据此**给货**。把"算需求"与"实际分配"拆开有两个好处：

1. 同一份需求可以被复用：算一次，创建多次（或先窥探总量再统一规划）；
2. `NVCVMemRequirements` 不是单个总字节数，而是**按 log₂ 块大小分桶的直方图**（`numBlocks[log2BlockSizeBytes]`），这让未来的块分配器可以按"几个 1KB 块 + 几个 4KB 块"的方式做池化预分配，而不必每次都凑一个整块。

`Requirements::Memory::addBuffer(bufSize, bufAlign)` 是"往清单里加一条"；`CalcTotalSizeBytes` 是"把清单压成一个总字节数"（当前默认分配器路径只用到这个总量）。

#### 4.3.2 核心流程

`nvcv::Tensor(shape, dtype, bufAlign, alloc)` 的内存路径：

```text
Tensor::CalcRequirements(shape, dtype, bufAlign)
  ├─ 计算 cstride（行距按 rowAlign 对齐到 2 的幂）
  ├─ alignBytes = roundUpPow2(lcm(纹理对齐属性 或 用户 baseAlign, rowAlign))
  ├─ 逐维推 strides（最内维 = dtype.strideBytes()）
  └─ AddBuffer(reqs.mem.cudaMem, strides[0]*shape[0], alignBytes)   ← 需求入清单
Tensor(reqs, alloc)
  └─ priv::GetAllocator(alloc.handle 或 nullptr) → IAllocator&
       └─ priv::Tensor(reqs, alloc)
            ├─ bufSize = CalcTotalSizeBytes(reqs.mem.cudaMem)
            ├─ buffer  = alloc.allocCudaMem(bufSize, reqs.alignBytes) ← 唯一一次分配
            └~Tensor()：alloc.freeCudaMem(buffer, bufSize, alignBytes) ← 析构归还
```

关键点：**一个 Tensor 只有一次 `allocCudaMem`**——stride 已在需求阶段把"行对齐"折算进各维步长，总缓冲 = 最外维 stride × shape。多平面/多缓冲的容器（如 ImageBatchVarShape）则调用多次 `AddBuffer`/多次 alloc。

#### 4.3.3 源码精读

- [src/nvcv/src/include/nvcv/Tensor.hpp:L170-L174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L170-L174) — 三个公开构造函数都以 `const Allocator &alloc = Allocator{nullptr}` 结尾：**分配器是每个 Tensor 可独立指定的**，缺省空句柄即默认分配器。
- [src/nvcv/src/Tensor.cpp:L86-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Tensor.cpp#L86-L107) — C API `nvcvTensorConstruct(reqs, halloc, handle)`：`priv::GetAllocator(halloc)` 一行完成"NULL→默认"的解析，然后创建 priv Tensor。C 用户同样可以传入自定义分配器句柄。
- [src/nvcv/src/priv/Tensor.cpp:L150-L191](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L150-L191) — 需求计算现场：第 151 行读 `cudaDevAttrTextureAlignment` 设备属性，第 152-153 行做 lcm 并向上取整到 2 的幂，第 176-187 行从最内维向外推 stride，第 189 行 `AddBuffer(reqs.mem.cudaMem, reqs.strides[0] * reqs.shape[0], reqs.alignBytes)` 把总量与对齐写入需求清单。
- [src/nvcv/src/priv/Tensor.cpp:L194-L216](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L194-L216) — `AllocateBuffer`：`CalcTotalSizeBytes` 压总量 → `alloc.allocCudaMem(bufSize, reqs.alignBytes)`；构造函数把分配器存进 `m_alloc`，析构函数对称调用 `m_alloc->freeCudaMem(...)`——**分配器与 Tensor 同生命周期绑定，谁分配谁释放**。
- [src/nvcv/src/priv/Tensor.cpp:L238-L241](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L238-L241) — `Tensor::alloc()` 访问器：持有 SharedCoreObj，说明分配器被 Tensor 引用计数保活（自定义分配器不能先于 Tensor 死亡）。
- [src/nvcv/src/include/nvcv/alloc/Requirements.hpp:L36-L90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Requirements.hpp#L36-L90) — `Requirements` 类：`ConstMemory`（只读视图：`numBlocks(log2BlockSizeBytes)`）/ `Memory`（可写：`addBuffer`）区分了"生产需求"与"查询需求"两种用法；`operator+=` 支持把多个对象的需求**合并成一份**。
- [src/nvcv/src/include/nvcv/alloc/Requirements.hpp:L109-L139](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Requirements.hpp#L109-L139) — `CalcTotalSizeBytes` 与 `addBuffer` 的内联实现：都委托给 C 函数 `nvcvMemRequirementsCalcTotalSizeBytes` / `nvcvMemRequirementsAddBuffer`（校验与分桶逻辑在 C 侧，供 C 用户复用）。
- 同一消费模式的兄弟姐妹（可各引用一次确认模式一致）：
  - [src/nvcv/src/priv/Image.cpp:L132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Image.cpp#L132) — Image 同样 `alloc.allocCudaMem(bufSize, reqs.alignBytes)`；
  - [src/nvcv/src/priv/ImageBatchVarShape.cpp:L79-L95](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L79-L95) — 变长批要分配**三种**缓冲：设备侧 imageList、格式列表（CUDA 或 host 按缓冲类型）、host 侧句柄数组——正对上 u2-l3 讲过的"双面结构"；
  - [src/nvcv/src/priv/TensorBatch.cpp:L37-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TensorBatch.cpp#L37-L45) 与 [src/nvcv/src/priv/Array.cpp:L139-L148](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Array.cpp#L139-L148) — TensorBatch/Array 按 `memType` 在三种频道间选择。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手算出一个 Tensor 的需求总量并与源码对照。
2. **操作步骤**：
   - 取 `shape = (2, 4, 8, 3)`（N,H,W,C）、`dtype = RGB8`（每元素 3 字节）、`rowAlign` 取默认；
   - 手算：最内维 stride = 3B；W 维采样后一行 = 8×3 = 24B，若 rowAlign=4 则行距对齐到 24（已是 4 的倍数）；依次外推得 `strides[0]*shape[0]`；
   - 对照 [src/nvcv/src/priv/Tensor.cpp:L176-L189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L176-L189) 的循环逐步核对你的手算。
3. **需要观察的现象**：把 W 从 8 改成 7（一行 21B），行距会被 roundUp 到多少？总字节如何变化？
4. **预期结果**：理解"需求阶段决定的 stride"既是分配量的来源，也是后续 kernel 寻址（u5-l2 的 TensorDataAccess）的依据——一份计算，两处消费。具体数值**待本地验证**（可写小程序打印 `Tensor(reqs).exportData()` 的 strides 对照）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `NVCVMemRequirements` 存"按 log₂ 块大小分桶的块数"而不是一个总字节数？
**答案**：为块分配器/内存池预留的协商格式。池化分配器想知道"要几块 1KB、几块 64KB"以便从预分配的块表里取货；只给总量就无法做块级复用。`Requirements.hpp` 文件头注释（L22-26）明确说明该信息的用途是"让分配器可以预分配将要使用的资源"。

**练习 2**：`priv::Tensor` 析构时如何保证用"当初那个"分配器释放？
**答案**：构造时把 `IAllocator&` 存入成员 `m_alloc`（ SharedCoreObj 引用计数保活），析构调用 `m_alloc->freeCudaMem(...)` 并传回与分配时相同的 size/align。因此"cudaMalloc 的配 cudaFree、池子的还给池子"天然配对，无需全程序查表。

**练习 3**：给 `nvcv::Tensor` 传了自定义分配器后，Tensor 的元数据（shape/stride 描述等 host 侧小对象）也走你的自定义分配器吗？
**答案**：Tensor 的显存缓冲走你给的分配器；priv 层宿主侧 C++ 对象另有 `AllocHostObj` 这类工具（[IAllocator.hpp:L57-L80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/IAllocator.hpp#L57-L80)）可走分配器的 host 频道，但句柄管理结构本身由各 Manager 管理。你若只定制了 CUDA 频道，host 频道自动保持默认（见下一模块的回填机制）。

### 4.4 自定义分配器：CustomMemAllocator 与缺省回填

#### 4.4.1 概念说明

自定义分配器解决一类真实问题：把 nvcv 的显存纳入你自己的内存池（如 CUDA VMM、内存池化、统计、NUMA 绑定）。

公开 C++ 侧的用法是两层组合：

1. `nvcv::CustomCudaMemAllocator allocFn(allocLambda, freeLambda)` —— 把一对 lambda 编组成一个频道的 C 描述符（`NVCVResourceAllocator`）；
2. `nvcv::CreateCustomAllocator(allocFn, ...)` —— 聚合 0~3 个频道描述符，创建出完整 `Allocator`（跨 C ABI 调 `nvcvAllocatorConstructCustom`）。

**最关键的语义**：你不必三个频道全都定制。priv 层的 `CustomAllocator` 构造函数会把你定制的频道存入函数指针表，其余频道**自动回填默认分配器**（`GetDefaultAllocator().get(resType)`）。这叫"部分定制"。

lambda 的编组策略（零分配优先）：若 lambda 平凡可拷贝且足够小，直接把其字节内联进 `ctx`（一个指针大小的字段），**零堆分配**；否则堆上建一个 `tuple<Alloc,Free>`，`ctx` 指向它，`cleanup` 负责析构。带捕获（如 `shared_ptr` 状态）的 lambda 走后者，因此 `CustomMemAllocator` 析构前捕获对象一直保活——官方测试 `ConstructCustomWithDeleter` 专门验证了这一点。

#### 4.4.2 核心流程

```text
用户侧
  CustomCudaMemAllocator(allocLambda, freeLambda)
      │  编组：trivial+小 → 内联 ctx；否则堆 tuple + cleanup
      ▼
  CreateCustomAllocator(cudaAllocFn /*, hostAllocFn, ...*/)
      │  收集各频道 cdata() → 数组
      ▼
  C ABI: nvcvAllocatorConstructCustom(descs, n, &handle)
      │  n == 0 → 创建 priv::DefaultAllocator
      │  n  > 0 → 创建 priv::CustomAllocator(descs, n)
      ▼
priv::CustomAllocator 构造
  ├─ 校验：fnAlloc/fnFree 非空、resType 合法、无重复频道
  ├─ 用户频道 → 存表，置 filledMap 位
  └─ 空缺频道 → GetDefaultAllocator().get(i) 回填
使用侧
  allocCudaMem(size, align) → 查表[MEM_CUDA].fnAlloc(ctx, size, align)
```

#### 4.4.3 源码精读

- [src/nvcv/src/include/nvcv/alloc/Allocator.hpp:L288-L353](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp#L288-L353) — `CustomMemAllocator` 模板类与构造函数文档；头文件注释里的示例（L334-344）就是"用捕获引用的 lambda 包装一个已有分配器对象"的官方写法，并警告：**捕获引用时用户须保证被捕获对象活得比分配器久**。
- [src/nvcv/src/include/nvcv/alloc/Allocator.hpp:L446-L448](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/Allocator.hpp#L446-L448) — 三个频道别名：`CustomHostMemAllocator` / `CustomHostPinnedMemAllocator` / `CustomCudaMemAllocator`。
- [src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp:L61-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp#L61-L107) — 编组决策树：`tuple_by_value`（trivial 且装得下 → 内联）；`construct_from_one_value_if_equal`（alloc/free 是同类型同字节 → 共享一份 ctx）；否则堆分配。static_assert 禁止传左值引用，需要引用时用 `std::ref`。
- [src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp:L144-L167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp#L144-L167) — 堆路径 `Construct(false_type)`：`MakeUniqueObj<tuple>` 建上下文，cleanup lambda 用 `UniqueObj` 析构；fnAlloc/fnFree 通过 `std::get<0>/<1>` 调用。带捕获 lambda 的生命周期由此保证。
- [src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp:L200-L209](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/alloc/AllocatorImpl.hpp#L200-L209) — `CustomAllocator` 构造：收集各频道 `cdata()` → `nvcvAllocatorConstructCustom` → `allocators.release()` 把描述符所有权移交（避免析构时 cleanup 提前触发）→ `reset(handle)` 接管句柄。
- [src/nvcv/src/Allocator.cpp:L35-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Allocator.cpp#L35-L56) — C API 分岔点：`numCustomAllocators != 0` 创建 `priv::CustomAllocator`，等于 0 创建 `priv::DefaultAllocator`。所以 `nvcv::CustomAllocator<>{}`（零频道）其实就是一个默认分配器——Python 绑定的 WorkspaceCache 正是这样用的（见下）。
- [src/nvcv/src/priv/CustomAllocator.cpp:L31-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/CustomAllocator.cpp#L31-L107) — **本模块的核心**：先遍历用户描述符做三重校验（函数指针非空 L56-65、类型合法 L50-74、频道不重复 L76-80）并存表置位；再在 L91-103 把所有未定制频道回填为 `GetDefaultAllocator().get(...)`；最后断言三个频道全满。
- [src/nvcv/src/priv/CustomAllocator.cpp:L120-L172](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/CustomAllocator.cpp#L120-L172) — 使用侧转发：每个 `doAllocXxx` 查表取出 `fnAlloc` 原样调用，`doGet` 直接返回表项。析构（L109-118）对所有有 `cleanup` 的表项逐一清理。
- [src/nvcv/src/priv/AllocatorManager.hpp:L27-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/AllocatorManager.hpp#L27-L34) — `ResourceStorage<IAllocator>` 声明合法存储类型为 `CompatibleStorage<DefaultAllocator, CustomAllocator>`：**分配器世界只有这两个实现**，没有第三个。
- 仓库内的真实用例：[python/mod_cvcuda/WorkspaceCache.cpp:L84-L87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/WorkspaceCache.cpp#L84-L87) — Python 绑定的 WorkspaceCache 默认构造用 `nvcv::CustomAllocator<>{}` 作为三频道的分配器来源（即默认函数），u8-l3 将展开这条线。
- 官方测试依据：[tests/nvcv_types/system/TestAllocatorCpp.cpp:L46-L86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/system/TestAllocatorCpp.cpp#L46-L86)（无捕获 lambda + thread_local 状态记录，`needsCleanup()==false`）；[tests/nvcv_types/system/TestAllocatorCpp.cpp:L304-L343](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/system/TestAllocatorCpp.cpp#L304-L343)（`CreateCustomAllocator` 聚合 host+cuda 两频道，`ca.cudaMem().alloc(256)` 触发计数）。

#### 4.4.4 代码实践（动手编码型，本讲主实践）

**实践目标**：实现一个统计分配次数与字节数的自定义 Allocator，创建张量验证它被真实调用，并回答它与 Python 对象缓存的关系。

**操作步骤**：

1. 先读官方测试 [TestAllocatorCpp.cpp:L304-L343](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/system/TestAllocatorCpp.cpp#L304-L343)，注意它的两个技巧：状态用 `thread_local`（无捕获 lambda 也能写）；CUDA 频道的 lambda 内直接 `cudaMalloc/cudaFree`。
2. 参照它写出如下**示例代码**（非仓库原有代码，需自行编译验证）：

```cpp
// stats_alloc.cpp —— 示例代码：统计型自定义分配器
// 依赖: nvcv_types 头文件与 CUDA runtime; 需在具备 GPU 的环境编译运行
#include <nvcv/alloc/Allocator.hpp>
#include <nvcv/Tensor.hpp>
#include <cuda_runtime.h>
#include <cstdio>

struct Stats {                      // 用 shared_ptr 捕获 → 走堆编组路径, 生命周期安全
    long n_allocs = 0, n_frees = 0;
    long long total_bytes = 0, live_bytes = 0;
};

int main()
{
    auto stats = std::make_shared<Stats>();

    auto cudaAllocFn = nvcv::CustomCudaMemAllocator(
        [stats](int64_t size, int32_t /*align*/) -> NVCVMemoryBuffer
        {
            void *mem = nullptr;
            if (cudaMalloc(&mem, size) != cudaSuccess) return nullptr;
            stats->n_allocs++;  stats->total_bytes += size;  stats->live_bytes += size;
            std::printf("[alloc] size=%lld\n", (long long)size);
            return static_cast<NVCVMemoryBuffer>(mem);
        },
        [stats](NVCVMemoryBuffer mem, int64_t size, int32_t /*align*/)
        {
            stats->n_frees++;  stats->live_bytes -= size;
            cudaFree(mem);
        });

    nvcv::Allocator alloc = nvcv::CreateCustomAllocator(cudaAllocFn); // host/pinned 频道自动回填默认

    {   // 用自定义分配器创建一个 NHWC 张量: 分配应恰好发生 1 次
        nvcv::Tensor t({2, 4, 8, 3}, nvcv::DataType{-1 /* 见下方说明 */}, {}, alloc);
        // 说明: 公开 API 用 nvcv::DataType 或由 ImageFormat 构造; 也可用
        // nvcv::Tensor(2, {8, 4}, nvcv::ImageFormat(nvcv::RGB8), {}, alloc)
    }   // t 析构 → freeCudaMem 一次

    std::printf("allocs=%ld frees=%ld live=%lld\n",
                stats->n_allocs, stats->n_frees, stats->live_bytes);
}
```

> 注：示例中 dtype 的构造请以 [src/nvcv/src/include/nvcv/Tensor.hpp:L170-L174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L170-L174) 提供的构造重载为准（推荐 `Tensor(numImages, Size2D, ImageFormat, bufAlign, alloc)` 这条，dtype 语义最清楚）。示例重点在分配器接线，不在张量构造细节。

3. 编译（按 u1-l3 的 preset 体系）：`cmake --preset dev && cmake --build --preset dev` 先产出 `libnvcv_types`，再让示例链接它；或直接把本文件加入一个最小 CMake 目标。
4. 运行并观察统计输出。

**需要观察的现象**：

- `[alloc] size=...` 恰好打印一次（一个 Tensor 一次分配，对照 4.3.2）；
- 析构后 `allocs == frees` 且 `live_bytes == 0`；
- 打印的 size 与你手算的 `strides[0]*shape[0]`（4.3.4 的练习）一致或为其向上对齐值；
- 若再创建一个同 shape 的 Tensor（前一个已析构），计数再加一——因为**这层没有缓存**。

**预期结果**：统计器证明"Tensor 显存确实出自你的分配器"；同时 host 侧小对象不走你的 CUDA 频道（部分定制生效）。具体数值**待本地验证**（本环境无 GPU，无法编译运行 CUDA 程序）。

**回答实践任务的问题——Python 对象缓存与这层 allocator 是什么关系（承接 u4-l2）**：

两层位于不同高度、互补而非替代：

| | Python 对象缓存（u4-l2） | nvcv Allocator（本讲） |
|---|---|---|
| 所在层 | Python 绑定层，仅 Python 有 | C++ 核心，所有语言共用 |
| 解决的问题 | **要不要分配**：同 shape 的输出对象复用外壳与显存，避免走到创建流程 | **怎么分配**：真要分配时，字节从哪来（cudaMalloc / 内存池 / 统计器） |
| 生效时机 | 在 `Tensor::Create` 之前拦截（命中即返回） | 仅在缓存未命中、真正创建 nvcv::Tensor 时被调用 |
| 生命周期 | 按线程缓存、按设备限额、del 后不立即释放 | 与 Tensor 严格配对：构造 alloc、析构 free |

也就是说：缓存命中 → 你的自定义分配器根本不会被调用（Python 侧甚至不传自定义分配器，见下方）；缓存未命中 → 才沿 `Tensor::Create` → 默认 `cudaMalloc`。另注意一个现状：Python 绑定创建 Tensor 时并不暴露分配器参数（Python API 无 alloc 入口），自定义分配器目前是 C/C++ 用户的特性；Python 侧唯一系统性使用 Allocator 抽象的地方是算子 Workspace 内存（`WorkspaceCache` 用 `nvcv::CustomAllocator<>{}`，见 4.4.3），它将在 u8-l3 展开。

#### 4.4.5 小练习与答案

**练习 1**：只定制 CUDA 频道、不定制 host 频道，创建 Tensor 时元数据分配会不会用到你的 lambda？
**答案**：不会。`priv::CustomAllocator` 构造时把 host 频道回填为默认分配器的描述符（[CustomAllocator.cpp:L91-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/CustomAllocator.cpp#L91-L103)），只有 `NVCV_RESOURCE_MEM_CUDA` 频道的表项指向你的函数。

**练习 2**：为什么 `CustomMemAllocator` 的构造函数禁止传入左值引用类型的可调用对象（static_assert），并提示用 `std::ref`？
**答案**：编组时描述符可能只保存对象的字节拷贝（trivial 内联路径）或堆上的移动构造副本；左值引用被推导会绕过这些语义，导致悬空引用。`std::ref` 显式表达"我要按引用共享"，此时用户自行负责被引用对象的生命周期（头文件 L65-68 的断言消息与 L346-347 的警告都强调这一点）。

**练习 3**：`nvcv::CustomAllocator<>{}`（空模板参数）创建的是什么？Python 绑定哪里用到了它？
**答案**：零个频道描述符 → `nvcvAllocatorConstructCustom(nullptr, 0, &h)` → `priv::DefaultAllocator`，即"标准默认分配器"的对象化写法。`python/mod_cvcuda/WorkspaceCache.cpp` 的默认构造函数用它作为 workspace 三频道内存的分配来源。

## 5. 综合实践

**任务：给"Tensor 创建"装上仪表盘，量化三层复用机制的边界。**

结合本讲全部模块，完成一个小型 C++ 实验（可放入独立目录，勿改动仓库源码）：

1. **实现统计分配器**：按 4.4.4 的示例实现 `Stats` + `CustomCudaMemAllocator` + `CreateCustomAllocator`，统计 alloc/free 次数、单次 size、累计字节。
2. **接两种容器**：分别用它创建（a）一个固定批 `nvcv::Tensor(4, {640,480}, RGB8, {}, alloc)`；（b）一个 `nvcv::ImageBatchVarShape`（capacity=4，绑定 4 张不同尺寸的 Image）。对照 [Image.cpp:L132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Image.cpp#L132) 与 [ImageBatchVarShape.cpp:L79-L95](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L79-L95)，预测各自的分配次数与频道（cuda vs host），再用统计器验证。
3. **验证需求协商**：对 (a) 手算 `CalcTotalSizeBytes`（含行距对齐），与 `[alloc] size=` 输出比对；再故意把 `TensorShape` 换成不对齐的宽度（如 641），观察 size 跳变。
4. **验证配对释放**：让容器离开作用域，确认 `allocs == frees`、`live_bytes == 0`。
5. **写结论**：回答"若把这套统计器搬到 Python 侧等价场景（循环创建同 shape Tensor），计数会怎样？"——预期：Python 对象缓存命中时计数远低于循环次数；用 `_into` 变体（u3-l3）则接近零。给出你的解释框架（缓存挡在 allocator 之前）。

**验收标准**：一份记录表（容器 × 预测分配次数 × 实测 × 频道）+ 一段关于"缓存层 vs 分配层"的结论。GPU 环境不可用时，步骤 1-2 可降级为"源码推演 + 待本地验证"并在表中注明。

## 6. 本讲小结

- 分配器的最底层契约是纯 C 的 `NVCVResourceAllocator`：`fnAlloc`/`fnFree` 函数指针 + 透传 `ctx` + `cleanup`；三个频道对应 host / CUDA / host-pinned 三种内存。
- 公开 C++ 的 `Allocator` 是引用计数句柄（`CoreResource`），`hostMem()/cudaMem()/get()` 拆出单频道视图；priv 层 `IAllocator` 用 NVI 把"校验（size≥0、2 的幂、整除对齐）"集中在公开层。
- `DefaultAllocator` 是无状态的唯一策略对象（全局上下文成员）：host 用对齐 new/delete、pinned 用 `cudaHostAlloc(WC|Mapped)`、CUDA 用 `cudaMalloc`，三者都做对齐复查、失败即抛；`GetAllocator(nullptr)` 把空句柄解析到它。
- 容器与分配器通过 `Requirements` 协商：Tensor 先算 stride/alignBytes（lcm+取整到 2 的幂）并 `addBuffer` 入按 log₂ 块大小分桶的清单，分配时 `CalcTotalSizeBytes` 压总量、`allocCudaMem` 一次性给货；分配器被 Tensor 引用计数保活，析构对称归还。
- 自定义分配器 = 公开侧 `CustomMemAllocator`（lambda 编组：小而平凡则零堆分配内联进 ctx，否则堆 tuple+cleanup）+ `CreateCustomAllocator` 聚合 → priv 侧 `CustomAllocator` **只替换你定制的频道，其余回填默认**；仓库内 Python WorkspaceCache 用 `CustomAllocator<>{}` 即默认分配器的对象化写法。
- Python 对象缓存（u4-l2）决定"要不要走到分配"，Allocator 决定"分配从哪拿内存"；缓存命中的创建根本不会触碰 allocator，且 Python API 目前不暴露分配器参数——自定义分配器是 C/C++ 侧特性。

## 7. 下一步学习建议

- **u8-l3 Workspace 与 per-stream 缓存**：看 Python 绑定如何以 `nvcv::Allocator` 为底座实现算子临时内存的三频道租借（`WorkspaceCache::get`/`WorkspaceLease`），并与本讲的 `CustomAllocator<>{}` 接线呼应。
- **通读同类消费者**：精读 [src/nvcv/src/priv/ImageBatchVarShape.cpp:L60-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L60-L100) 与 [src/nvcv/src/priv/TensorBatch.cpp:L30-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TensorBatch.cpp#L30-L60)，体会"多缓冲需求清单"的真实形态。
- **C API 视角**：对照 [src/nvcv/src/Allocator.cpp:L122-L204](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Allocator.cpp#L122-L204) 的六个 `nvcvAllocatorAlloc*/Free*` C 函数，练习用纯 C 写一个自定义分配器并传给 `nvcvTensorConstruct`（u6-l1 的句柄式生命周期在此复用）。
- **延伸测试**：浏览 [tests/nvcv_types/system/TestAllocatorC.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/system/TestAllocatorC.cpp)，比较 C 与 C++ 两套测试对同一契约的不同表达方式。
