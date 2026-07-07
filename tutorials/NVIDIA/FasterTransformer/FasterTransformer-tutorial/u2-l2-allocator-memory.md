# GPU 显存管理：Allocator 与 memory_utils

## 1. 本讲目标

在 [u2-l1](u2-l1-tensor-data-structure.md) 里我们看到，`Tensor` 只是一个**不拥有内存**的轻量描述符——它只记录指针、形状、数据在 CPU 还是 GPU，却既不 `malloc` 也不 `free`。那么真正的 GPU 显存到底是谁分配、谁释放、谁负责复用？本讲就来回答这个问题。

读完本讲，你应当能够：

1. 说出 FasterTransformer（后简称 FT）里**两条**显存分配路径的区别：`IAllocator`（带复用、给每步 forward 的工作区用）与 `memory_utils` 的 `deviceMalloc/deviceFree`（一次性、给权重等长期存活的对象用）。
2. 看懂 `IAllocator` 抽象接口，以及它在 `CUDA / TF / TH` 三种框架下的三种实现差异。
3. 讲清楚 `ReallocType` 的 `INCREASE / REUSE / DECREASE` 三种语义，以及 `reMalloc` 如何据此决定是「直接复用旧 buffer」还是「释放后重新分配」。
4. 说明 CUDA 11.2 引入的**异步内存池**（`cudaMallocAsync`）为什么能让显存复用变得近乎零开销，以及编译期宏 `CUDA_MEMORY_POOL_DISABLED` 的作用。
5. 解释为什么「buffer 复用」能大幅减少 `cudaMalloc` 调用次数，从而加速推理热路径。

---

## 2. 前置知识

- **GPU 显存为什么贵**：在 GPU 上调用一次 `cudaMalloc` 是一个很重的同步操作（动辄几十到几百微秒），而且会触发驱动层面的页表建立。如果每个 transformer 层、每一步生成都重新 `malloc/free` 一次工作区，显存管理的开销会盖过 kernel 本身的计算开销。
- **推理场景的显存特征**：推理时每次 `forward` 用到的临时 buffer 形状几乎固定（只取决于 batch、seq_len 等配置），而且会**反复使用**。这天然适合「分配一次、长期复用」的策略，而不是「用完即释放」。
- **CUDA Stream**：GPU 上的异步命令队列。`cudaMallocAsync` / `cudaMemsetAsync` 都绑定到某个 stream，从而与该 stream 上的 kernel 异步串联。
- **C++ 模板与纯虚函数**：`IAllocator` 用纯虚函数定义接口，再用模板偏特化给出 `CUDA/TF/TH` 三套实现。如果你对「纯虚基类 + 模板特化」不熟，只需把它理解成「先定义统一接口，再为不同后端各写一份实现」。
- 建议先读完 [u2-l1](u2-l1-tensor-data-structure.md)，理解 `Tensor` 是「非拥有」描述符这一前提。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fastertransformer/utils/allocator.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h) | 定义 `AllocatorType`/`ReallocType` 枚举、`IAllocator` 抽象基类（含核心的 `reMalloc` 复用算法），以及 `CUDA/TF/TH` 三种实现。**本讲的主战场。** |
| [src/fastertransformer/utils/memory_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.h) | 声明一组「一次性」显存工具函数：`deviceMalloc`/`deviceFree`/`deviceMemSetZero` 等。 |
| [src/fastertransformer/utils/memory_utils.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu) | 上述工具函数的 CUDA 实现，是对 `cudaMalloc`/`cudaFree`/`cudaMemcpy` 的薄封装。 |
| examples/cpp/bert/bert_example.cc | 示例：在 C++ 入口里如何 `new` 一个 `Allocator<AllocatorType::CUDA>`。 |
| src/fastertransformer/models/t5/T5Decoding.cc | 真实模型里 `reMalloc` 的典型调用写法。 |

> 一句话区分：`allocator.h` 负责**可复用的工作区显存**；`memory_utils.*` 负责**一次性的原始显存**（以及各种拷贝/类型转换工具）。两者职责不重叠。

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要两套显存管理：动机与全景

#### 4.1.1 概念说明

FT 里所有「临时的、每步 forward 都要用、形状随配置而变」的显存（比如 attention 的中间矩阵、FFN 的输出 buffer、KV cache），都由 `IAllocator` 统一管理。它的核心能力是**记住自己之前分配过的每一块 buffer 及其大小**，下次再要同样大小的 buffer 时，直接把旧的那块还给你，而不是再去敲一次 `cudaMalloc`。

而模型权重、embedding 表这类「加载一次、整个生命周期都在」的显存，则用更轻的 `memory_utils::deviceMalloc` 一次性分配——它们不需要复用逻辑，因为根本不会被频繁释放。

于是 FT 形成了清晰的分工：

- `IAllocator`（本讲 4.2–4.4）：**热路径**，反复 `reMalloc`，靠复用躲开 `cudaMalloc`。
- `deviceMalloc/deviceFree`（本讲 4.5）：**冷路径**，一次性分配/释放，仅是 `cudaMalloc` 的安全封装。

#### 4.1.2 核心流程

```text
模型 forward 需要一块临时 buffer
        │
        ├─ 这是「每步都用的工作区」？ ──► allocator_->reMalloc(buf_, size)
        │                                    （查表 → 复用 or 重分配）
        │
        └─ 这是「加载一次的权重」？     ──► deviceMalloc(&weight, n)
                                             （直接 cudaMalloc，不复用）
```

#### 4.1.3 源码精读

真实模型里 `reMalloc` 的典型调用长这样（来自 T5Decoding）：

[src/fastertransformer/models/t5/T5Decoding.cc:94](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L94) —— 把 `reMalloc` 的返回值**重新赋回**成员指针 `decoder_output_buf_`。

```cpp
decoder_output_buf_ = (T*)(allocator_->reMalloc(decoder_output_buf_, sizeof(T) * batchxbeam * d_model_, false));
```

注意 `buf_ = (T*)allocator_->reMalloc(buf_, ...)` 这个「自赋值」写法是**强制的惯用法**：因为当需要扩容时 `reMalloc` 会返回一个**新地址**，必须写回成员变量，否则下次用到的是已释放的旧指针。

而构造 allocator 的入口在示例里非常简洁：

[examples/cpp/bert/bert_example.cc:87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L87)

```cpp
Allocator<AllocatorType::CUDA> allocator(getDevice());
```

#### 4.1.4 代码实践

**实践目标**：在源码里分辨「热路径」与「冷路径」两种显存使用。

**操作步骤**：

1. 打开 `src/fastertransformer/utils/allocator.h`，找到 `reMalloc`（见 4.3）。
2. 打开 `src/fastertransformer/utils/memory_utils.cu`，找到 `deviceMalloc`（见 4.5）。
3. 在 `src/fastertransformer/models/` 下任意挑一个模型（如 `bert/Bert.cc`），用搜索功能分别统计 `reMalloc(` 与 `deviceMalloc(` 各出现多少次。

**需要观察的现象**：模型 forward 主循环里出现的基本都是 `reMalloc`；而权重加载流程（`loadWeightFromBin` 及其内部，见 `memory_utils.cu`）才会出现 `deviceMalloc`。

**预期结果**：你会直观看到「热路径用 reMalloc、冷路径用 deviceMalloc」的分工。

#### 4.1.5 小练习与答案

**练习**：为什么权重 buffer 不需要 `reMalloc` 的复用机制，直接 `deviceMalloc` 就行？

**参考答案**：权重在模型构造时加载一次，整个推理生命周期里都不会改变大小、也不会被释放，根本不存在「重复请求同样大小 buffer」的场景，因此复用机制对它没有收益，反而增加 `pointer_mapping_` 的记账开销。一次性 `cudaMalloc` 更直接。

---

### 4.2 IAllocator 抽象与三种实现（AllocatorType）

#### 4.2.1 概念说明

`IAllocator` 是一个纯虚基类，定义了「分配 / 释放 / 设置 stream / memset」这一组统一接口。它的存在让 FT 的模型代码可以**面向接口编程**：模型只持有 `IAllocator*` 指针，不关心底层到底是裸 CUDA、TensorFlow 还是 PyTorch 在提供显存。

`AllocatorType` 枚举就是用来选择这三套后端的：

[src/fastertransformer/utils/allocator.h:52-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L52-L56)

```cpp
enum class AllocatorType {
    CUDA,
    TF,
    TH
};
```

- `CUDA`：FT 自己用 `cudaMallocAsync`/`cudaMalloc` 管理，C++ 示例与 TensorRT plugin 都用它。
- `TF`：把分配委托给 TensorFlow 的 `OpKernelContext::allocate_temp`，由 TF 的 BFC allocator 实际出内存（`#ifdef GOOGLE_CUDA` 守卫）。
- `TH`：委托给 PyTorch 的 `torch::empty(..., device(kCUDA))`，由 PyTorch 的 CachingAllocator 出内存（`#ifdef TORCH_CUDA` 守卫）。

#### 4.2.2 核心流程

`IAllocator` 的对外接口（纯虚函数）只有 5 个：

[src/fastertransformer/utils/allocator.h:64-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L64-L72)

```cpp
virtual void*        malloc(size_t size, const bool is_set_zero = true, bool is_host = false) = 0;
virtual void         free(void** ptr, bool is_host = false) const                             = 0;
virtual void         setStream(cudaStream_t stream)                                           = 0;
virtual cudaStream_t returnStream()                                                           = 0;
virtual void         memSet(void* ptr, const int val, const size_t size)                      = 0;
```

三套实现都要落实这 5 个函数，差别只在于「malloc/free 背后真正调谁」。注意 `free` 的签名是 `void**`（二级指针），目的是释放后能把调用方的指针置空（见 4.4）。

#### 4.2.3 源码精读

三套实现的核心差异，可以浓缩成一张表（行号都指向 `allocator.h`）：

| 维度 | `Allocator<CUDA>` | `Allocator<TF>` | `Allocator<TH>` |
| --- | --- | --- | --- |
| 守卫宏 | 无（默认可用） | `GOOGLE_CUDA` | `TORCH_CUDA` |
| 真正分配 | `cudaMallocAsync` / `cudaMalloc` | `context_->allocate_temp` | `torch::empty(..., kCUDA)` |
| 真正释放 | `cudaFreeAsync` / `cudaFree` | 仅从 map 删除（TF 自己回收） | 仅从 map 删除（PyTorch 自己回收） |
| `pointer_mapping_` 的值类型 | `size_t`（记录字节数） | `tensorflow::Tensor`（持有所有权） | `torch::Tensor`（持有所有权） |

CUDA 实现的 `pointer_mapping_` 字段：

[src/fastertransformer/utils/allocator.h:127](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L127)

```cpp
std::unordered_map<void*, size_t>* pointer_mapping_;
```

它就是「地址 → 这块 buffer 有多少字节」的账本，是后面 4.3 复用判断的唯一依据。

TF 实现把分配完全交给框架：

[src/fastertransformer/utils/allocator.h:318-346](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L318-L346) —— `malloc` 内部调用 `context_->allocate_temp(DT_UINT8, ...)` 拿到一块 `tensorflow::Tensor`，把它的底层指针登记进 map。

TH（PyTorch）实现同理：

[src/fastertransformer/utils/allocator.h:420-438](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L420-L438) —— `torch::empty({buf_size}, torch::dtype(torch::kUInt8).device(torch::kCUDA))` 造一个 `torch::Tensor`，用 `data_ptr()` 取出裸指针登记。

注意 TF/TH 实现的 `free` 并不真正释放显存，只是 `erase` 掉 map 里的记录——因为 `tensorflow::Tensor`/`torch::Tensor` 的析构（在 map 清理时触发）会由框架自己的 allocator 负责回收，框架通常也会缓存复用。所以「复用」这件事，在 CUDA 后端由 FT 自己做，在 TF/TH 后端则部分交给了框架。

#### 4.2.4 代码实践

**实践目标**：理解同一个 `IAllocator*` 接口在不同后端下的行为差异。

**操作步骤**：

1. 读 `allocator.h` 的 `Allocator<CUDA>::malloc`（4.4 节给出）与 `Allocator<TH>::malloc`（[L420-L438](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L420-L438)）。
2. 对比两者对 `is_set_zero=true` 的处理：CUDA 用 `cudaMemsetAsync`，TH 用 `cudaMemset`（同步）。

**需要观察的现象**：CUDA 版本的 memset 是 **Async**（绑定 stream），TH 版本是同步 `cudaMemset`。

**预期结果 / 待本地验证**：在纯 CUDA 模式下，多次分配可以充分重叠在 stream 上；嵌入 PyTorch 时则遵循 PyTorch 的执行模型。具体性能差异需在本地 GPU 上验证。

#### 4.2.5 小练习与答案

**练习**：为什么 `free` 设计成接收 `void**`（二级指针）而不是 `void*`？

**参考答案**：因为释放后希望同时把调用方的指针置为 `nullptr`，避免悬空指针被再次使用。看 CUDA 实现 [allocator.h:261](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L261) 末尾的 `*ptr = nullptr;`——它直接改写调用方传入的那根指针变量，所以必须传指针的地址（二级指针）。

---

### 4.3 ReallocType 与 reMalloc：buffer 复用的核心算法

#### 4.3.1 概念说明

`reMalloc` 是 `IAllocator` 里**最关键**的一个模板方法，它实现了「优先复用、必要时才重分配」的策略。它依据另一个枚举 `ReallocType` 来决策：

[src/fastertransformer/utils/allocator.h:58-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L58-L62)

```cpp
enum class ReallocType {
    INCREASE,   // 旧 buffer 装不下，必须释放后分配更大的
    REUSE,      // 旧 buffer 大小正好，直接复用
    DECREASE,   // 旧 buffer 比需要的大（仅在内存池开启时才真正回收）
};
```

判断规则很朴素——拿「账本里记录的旧大小」和「本次请求的新大小」比：新 > 旧 → `INCREASE`；相等 → `REUSE`；新 < 旧 → `DECREASE`。

#### 4.3.2 核心流程

`reMalloc` 的判定逻辑（伪代码）：

```text
reMalloc(ptr, size):
    size = align_to_32_bytes(size)          # 32 字节对齐
    if ptr 在 pointer_mapping_ 中:
        type = 比较(旧大小, size)
        if type == INCREASE:                 # 装不下
            free(ptr); return malloc(size)   # 释放旧的，分配更大的
        elif type == DECREASE  (且内存池开启):
            free(ptr); return malloc(size)   # 回收多余内存到池
        else:                                 # REUSE，或池关闭时的 DECREASE
            if is_set_zero: memSet(ptr, 0, size)
            return ptr                        # 直接复用，零 malloc
    else:
        return malloc(size)                   # 新地址，直接分配
```

CUDA 实现里「比较」这一步：

[src/fastertransformer/utils/allocator.h:133-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L133-L145)

```cpp
ReallocType isReMalloc(void* address, size_t size) const {
    FT_CHECK(isExist(address));
    if (pointer_mapping_->at(address) < size) {
        return ReallocType::INCREASE;
    }
    else if (pointer_mapping_->at(address) == size) {
        return ReallocType::REUSE;
    }
    else {
        return ReallocType::DECREASE;
    }
}
```

#### 4.3.3 源码精读

`reMalloc` 的完整实现：

[src/fastertransformer/utils/allocator.h:74-107](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L74-L107)

关键片段（保留行号注释便于对照）：

```cpp
template<typename T>
void* reMalloc(T* ptr, size_t size, const bool is_set_zero = true, bool is_host = false)
{
    size = ((size + 31) / 32) * 32;            // L78: 32 字节对齐
    void* void_ptr    = (void*)ptr;
    void* ptr_address = getAddress(void_ptr);
    if (isExist(ptr_address)) {
        ReallocType realloc_type = isReMalloc(ptr_address, size);
        if (realloc_type == ReallocType::INCREASE) {                 // L83
            free((void**)(&void_ptr), is_host);
            return malloc(size, is_set_zero, is_host);
        }
    #if !defined(CUDA_MEMORY_POOL_DISABLED)                          // L88
        else if (realloc_type == ReallocType::DECREASE) {
            free((void**)(&void_ptr), is_host);
            return malloc(size, is_set_zero, is_host);
        }
    #endif
        else {                                                        // L95: REUSE（或池关闭时降级的 DECREASE）
            if (is_set_zero) {
                memSet(void_ptr, 0, size);
            }
            return void_ptr;
        }
    }
    else {                                                            // L103: 全新指针
        return malloc(size, is_set_zero, is_host);
    }
}
```

要特别留意两个细节：

1. **32 字节对齐**（L78）：`((size + 31) / 32) * 32`。GPU 上向量化读写（如 256-bit load）要求数据对齐，统一向上对齐到 32 字节，能避免未对齐访问带来的性能惩罚，也让相邻 buffer 的地址整齐。
2. **DECREASE 分支被 `CUDA_MEMORY_POOL_DISABLED` 守卫**（L88）：只有在启用了 CUDA 内存池（CUDA ≥ 11.2）时，才会对「请求变小」的情况真正 free+重分配，目的是把多余内存**还给内存池**供别人用；若没有内存池（老版 CUDA），则直接落到 `else` 分支，**保留旧的大 buffer 当作复用**——因为老式 `cudaFree`+`cudaMalloc` 太贵，不如直接留着。

#### 4.3.4 代码实践

> 这正是本讲规格指定的实践任务。

**实践目标**：用 `IAllocator` 接口写一段伪代码，演示三种 `ReallocType`，并解释为什么这样能减少 `cudaMalloc` 调用。

**操作步骤**（伪代码，对照上面的真实实现理解）：

```cpp
// 示例代码：演示 reMalloc 的三种复用语义（非仓库原码，仅作教学）
Allocator<AllocatorType::CUDA> alloc(getDevice());

T* buf = nullptr;

// 第 1 次：buf 不存在 → 走 L103 分支 → 触发一次 cudaMallocAsync，得到 1024 字节
buf = (T*)alloc.reMalloc(buf, 1024, /*is_set_zero=*/true);

// 第 2 次：请求同样 1024 字节 → isReMalloc 返回 REUSE → 走 L95 else 分支
//         不调用 cudaMalloc，只 memset，直接返回原地址
buf = (T*)alloc.reMalloc(buf, 1024, /*is_set_zero=*/true);

// 第 3 次：请求 2048 字节（更大）→ INCREASE → L83 分支
//         free 旧的 + malloc 更大的（这一次才再次触碰 cudaMalloc）
buf = (T*)alloc.reMalloc(buf, 2048, /*is_set_zero=*/true);

// 第 4 次：请求 512 字节（更小）→ DECREASE
//         若 CUDA ≥ 11.2：free+malloc（操作池，几乎零开销）
//         若 CUDA <  11.2：降级为 REUSE，继续用 2048 那块
buf = (T*)alloc.reMalloc(buf, 512, /*is_set_zero=*/false);
```

**需要观察的现象 / 预期结果**：在「形状不变」的稳态推理里（第 2 次及以后），`reMalloc` 几乎都走 `REUSE` 分支，**完全不调用 `cudaMalloc`**，只做一次 memset。只有 batch/seq_len 变化导致形状变大时（第 3 次）才真正扩容。

**为什么能减少 `cudaMalloc`**：因为 `pointer_mapping_` 这个账本让分配器「记得」每个地址对应的容量。稳态下每次请求都能命中 `REUSE`，把 N 次 forward 中的 N 次 `cudaMalloc` 压成「首次 1 次 + 偶发扩容几次」。而偶发的 free+malloc 又因为 CUDA 内存池（见 4.4）只是池内的指针搬运，不触碰驱动层，所以即便扩容也很便宜。

> 说明：规格里把「请求更小尺寸」描述为「复用（REUSE）」。严格按源码，**等尺寸**才是 `REUSE`，**更小尺寸**是 `DECREASE`；只有在 CUDA < 11.2（无内存池）时，`DECREASE` 才会「降级」为保留旧大 buffer 的复用行为，这与「复用」的直觉一致。

#### 4.3.5 小练习与答案

**练习 1**：如果同一个 `buf` 第一次请求 1000 字节、第二次请求 100 字节、第三次又请求 1000 字节（CUDA ≥ 11.2），三次分别走哪个分支？期间发生几次真正的 `cudaMalloc`？

**参考答案**：第 1 次 `INCREASE`/新分配（1 次 malloc，且因对齐实际分配 1024 字节）；第 2 次 `DECREASE`，池开启 → free+malloc（操作内存池，不计为昂贵的驱动级 malloc）；第 3 次 `INCREASE` → free+malloc。账本里始终记录「当前地址的真实容量」，所以判断永远基于实际容量而非历史峰值。

**练习 2**：为什么 `reMalloc` 是模板函数 `template<typename T>`？

**参考答案**：为了能接受任意类型化指针 `T* ptr` 并在内部统一转成 `void*` 去查账本。调用方写 `reMalloc(buf_, sizeof(T)*n)` 时，`T` 由 `buf_` 的类型自动推导，省去手动 `(void*)` 转换，也让 `sizeof(T)` 这类计算更自然。

---

### 4.4 CUDA 异步内存池：cudaMallocAsync 与 CUDA 11.2

#### 4.4.1 概念说明

前面反复提到「内存池」。这是 CUDA 11.2 引入的 ** asynchronous memory pool **（`cudaMallocAsync` / `cudaFreeAsync`）。它的本质是：驱动在每张 GPU 上维护一个**进程内的显存池**，`cudaMallocAsync` 优先从池里切一块出来，`cudaFreeAsync` 把内存还回池里——两者都**不触碰操作系统的显存分配 syscall**，因而极快、且可异步排进 stream。

FT 用一个编译期宏来判断当前 CUDA 运行时是否支持它：

[src/fastertransformer/utils/allocator.h:46-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L46-L48)

```cpp
#if defined(CUDART_VERSION) && CUDART_VERSION < 11020
#define CUDA_MEMORY_POOL_DISABLED
#endif
```

`CUDART_VERSION < 11020`（即低于 11.2）时就定义 `CUDA_MEMORY_POOL_DISABLED`，于是前面 `reMalloc` 的 `DECREASE` 分支被编译掉，`malloc/free` 也退化为同步的 `cudaMalloc/cudaFree`。

#### 4.4.2 核心流程

`Allocator<CUDA>` 的构造函数在池可用时做了两件关键事：

1. **配置 peer access**：让本卡默认内存池可以访问其它卡的显存（多卡场景）。
2. **把池的「释放阈值」设为 `UINT64_MAX`**：意思是池**永远不把内存还给操作系统**，始终保留峰值占用，使后续分配都是池内零开销操作。

数学上，池的占用量 \( P(t) \) 满足

\[
P(t) = \min\bigl(\text{peak}_t,\ \text{threshold}\bigr),\qquad \text{threshold} = 2^{64}-1
\]

即一旦涨到峰值就「锁住」，不再回落（直到 allocator 析构）。

#### 4.4.3 源码精读

构造函数里的池配置：

[src/fastertransformer/utils/allocator.h:148-182](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L148-L182)

```cpp
Allocator(int device_id): device_id_(device_id) {
    pointer_mapping_ = new std::unordered_map<void*, size_t>();
#if defined(CUDA_MEMORY_POOL_DISABLED)
    FT_LOG_WARNING("Async cudaMalloc/Free is not supported before CUDA 11.2 ...");
#else
    // ... 取默认池、为每张可 peer-access 的卡设置 cudaMemPoolSetAccess ...
    uint64_t setVal = UINT64_MAX;
    check_cuda_error(cudaMemPoolSetAttribute(mempool, cudaMemPoolAttrReleaseThreshold, &setVal));
#endif
}
```

`malloc` 与 `free` 在两种模式下的分流：

[src/fastertransformer/utils/allocator.h:203-232](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L203-L232) —— 池开启走 `cudaMallocAsync(&ptr, ..., stream_)`，关闭走同步 `cudaMalloc(&ptr, ...)`；分配后登记 `{ptr, size}` 进账本。

[src/fastertransformer/utils/allocator.h:234-263](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L234-L263) —— 池开启走 `cudaFreeAsync(*ptr, stream_)` 后**再 `cudaStreamSynchronize(stream_)`**，关闭走同步 `cudaFree(*ptr)`；释放后从账本 `erase`。

```cpp
// malloc（节选）
#if defined(CUDA_MEMORY_POOL_DISABLED)
    check_cuda_error(cudaMalloc(&ptr, (size_t)(ceil(size / 32.)) * 32));
#else
    check_cuda_error(cudaMallocAsync(&ptr, (size_t)(ceil(size / 32.)) * 32, stream_));
#endif
```

```cpp
// free（节选）
#if defined(CUDA_MEMORY_POOL_DISABLED)
    check_cuda_error(cudaFree(*ptr));
#else
    check_cuda_error(cudaFreeAsync(*ptr, stream_));
    cudaStreamSynchronize(stream_);
#endif
```

析构函数会把账本里所有未释放的 buffer 逐个 `free` 掉，避免泄漏：

[src/fastertransformer/utils/allocator.h:184-191](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L184-L191)

#### 4.4.4 代码实践

**实践目标**：体会「池开启 vs 关闭」对热路径分配开销的影响。

**操作步骤**：

1. 确认本机 CUDA 运行时版本：`nvcc --version` 看 `CUDART_VERSION`（≥ 11.2 即池可用）。
2. 编译任意 C++ 示例（如 bert_example），在 `FT_LOG_LEVEL=DEBUG` 下运行，观察日志里 `ReMalloc the buffer ...` / `Reuse original buffer ...` 的出现频率。

**需要观察的现象**：稳态推理时，日志几乎全是 `Reuse original buffer`；只有在 batch 变化时才偶尔出现 `ReMalloc the buffer ... since it is too small`。

**预期结果 / 待本地验证**：池开启时，即便触发 `ReMalloc`，延迟也应远低于「冷启动」的首次分配；具体数值需在本地 GPU 上用 Nsight Systems 量化验证（可参考 [u1-l5](u1-l5-logging-debug-env.md) 的 NVTX 用法）。

#### 4.4.5 小练习与答案

**练习**：为什么 `cudaFreeAsync` 之后还要跟一句 `cudaStreamSynchronize(stream_)`？

**参考答案**：`cudaFreeAsync` 只是把「释放」这个动作异步排进 stream。FT 在这里显式同步一次，确保这块内存在被池重新分配给别人之前，其上排队的 kernel 已经全部完成，避免出现「数据还在写、显存却已被池回收给另一个分配」的竞争。这是一种偏保守但安全的写法。

---

### 4.5 memory_utils 原语：deviceMalloc / deviceFree

#### 4.5.1 概念说明

`memory_utils` 里的 `deviceMalloc`/`deviceFree` 是一对**最薄的封装**：它们直接调用 `cudaMalloc`/`cudaFree`，**不复用、不记账、不区分池**。它们的定位是「我明确知道这块显存要存在很久、不需要复用逻辑」的场景，最典型的就是加载模型权重。

注意：它和 `IAllocator::malloc` 是**两条独立路径**。`IAllocator` 是「智能的、带账本带池的」，而 `deviceMalloc` 是「傻瓜的、一次性的」。

#### 4.5.2 核心流程

```text
deviceMalloc(&p, n):           # 在 GPU 上分配 n 个 T
    cudaMalloc(&p, sizeof(T)*n)
    可选: 用随机值初始化 (供测试用)

deviceFree(p):                 # 释放，且把指针置空
    if p != NULL:
        cudaFree(p); p = NULL
```

#### 4.5.3 源码精读

声明在头文件里，是模板函数：

[src/fastertransformer/utils/memory_utils.h:25-32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.h#L25-L32)

```cpp
template<typename T> void deviceMalloc(T** ptr, size_t size, bool is_random_initialize = true);
template<typename T> void deviceMemSetZero(T* ptr, size_t size);
template<typename T> void deviceFree(T*& ptr);
```

实现在 `.cu` 里：

[src/fastertransformer/utils/memory_utils.cu:28-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu#L28-L36) —— `deviceMalloc` 调 `cudaMalloc`，并可选地用 `cudaRandomUniform` 填随机值（这是为了在示例里用随机假权重跑通流程，呼应 [u1-l4](u1-l4-first-run-examples.md) 提到的「建随机假权重」）。

```cpp
template<typename T>
void deviceMalloc(T** ptr, size_t size, bool is_random_initialize) {
    FT_CHECK_WITH_INFO(size >= ((size_t)0), "Ask deviceMalloc size " + std::to_string(size) + "< 0 is invalid.");
    check_cuda_error(cudaMalloc((void**)(ptr), sizeof(T) * size));
    if (is_random_initialize) {
        cudaRandomUniform(*ptr, size);
    }
}
```

[src/fastertransformer/utils/memory_utils.cu:70-77](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu#L70-L77) —— `deviceFree` 做了**空指针保护**，且释放后置空。

```cpp
template<typename T>
void deviceFree(T*& ptr) {
    if (ptr != NULL) {
        check_cuda_error(cudaFree(ptr));
        ptr = NULL;
    }
}
```

这里有几个值得对比的设计差异：

- `deviceMalloc` 用的是**同步** `cudaMalloc`，不是 `cudaMallocAsync`——因为它服务的是一次性、启动期的分配，不在乎那点同步开销，反而需要确定性。
- `deviceFree` 的参数是 `T*&`（引用），所以函数内 `ptr = NULL` 能直接改写调用方的指针；而 `IAllocator::free` 用的是 `void**`，目的相同但写法不同。
- `deviceMalloc` 之后紧跟的 `cudaRandomUniform` 默认开启（`is_random_initialize=true`），方便示例代码直接拿随机数据跑通；加载真实权重时会传 `false`（见 `loadWeightFromBinFunc` 里 [memory_utils.cu:357](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu#L357) 的 `deviceMalloc(&ptr_2, host_array.size(), false)`）。

#### 4.5.4 代码实践

**实践目标**：动手用 `deviceMalloc`/`deviceFree` 分配并释放一块显存，理解它与 `reMalloc` 的区别。

**操作步骤**（伪代码，可在自己的小 CUDA 程序里复现）：

```cpp
// 示例代码：deviceMalloc / deviceFree 用法（非仓库原码，仅作教学）
#include "src/fastertransformer/utils/memory_utils.h"
using namespace fastertransformer;

half* d_ptr = nullptr;
deviceMalloc(&d_ptr, 1024, /*is_random_initialize=*/false);  // 分配 1024 个 half
deviceMemSetZero(d_ptr, 1024);                                // 清零
// ... 这里 d_ptr 可作为「权重」长期使用 ...
deviceFree(d_ptr);   // 释放，且 d_ptr 自动被置为 nullptr
```

**需要观察的现象**：`deviceFree` 之后 `d_ptr` 变成 `nullptr`，再次调用 `deviceFree(d_ptr)` 是安全的（被空指针保护拦住）。

**预期结果**：与 `reMalloc` 不同，这里**没有任何复用**——连续两次 `deviceMalloc` 同样大小，会触发两次 `cudaMalloc`；这正是它只适合「冷路径」的原因。

#### 4.5.5 小练习与答案

**练习**：`deviceMalloc` 的第二个参数 `size` 是「元素个数」还是「字节数」？内部怎么换算？

**参考答案**：是**元素个数**。内部按 `sizeof(T) * size` 换算成字节数后再调 `cudaMalloc`（见 [memory_utils.cu:32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu#L32)）。这与 `reMalloc(ptr, sizeof(T)*n)` 在调用点的写法不同——`reMalloc` 接收字节数，所以调用方要自己乘 `sizeof(T)`，使用时要留意别搞混。

---

## 5. 综合实践

**任务**：为 `IAllocator` 画一张「显存生命周期图」，把本讲全部知识点串起来。

请按以下步骤完成（源码阅读 + 手绘流程，不需要改任何源码）：

1. **找入口**：在 `examples/cpp/bert/bert_example.cc` 里定位 `Allocator<AllocatorType::CUDA> allocator(getDevice());`（[L87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L87)），它在构造时建立了内存池并把释放阈值设为 `UINT64_MAX`。
2. **找热路径调用**：在 `src/fastertransformer/models/t5/T5Decoding.cc`（或 `bert/Bert.cc`）里任选一处 `allocator_->reMalloc(buf_, sizeof(T)*n, false)`（如 [T5Decoding.cc:94](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L94)）。
3. **找冷路径调用**：在 `src/fastertransformer/utils/memory_utils.cu` 里定位 `loadWeightFromBinFunc` 中的 `deviceMalloc(&ptr_2, host_array.size(), false)`（[L357](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/memory_utils.cu#L357)）。
4. **手绘一张图**，把以下时间线画出来：
   - 启动期：构造 `Allocator` → 建池 → `deviceMalloc` 加载权重（一次性）；
   - 推理期：第 1 次 forward 的 `reMalloc`（`INCREASE`/新分配，池里切内存）；
   - 稳态期：第 2…N 次 forward 的 `reMalloc`（`REUSE`，零 malloc）；
   - 形状变化：batch 变大时的 `reMalloc`（`INCREASE`，池内扩容）；
   - 析构期：`Allocator` 析构 → 把账本里剩余 buffer 全 `free` 回池。
5. 在图上标注：哪一步真正调用了 `cudaMalloc`/`cudaMallocAsync`，哪一步只命中账本、完全没碰分配 API。

**验收标准**：你能对着图向别人讲清楚——「为什么稳态推理时 FT 几乎不再调用 `cudaMalloc`」。这正是显存管理对推理加速的核心贡献之一。

---

## 6. 本讲小结

- FT 有**两条**显存路径：`IAllocator`（带账本、带池、可复用，服务热路径工作区）与 `memory_utils::deviceMalloc/deviceFree`（一次性 `cudaMalloc` 薄封装，服务权重等冷路径对象）。
- `IAllocator` 用纯虚接口 + 模板特化提供 `CUDA / TF / TH` 三种后端；CUDA 后端自己管显存，TF/TH 后端把分配委托给框架。
- `reMalloc` 是复用核心：靠 `pointer_mapping_`（地址→字节数）账本，把请求分为 `INCREASE / REUSE / DECREASE`，稳态下命中 `REUSE` 完全跳过 `cudaMalloc`。
- 调用 `reMalloc` 必须**写回**返回值（`buf_ = (T*)alloc.reMalloc(buf_, ...)`），因为扩容会返回新地址。
- CUDA 11.2 的异步内存池（`cudaMallocAsync`/`cudaFreeAsync`）让 free+malloc 退化为池内指针搬运；编译期宏 `CUDA_MEMORY_POOL_DISABLED` 控制是否启用，并影响 `DECREASE` 分支是否存在。
- `deviceMalloc` 接收「元素个数」并在内部乘 `sizeof(T)`；`reMalloc` 接收「字节数」由调用方自己乘 `sizeof(T)`——两者参数语义不同，使用时需留意。

---

## 7. 下一步学习建议

- 有了 allocator，下一步自然是看它怎么被 `BaseLayer` 持有和驱动。建议进入 **u3-l5（BaseLayer 架构：buffer 生命周期管理）**，看 `allocateBuffer/freeBuffer` 如何把 `IAllocator` 封装成 layer 级的 buffer 生命周期约定。
- 如果你对「矩阵乘怎么用这块 buffer」更感兴趣，可以先跳到 **u2-l3（cublasMMWrapper 与 GEMM）**，看 GEMM 的工作区如何从 allocator 切出来。
- 想验证本讲的行为，可以结合 **u1-l5（日志、调试与环境变量）** 的 `FT_LOG_LEVEL=DEBUG` 与 NVTX，观察 `reMalloc` 各分支的命中情况。
