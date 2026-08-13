# Context 资源池管理

## 1. 本讲目标

在 [u1-l5](u1-l5-context.md) 里我们建立了「Context 是一组 Operation 共享的运行时环境」的心智模型，在 [u3-l2](u3-l2-runner-system.md) 里又看到 Operation 把执行后端交给了 Runner。但有一个问题一直被悬置：**这些跨算子、跨多次执行的可复用资源到底由谁持有、怎么分配、怎么回收？**

本讲就下沉到 `ContextBase` 的内部实现，拆解它持有的三个核心资源池：

1. **TilingBufferPool**：管理 Host/Device 两块「环形 tiling 缓冲」，承载算子切分参数的 Host→Device 流转。
2. **Allocator**：统一的内存分配抽象，及其默认实现如何对齐、记账与兜底释放。
3. **RunnerPool**：按 Runner 类型分桶的对象复用池，让昂贵的 Runner 对象（含 KernelGraph、缓存）能被反复借用而非重建。

学完本讲，你应当能够：

- 说清 Tiling 为什么需要 Host 与 Device 两个池，以及它们是怎么被算子消费的；
- 解释 Allocator 抽象的作用、`DefaultDeviceAllocator` 的 32 字节对齐与 `memMap` 记账机制；
- 描述 RunnerPool「借出—归还—复用」的协作过程，以及 `REG_RUNNER_TYPE` 如何把字符串名映射成池下标；
- 知道 `CreateContext` 的三个重载分别如何影响上述池子。

## 2. 前置知识

- **Tiling 是什么**：在昇腾算子里，Host（CPU）需要先把输入形状、切分策略等「调度参数」算好，打包成一块连续内存（TilingData），再拷到 Device（NPU），Kernel 才能据此决定核间/核内怎么干活。这块内存就叫 tiling buffer。详见 [u3-l4](u3-l4-kernel-mki.md)。
- **Host Bound 与两段式执行**：Setup 在 Host 做（含 Tiling），Execute 才异步下发到 Device。Host 慢于 Device 就会卡住流水，所以 ATB 大量使用「预分配 + 复用」来减少每次 malloc 的开销。详见 [u1-l1 项目定位](u1-l1-project-overview.md)。
- **RunnerVariantPack**：Runner 执行时使用的「厚集装箱」，除了用户输入输出，还携带 host/device tiling 指针、workspace、context 等。详见 [u3-l2](u3-l2-runner-system.md)。
- **ACL 内存接口**：`aclrtMalloc`/`aclrtFree` 申请释放 Device 显存，`aclrtMallocHost`/`aclrtFreeHost` 申请释放 Host（页锁定）内存。本讲的默认 Allocator 就是它们的薄封装。

一句话先建立直觉：**ContextBase 是一个「资源管家」，它把 tiling 缓冲、内存分配器、Runner 对象三类昂贵资源提前备好、循环复用，让每次 Setup/Execute 只做「借一块、用完还回去」的轻动作，从而把 malloc 和重建的开销摊薄到接近零。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/atb/context/context_base.h` | `ContextBase` 类声明，列出它持有的全部资源成员（两个 tiling 池、runnerPools、两个 allocator）。 |
| `src/atb/context/context_base.cpp` | `ContextBase::Init/Destroy`、`GetHostTilingBuffer/GetDeviceTilingBuffer`、`GetRunnerPool`、args 缓冲等实现。 |
| `src/atb/context/context.cpp` | `CreateContext` 三个重载与 `DestroyContext`，是用户调节池子参数的入口。 |
| `src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h` / `.cpp` | `TilingBufferPool` 抽象基类：环形块管理与 `Init/Destroy/GetBuffer`。 |
| `src/atb/context/tiling_buffer_pool/host_tiling_buffer_pool.*` | Host 子类，用 `malloc/free`。 |
| `src/atb/context/tiling_buffer_pool/device_tiling_buffer_pool.*` | Device 子类，用 `aclrtMalloc/aclrtFree` 或用户自定义分配函数。 |
| `src/atb/context/allocator/allocator.h` | `Allocator` 纯虚抽象（`Allocate/Deallocate`）。 |
| `src/atb/context/allocator/default_device_allocator.*` / `default_host_allocator.*` | 默认分配器：32 字节对齐、`memMap` 记账、析构兜底释放。 |
| `src/atb/context/runner_pool.h` / `.cpp` | `RunnerPool`：按槽位借还 Runner 的对象池。 |
| `src/atb/utils/operation_register.h` | `RunnerTypeRegister` 与 `REG_RUNNER_TYPE` 宏，把 Runner 类型名映射成下标。 |

## 4. 核心概念与源码讲解

### 4.1 TilingBufferPool：Host/Device 双池的环形缓冲

#### 4.1.1 概念说明

Tiling 的生命周期横跨 Host 和 Device 两端：

1. Setup 阶段，Runner 在 **Host** 上算出 TilingData，写进一块 Host 内存；
2. 这块 Host 内存需要 **拷贝** 到 Device；
3. Execute 阶段，Kernel 从 **Device** 内存里读 TilingData。

这就要求 Context 同时维护「Host 侧缓冲」和「Device 侧缓冲」两类内存。如果每次 Setup 都 `malloc` 一块、Execute 后 `free`，会引入大量小内存分配，而 `aclrtMalloc` 这类 Device 分配本身就不便宜。`TilingBufferPool` 的做法是**一次性申请一大块连续内存，切成等大的 N 个 block，按环形（ring）依次发放**，用完一圈再绕回来——既免了反复 malloc，又允许同一时刻有多个在途（in-flight）的算子各拿一块、互不覆盖。

`TilingBufferPool` 是个抽象基类，把「怎么申请/释放底层大内存」下放给子类，自己只管「切 block + 环形发放」这件与位置无关的事。这正是典型的**模板方法模式**。

#### 4.1.2 核心流程

一个池的内部状态非常简单：

```
totalBuffer_  ──┐
                ├── 一次性 MallocTotalBuffer(blockNum * blockSize) 得到的大内存
totalSize_   ──┘
blockIndex_     当前发到第几块（0 ~ blockNum-1），发完归零
```

`GetBuffer()` 的发放逻辑：

```
nextBuffer = totalBuffer_ + blockSize * blockIndex_   // 算出这一块的首地址
blockIndex_++
if blockIndex_ == blockNum: blockIndex_ = 0          // 绕回起点，环形
return nextBuffer
```

这就是一个**无锁环形分配器**：调用者拿到一块就负责用完，池子不追踪「这块是否还在用」，靠 `blockNum` 足够大来保证绕回时旧数据已被消费。

两个子类的区别只在底层大内存的来源：

| 子类 | `MallocTotalBuffer` | `FreeTotalBuffer` | `IsDeviceBufferPool` |
| --- | --- | --- | --- |
| `HostTilingBufferPool` | `malloc` | `free` | `false` |
| `DeviceTilingBufferPool` | `aclrtMalloc` 或用户 `allocateFunc_` | `aclrtFree` 或用户 `deallocateFunc_` | `true` |

`IsDeviceBufferPool()` 仅用于在 `Init` 失败时返回更准确的错误码（`ERROR_OUT_OF_DEVICE_MEMORY` vs `ERROR_OUT_OF_HOST_MEMORY`）。

#### 4.1.3 源码精读

先看基类成员与接口，注意三个纯虚钩子把内存来源下放给子类：

[tiling_buffer_pool.h:16-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.h#L16-L39) —— `TilingBufferPool` 抽象基类，`GetBuffer()` 是对外的取块接口，`MallocTotalBuffer/FreeTotalBuffer/IsDeviceBufferPool` 是子类必须实现的三个钩子；私有成员 `totalBuffer_/totalSize_/blockIndex_` 构成环形状态。

`Init` 把 `blockNum * blockSize` 一次性申请下来：

[tiling_buffer_pool.cpp:20-40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.cpp#L20-L40) —— 第 29 行 `totalSize = blockNum_ * blockSize_`，第 32 行 `MallocTotalBuffer(totalSize)` 由子类决定走 `malloc` 还是 `aclrtMalloc`；失败时第 38 行按 `IsDeviceBufferPool()` 返回不同错误码。

环形发放的核心只有 5 行：

[tiling_buffer_pool.cpp:51-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.cpp#L51-L61) —— 第 53 行用 `blockSize_ * blockIndex_` 偏移定位下一块；第 56-58 行 `blockIndex_` 到顶归零，实现环形绕回。

两个子类的差异点很清晰。Host 子类用标准 C 函数：

[host_tiling_buffer_pool.cpp:21-36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/host_tiling_buffer_pool.cpp#L21-L36) —— `MallocTotalBuffer` 直接 `malloc(bufferSize)`，`IsDeviceBufferPool()` 返回 `false`。

Device 子类多了一个「自定义分配函数」分支，这是 `CreateContext` 自定义 alloc/dealloc 的落点：

[device_tiling_buffer_pool.cpp:21-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/device_tiling_buffer_pool.cpp#L21-L54) —— 第 23 行 `if (!allocateFunc_)` 时走默认 `aclrtMalloc(..., ACL_MEM_MALLOC_HUGE_FIRST)`（优先用大页）；否则第 31 行调用用户传入的 `allocateFunc_`。释放同理（第 38/46 行）。

池子在 `ContextBase` 里是这样被创建和编号的：

[context_base.cpp:51-84](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L51-L84) —— 第 56 行用 `hostTilingBlockNum` 与常量 `TILING_BUFFER_BLOCK_SIZE` 建 Host 池；第 65-74 行决定是否使用用户自定义分配函数；第 75-76 行用 `deviceTilingBlockNum` 建 Device 池。每个 block 的大小是常量：

[context_base.cpp:26-28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L26-L28) —— `TILING_BUFFER_BLOCK_SIZE = 1024 * 1024 * 3`，即每块 3 MB。

那这两个池是怎么被算子消费的？答案在 `OperationBase` 里。Setup 时借 Host 块、Execute 时借 Device 块：

[operation_base.cpp:537-543](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L537-L543) —— Setup 阶段 `hostTilingBuffer_ = context->GetHostTilingBuffer()` 从 Host 池借一块，Runner 把 TilingData 写进去。

[operation_base.cpp:1284-1288](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1284-L1288) —— Execute 阶段 `GetDeviceTilingBuffer()` 从 Device 池借一块，Host tiling 拷贝过来供 Kernel 读取。

而 `ContextBase::GetHostTilingBuffer/GetDeviceTilingBuffer` 还藏着一个**图模式分支**——整图下发时绕开池子，改用 Allocator 现场申请：

[context_base.cpp:173-191](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L173-L191) —— `mode_ == GRAPH_LAUNCH_MODE` 时直接 `hostAllocator_->Allocate(...)`/`deviceAllocator_->Allocate(...)`；否则走池子的 `GetBuffer()`。原因在 [u1-l5](u1-l5-context.md) 提过：整图模式靠 `aclmdlRICapture` 录制整张图，tiling 地址必须在整个录制期稳定存在，环形池会被绕回覆盖，所以改用「按需分配、生命周期由 Allocator 托管」的方式。

#### 4.1.4 代码实践

**实践目标**：在源码层面把「Tiling 为何要分 Host/Device 两个池」讲清楚，并验证 `CreateContext` 参数对池大小的影响。

**操作步骤（源码阅读型）**：

1. 打开 `src/atb/context/tiling_buffer_pool/tiling_buffer_pool.cpp`，对照 [L51-L61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/tiling_buffer_pool.cpp#L51-L61) 的 `GetBuffer`，确认它是环形绕回（无空闲链表、无引用计数）。
2. 打开 `src/atb/operation/operation_base.cpp` [L537](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L537) 与 [L1284](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1284)，确认 Host 块在 Setup 借、Device 块在 Execute 借。
3. 打开 `src/atb/context/context.cpp` [L66-L99](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L66-L99)，记录两个块数参数的合法区间。

**需要观察的现象 / 预期结论**：

- **为什么分两个池**：Tiling 先在 Host 生成（Setup），再拷到 Device 供 Kernel 读（Execute）。Host 与 Device 是两段物理上不同的内存，必须各有一块；又因为 Host→Device 拷贝是异步的，Host 块和 Device 块在同一时刻可能承载不同的在途算子，所以各自做成多块环形池，而非单块。
- **CreateContext 参数如何影响**：第三个重载 `CreateContext(ctx, hostTilingBlockNum, deviceTilingBlockNum)` 直接决定环形池的块数（`blockNum_`），每块固定 3 MB。Host 块数合法区间 128–1024、Device 块数 32–1024；越大的块数意味着同一时刻能容纳越多在途算子的 tiling，代价是占用更多内存（`blockNum × 3 MB`）。
- **自定义 alloc/dealloc 的落点**：第二个重载传入的分配函数**只**作用于 `DeviceTilingBufferPool` 的底层大内存（见 [device_tiling_buffer_pool.cpp:23-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/tiling_buffer_pool/device_tiling_buffer_pool.cpp#L23-L31)），Host 池与一般 Allocator 不受影响。

> 运行结果：本实践为源码阅读型，无需在 NPU 上运行；如需观察实际借还行为，可在 `GetBuffer` 入口加一行 `ATB_LOG(INFO)` 打印 `blockIndex_`，配合日志级别 INFO 在真实环境验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `hostTilingBlockNum` 设成 64，调用 `CreateContext(ctx, 64, 32)` 会发生什么？
**答案**：会失败。`context.cpp` 第 73-76 行校验 `hostTilingBlockNum` 必须 ∈ [128, 1024]，64 < 128，直接返回 `ERROR_INVALID_PARAM`，Context 不会被创建。

**练习 2**：`GetBuffer()` 没有任何锁，会不会出现两个算子拿到同一块？
**答案**：有可能「逻辑上」拿到同一块（当一个算子持有块超过一圈、`blockIndex_` 绕回时），但设计上靠 `blockNum` 足够大、且 Setup→Execute→Device 消费的时间远小于绕回一圈的时间来规避。它不是并发安全意义上的互斥，而是「足够深的环形缓冲」摊掉冲突。这也是为什么默认 Host 块数（128）比 Device 块数（32）大——Host 侧借块到拷贝完成的窗口更长。

---

### 4.2 Allocator：统一的内存分配抽象

#### 4.2.1 概念说明

`Allocator` 是一个非常薄的两函数抽象：`Allocate(size)` 与 `Deallocate(addr)`。它的意义在于**把「谁去申请/释放内存」变成可替换的策略**。默认实现用 ACL 的 `aclrtMalloc/aclrtFree`（Device）和 `aclrtMallocHost/aclrtFreeHost`（Host），但用户理论上可以塞进自己的缓存层、内存池或观测工具。

在 `ContextBase` 里，Allocator 服务于两类需求：

1. **Kernel args 缓冲**：`GetArgsDeviceBuffer/GetArgsHostBuffer` 申请小块内存承载 Kernel 参数；
2. **图模式下的 tiling 缓冲**：整图下发时（见 4.1.3）绕开 TilingBufferPool，改用 Allocator 现场申请。

注意一个容易混淆的点：`CreateContext` 第二个重载传入的自定义 alloc/dealloc **不会**替换 `deviceAllocator_`，它只作用于 `DeviceTilingBufferPool`。`ContextBase` 内部的 `deviceAllocator_`/`hostAllocator_` 始终是 `DefaultDeviceAllocator`/`DefaultHostAllocator`（在构造函数里写死）。

#### 4.2.2 核心流程

`DefaultDeviceAllocator` 的关键设计有三点：

1. **32 字节对齐**：申请前先用 `TensorUtil::AlignInt(bufferSize, 32)` 向上取整到 32 的倍数（常量 `ALIGN_INT = 32`）。这是 NPU DMA 搬运与某些 Kernel 对齐要求的需要。
2. **记账**：用一个 `std::map<void*, size_t> memMap` 记录「这块地址 ↔ 实际字节数」，并维护 `currentAllocateSize_` 累计已分配量。释放时先在 map 里查到对应大小，再 `aclrtFree`，并扣减累计量。
3. **析构兜底**：`Deallocate` 是「主动释放」，但如果调用方忘了释放，析构函数会遍历 `memMap` 把残留地址全部 `aclrtFree`，防止 Device 显存泄漏——这是「RAII 兜底」。

流程伪代码：

```
Allocate(size):
    size = AlignInt(size, 32)            # 向上对齐到 32 字节
    addr = aclrtMalloc(size, HUGE_FIRST) # 优先大页
    memMap[addr] = size
    currentAllocateSize_ += size
    return addr

Deallocate(addr):
    if addr == nullptr: return NO_ERROR
    it = memMap.find(addr)
    if not found: return ERROR            # 不是本分配器发的，拒绝
    aclrtFree(addr)
    currentAllocateSize_ -= it.size
    memMap.erase(addr)

~DefaultDeviceAllocator():
    for (addr, size) in memMap:           # 兜底：释放所有未主动归还的
        aclrtFree(addr)
```

`DefaultHostAllocator` 与之完全对称，只把 `aclrtMalloc/aclrtFree` 换成 `aclrtMallocHost/aclrtFreeHost`。

#### 4.2.3 源码精读

抽象基类只有两个纯虚函数：

[allocator.h:15-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/allocator/allocator.h#L15-L22) —— `Allocator` 抽象，`Allocate` 返回 `void*`，`Deallocate` 返回 `Status`。

默认 Device 分配器的成员：一个 `memMap` 加一个累计计数：

[default_device_allocator.h:17-26](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/allocator/default_device_allocator.h#L17-L26) —— `std::map<void*, size_t> memMap` 与 `currentAllocateSize_`。

`Allocate` 的对齐与记账：

[default_device_allocator.cpp:36-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/allocator/default_device_allocator.cpp#L36-L61) —— 第 43 行 `AlignInt(bufferSize, ALIGN_INT)` 向上对齐；第 46 行 `aclrtMalloc(..., ACL_MEM_MALLOC_HUGE_FIRST)`；第 51-52 行更新累计量并写入 `memMap`。第 38-41 行对 0 字节请求告警并返回空。

`Deallocate` 的查表—释放—抹项：

[default_device_allocator.cpp:63-90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/allocator/default_device_allocator.cpp#L63-L90) —— 第 69-73 行在 `memMap` 里找不到就报错（防止误释放非本分配器申请的地址）；第 75 行 `aclrtFree`；第 88 行 `memMap.erase`。

析构兜底，避免显存泄漏：

[default_device_allocator.cpp:16-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/allocator/default_device_allocator.cpp#L16-L34) —— 遍历 `memMap`，对每个残留地址 `aclrtFree`，并在 `_DEBUG` 下打印地址与大小。

`ContextBase` 在构造时就把两个默认分配器写死，并通过 args 缓冲对外暴露：

[context_base.cpp:31-35](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L31-L35) —— 构造函数把 `deviceAllocator_`/`hostAllocator_` 设为默认实现。

[context_base.cpp:333-351](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L333-L351) —— `GetArgsDeviceBuffer/FreeArgsDeviceBuffer/GetArgsHostBuffer/FreeArgsHostBuffer` 全是 Allocator 的薄转发。这就是「Allocator 服务于 args 缓冲」的入口。

成员声明一览（注意 `allocateFunc_/deallocateFunc_` 与 `deviceAllocator_` 是两回事）：

[context_base.h:63-72](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L63-L72) —— `hostTilingBufferPool_`/`deviceTilingBufferPool_`/`runnerPools_` 是三大池；`deviceAllocator_`/`hostAllocator_` 是通用分配器；`allocateFunc_`/`deallocateFunc_` 只喂给 DeviceTilingBufferPool。

#### 4.2.4 代码实践

**实践目标**：验证「自定义 alloc/dealloc 不影响 deviceAllocator_」这一结论。

**操作步骤（源码阅读型）**：

1. 读 `src/atb/context/context.cpp` [L41-L64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context.cpp#L41-L64) 的第二个 `CreateContext` 重载，确认它把 `alloc/dealloc` 透传给 `Init(alloc, dealloc, ...)`。
2. 回到 `context_base.cpp` [L65-L76](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L65-L76)，确认 `alloc/dealloc` 只被赋给 `allocateFunc_/deallocateFunc_`，并最终传给 `DeviceTilingBufferPool`。
3. 检查 `deviceAllocator_` 的赋值点（构造函数 [L31-L35](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L31-L35)），确认它**始终**是 `DefaultDeviceAllocator`，没有任何分支替换它。

**预期结论**：

- `deviceAllocator_` 永远是 `DefaultDeviceAllocator`，它服务于 args 缓冲与图模式 tiling。
- 用户自定义的 `alloc/dealloc` 只会改写 Device tiling 池的底层大内存来源。
- 因此即便用户传入了「带缓存」的自定义分配器，args 缓冲仍然走默认 `aclrtMalloc`。

> 运行结果：源码阅读型，结论可直接从静态分析得出（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Deallocate` 要先在 `memMap` 里 `find`，找不到就报错，而不是无条件 `aclrtFree`？
**答案**：为了**所有权校验**。`aclrtFree` 一个不是由 `aclrtMalloc` 申请的（或已经释放过的）地址是未定义行为，可能 core。`memMap` 是分配器的「账本」，只允许释放自己签发的地址，相当于一道安全栅栏。同时它需要从账本里查出这块地址对应的字节数，才能正确扣减 `currentAllocateSize_`。

**练习 2**：`DefaultDeviceAllocator` 和 `DefaultHostAllocator` 的代码几乎一模一样，它们唯一的实质区别是什么？
**答案**：底层 ACL 接口不同——Device 用 `aclrtMalloc/aclrtFree`（申请 Device 显存），Host 用 `aclrtMallocHost/aclrtFreeHost`（申请页锁定 Host 内存，便于高效 H2D/D2H 拷贝）。其余对齐、记账、析构兜底逻辑完全对称。

---

### 4.3 RunnerPool：Runner 对象的复用池

#### 4.3.1 概念说明

回顾 [u3-l2](u3-l2-runner-system.md)：一个 Runner 往往内部维护一张 `KernelGraph`、各类缓存（如 `OpsRunner` 的 `g_globalKernelCaches`、`AclnnRunner` 的 executor 缓存），构造代价不低。如果每执行一次算子就新建一个 Runner、执行完销毁，反复重建 KernelGraph 会成为 Host 侧的显著开销。

`RunnerPool` 解决的就是这个问题：**把用完的 Runner 对象保留下来，下次同类型算子再来时直接「换参数复用」，而不是重新构造。** 它的做法是「**借出—归还**」的对象池（object pool）：

- 借出（`MallocRunner`）：在池里找一个空闲槽位，标记为占用；若槽位里已有 Runner，就调 `SetParam` 换上新参数复用，否则才 `new` 一个新的；
- 归还（`FreeRunner`）：把槽位标记为空闲，**不删除**对象，留给下一次借用。

为了按 Runner 类型分桶（SelfAttention 的池不混着 Linear 用），还需要一个「类型名 → 池下标」的登记表，这就是 `RunnerTypeRegister` 与 `REG_RUNNER_TYPE` 宏的职责。

#### 4.3.2 核心流程

**类型登记（静态初始化期）**：每个 Runner 类型在源码里写一行 `REG_RUNNER_TYPE(XxxRunner)`，宏展开后在静态初始化时把字符串名 `"XxxRunner"` 插入全局 `runnerTypeMap`，并分配一个递增下标 `0,1,2,...`。

**Context 初始化期**：`ContextBase::Init` 调 `runnerPools_.resize(RunnerTypeRegister::GetRunnerTypeMapSize())`，即「每种已登记的 Runner 类型一个 `RunnerPool`」，每个 `RunnerPool` 默认 64 个槽位（`DEFAULT_RUNNER_POOL_SIZE = 64`）。

**算子借 Runner（CreateRunner 里）**：

```
idx = RunnerTypeRegister::GetRunnerTypeIdx("SelfAttentionFusionOpsRunner")
pool = contextBase->GetRunnerPool(idx)
runner = pool.MallocRunner<SelfAttentionFusionOpsRunner, Param>(param)
return runner
       ? shared_ptr<Runner>(runner, [pool](Runner* r){ pool.FreeRunner(r); })  # 自定义删除器=归还
       : make_shared<SelfAttentionFusionOpsRunner>(param)                      # 池满兜底
```

`MallocRunner` 内部（加锁）：

```
for each slot in poolItems_:
    if not slot.isUsed:
        slot.isUsed = true
        if slot.runner exists:
            slot.runner->SetParam(param)   # 复用：只换参数
        else:
            slot.runner = new RunnerClass(param)  # 首次：新建
        return slot.runner.get()
return nullptr  # 64 个槽全满
```

**归还**：`shared_ptr` 引用计数归零时触发自定义删除器，调用 `FreeRunner(runner)`，把对应槽位 `isUsed = false`。对象本身留在池里等下次复用。

#### 4.3.3 源码精读

类型登记表与宏：

[operation_register.h:17-65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h#L17-L65) —— 第 19-28 行构造函数用 `emplace(runnerType, idx++)` 登记类型名，重名会回退下标；第 42-55 行 `GetRunnerTypeIdx` 按名查下标；第 57-61 行 `GetRunnerTypeMapSize` 返回已登记类型数；第 64-65 行 `REG_RUNNER_TYPE` 宏生成一个静态 `RunnerTypeRegister` 实例触发登记。

池子大小由登记表决定：

[context_base.cpp:86-86](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L86) —— `runnerPools_.resize(RunnerTypeRegister::GetRunnerTypeMapSize())`，一类 Runner 一个池。

[context_base.h:40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L40) 与 [context_base.cpp:245-248](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L245-L248) —— `GetRunnerPool(idx)` 直接按下标从 `runnerPools_` 取池。

`MallocRunner` 模板的借出逻辑（注意它是模板，写在头文件里）：

[runner_pool.h:32-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L32-L54) —— 第 38 行 `std::lock_guard` 加锁保证线程安全；第 39-51 行遍历找空闲槽；第 43-45 行复用分支调 `SetRunnerParam`（即 `runner->SetParam`）；第 47-49 行首次分支 `make_shared<RunnerClass>(param)` 新建；第 53 行全满返回 `nullptr`。

`FreeRunner` 只翻标志位，不删对象：

[runner_pool.cpp:24-37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.cpp#L24-L37) —— 找到指针相等的槽位，置 `isUsed = false`，break。对象 `poolItem.runner`（`shared_ptr<Runner>`）继续持有，等下次复用。

池的默认容量与槽位结构：

[runner_pool.cpp:15-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.cpp#L15-L22) —— `DEFAULT_RUNNER_POOL_SIZE = 64`，构造时 `poolItems_.resize(64)`。

[runner_pool.h:21-24](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L21-L24) —— `PoolItem { bool isUsed; shared_ptr<Runner> runner; }`，`isUsed` 即「借出中」标记。

`SetParam` 的虚函数语义——基类默认空操作，由子类决定是否需要「换参数后重置内部状态」：

[runner.h:32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.h#L32) 与 [runner.cpp:269-272](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L269-L272) —— 基类 `Runner::SetParam` 是 `(void)param;` 空实现，复用时若子类不重写就等于「沿用旧 param」。

最后看一个真实算子是怎么用池的——`SelfAttention` 在 910A 芯片下借 Runner：

[self_attention_operation.cpp:2113-2140](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2113-L2140) —— 先 `GetRunnerTypeIdx("SelfAttentionEncoderFusionOpsRunner910A")` 取下标，`GetRunnerPool(idx)` 取池，`MallocRunner<...>(param)` 借；借到就用 `shared_ptr` 包起来、删除器绑 `FreeRunner`（归还）；借不到（返回空）就 `make_shared<...>(param)` 现建一个兜底。不同分支按 `calcType/inputLayout/kvcacheCfg` 选不同类型的 Runner，对应不同下标、不同池。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 Runner 的「借出—归还—复用」，量化复用带来的收益。

**操作步骤（源码阅读型 + 日志观测）**：

1. 在 `src/atb/context/runner_pool.h` [L44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L44) 与 [L48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/runner_pool.h#L48) 已有现成日志：复用走 `"Get pool old runner!"`，首次走 `"Pool create new runner!"`。
2. 在真实环境（昇腾 NPU）把 `ATB_LOG` 级别设为 INFO，连续执行同一个 SelfAttention 算子多次（例如 10 次）。
3. 数日志里 `Pool create new runner!` 与 `Get pool old runner!` 的次数。

**需要观察的现象 / 预期结果**：

- 第 1 次执行：打印 `Pool create new runner!`（首次构造，建 KernelGraph）。
- 第 2～10 次：打印 `Get pool old runner!`（复用，仅 `SetParam` 换参数，不重建 KernelGraph）。
- 由此可直观看到：池化把「构造 1 次 + 复用 N 次」摊薄，避免了 N 次重建。

> 运行结果：日志观测需要在真实昇腾环境运行，本讲无法代跑；静态结论是「首次新建、后续复用」（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MallocRunner` 返回的是裸指针 `Runner*`，而调用方立刻用 `shared_ptr` 包起来？
**答案**：因为「归还」语义不能是默认的 `delete`。`shared_ptr<Runner>(runner, deleter)` 的自定义删除器绑成 `pool.FreeRunner(runner)`，于是当 `shared_ptr` 引用计数归零（算子本次执行结束）时，触发的是「标记槽位空闲」而非「析构 Runner」，对象就此留在池里等下次复用。裸指针只是池内部所有权与外部 `shared_ptr` 生命周期之间的过渡。

**练习 2**：如果同一时刻有 65 个同类型算子在途（超过 64 槽），会发生什么？
**答案**：第 65 次 `MallocRunner` 遍历 64 个槽全是 `isUsed`，返回 `nullptr`。调用方走到兜底分支 `make_shared<RunnerClass>(param)` 现建一个**非池内**的 Runner，它的 `shared_ptr` 用默认删除器，引用归零时直接析构、不归还。所以池满不会报错，只是这一次借用退化成「用完即毁」。

**练习 3**：`RunnerTypeRegister::GetRunnerTypeIdx` 是按字符串名查表，如果拼错了名字会怎样？
**答案**：[operation_register.h:50-53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/operation_register.h#L50-L53) 里找不到该名会返回 `-1`，并打印 `Can not find the runnerTypeIdx by runner name`。随后 `GetRunnerPool(-1)` 会对 `runnerPools_` 越界访问（`.at(-1)` 转 `size_t` 后是极大值），抛 `std::out_of_range`。这也是为什么 `REG_RUNNER_TYPE` 的名字必须与 `GetRunnerTypeIdx` 查询的字符串完全一致。

---

## 5. 综合实践

**任务**：画一张「Context 资源全景图」，把三类资源在一次算子 Setup→Execute 中的协作串起来。

请按下列步骤完成（纯源码阅读 + 画图）：

1. **列出 Context 的家当**：对照 [context_base.h:59-72](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.h#L59-L72)，在图上画出 `executeStreams_`、`hostTilingBufferPool_`、`deviceTilingBufferPool_`、`runnerPools_`、`deviceAllocator_`、`hostAllocator_`、`asyncTilingCopyStream_/events_` 这些成员。

2. **标出 Setup 阶段的资源流**：
   - `OperationBase::Setup` 经 [L537](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L537) 从 **Host 池** 借一块写 Tiling；
   - `CreateRunner` 经 [self_attention_operation.cpp:2113-2118](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/self_attention/self_attention_operation.cpp#L2113-L2118) 从 **RunnerPool** 借一个 Runner（首次新建/后续复用）。

3. **标出 Execute 阶段的资源流**：
   - 从 **Device 池** 借一块（[operation_base.cpp:1284](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/operation_base.cpp#L1284)），把 Host tiling 拷过去；
   - Runner 经 `executeStreams_` 对应的流下发 Kernel；

4. **标出图模式（GRAPH_LAUNCH_MODE）的不同路径**：tiling 缓冲改走 **Allocator** 而非池（[context_base.cpp:173-191](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/context/context_base.cpp#L173-L191)），用不同颜色区分。

5. **写出 3 条结论**，至少覆盖：① 为什么 Host/Device 要两个池；② Allocator 与 TilingBufferPool 的分工；③ RunnerPool 复用比新建省在哪里。

> 这张图直接服务于下一讲 [u7-l1 Tiling 调度与多流执行](u7-l1-tiling-multistream.md)，那里会展开 `asyncTilingCopyStream_` 与多流如何与这些池协作。

## 6. 本讲小结

- **ContextBase 是资源管家**：它持有两个 TilingBufferPool、一组 RunnerPool、两个 Allocator，全部提前备好、循环复用，把 malloc 与重建开销摊薄。
- **TilingBufferPool 是无锁环形缓冲**：一次性申请 `blockNum × blockSize`（每块 3 MB），`GetBuffer` 按 `blockIndex` 依次发块、绕回；Host 子类用 `malloc/free`，Device 子类用 `aclrtMalloc/aclrtFree` 或用户自定义函数。
- **为什么分 Host/Device 两个池**：Tiling 先在 Host 生成（Setup）再拷到 Device（Execute），两端是不同物理内存，且同一时刻可能承载不同在途算子，故各自做成多块环形池。
- **Allocator 是两函数抽象**：`DefaultDeviceAllocator/DefaultHostAllocator` 做 32 字节对齐、`memMap` 记账、析构兜底释放；服务于 args 缓冲与图模式 tiling；`CreateContext` 的自定义 alloc/dealloc 只改写 Device tiling 池，不替换 `deviceAllocator_`。
- **RunnerPool 是按类型分桶的对象池**：`REG_RUNNER_TYPE` 登记类型名→下标，Context 按下标为每类 Runner 建一个 64 槽的池；`MallocRunner` 复用（`SetParam`）优先于新建，`FreeRunner` 只翻标志位不删对象，靠 `shared_ptr` 自定义删除器触发归还。
- **图模式会切换 tiling 来源**：`GRAPH_LAUNCH_MODE` 下 tiling 缓冲绕开池、改用 Allocator 现场申请，以适配整图录制对地址稳定性的要求。

## 7. 下一步学习建议

- **紧接本讲**：阅读 [u7-l1 Tiling 调度与多流执行](u7-l1-tiling-multistream.md)，看 `asyncTilingCopyStream_`、`asyncTilingCopyEvents_`（本讲只点到为止）如何让 tiling 的 H2D 拷贝与 Kernel 计算重叠，进一步压榨 Host Bound。
- **回到调用层**：对照 [u2-l1 C++ 单算子 Demo](u2-l1-cpp-op-demo.md) 的 `CreateContext` 调用，体会本讲的池子在「初始化一次、复用无数次」中的位置。
- **延伸阅读源码**：`src/atb/operation/operation_base.cpp` 的 `Setup/Execute/CopyTilingToDevice/UpdateTensorData` 是 tiling 借还的「消费方」，通读它能把本讲与 [u3-l2 Runner 体系](u3-l2-runner-system.md) 完整缝合。
- **调试技巧预告**：所有池的关键动作都有 `ATB_LOG(INFO)` 日志（`Pool create new runner!`、`malloc bufferSize:` 等），在 [u7-l2 日志与性能 Profiling](u7-l2-logging-profiling.md) 会系统讲如何打开并利用这些日志观测池行为。
