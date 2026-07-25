# Allocator 与 BFCAllocator 内存管理

## 1. 本讲目标

本讲是「设备、内存与图优化」单元的第二讲，承接 u6-l1（Device 与 DeviceFactory）。u6-l1 解决的是「op 该放到哪台设备上跑」，本讲要解决的是「op 跑起来需要的张量内存，由谁分配、怎么分配、怎么回收」。

读完本讲，你应当能够：

- 说清 `Allocator` 这个抽象接口的职责，以及为什么 TF 要在「设备」之上再套一层「分配器」。
- 理解 `AllocatorFactoryRegistry` 如何用静态全局对象在进程启动期注册分配器工厂（与 u6-l1 的 DeviceFactory 同一套机制）。
- 掌握 `BFCAllocator`（best-fit with coalescing，带合并的最佳适配）的核心数据结构 `Chunk` / `Bin` / `AllocationRegion`，并能描述一次「分配→切分」与一次「释放→合并」的全过程。
- 认识内存碎片（fragmentation）为何会拖慢甚至拖垮训练，以及 BFC 用来缓解它的三种手段：大小分级桶（size-class bins）、释放即合并（coalescing）、必要时回收空闲整区（garbage collection）。

## 2. 前置知识

本讲默认你已经掌握 u6-l1 的内容：Device 是执行单元抽象，`DeviceFactory` 用工厂模式加静态全局对象自动注册，op 经 `Placer` 被放到具体设备。本讲在此基础上回答：设备拿到 op 后，张量缓冲区的字节从哪里来。

几个对初学者可能陌生的术语，先建立直觉：

- **堆分配器（heap allocator）**：操作系统给每个进程一块大内存，进程需要一段连续字节时，由堆分配器（如 C 的 `malloc`）从中切出一块返回，用完再还回去。问题是反复切、还会产生**碎片**——总空间够，但找不到一块足够大的连续空间。
- **碎片（fragmentation）**：分两种。**外部碎片**指空闲内存被切成很多小块、彼此不连续，拼不出一个大请求；**内部碎片**指分配器给的块比你要的大（为了对齐或凑整），多出来的部分浪费了。
- **arena / pool**：分配器先一次性向系统（或 GPU 驱动）要一大块，之后的小请求都在这块内部「切饼」，避免每次都走昂贵的系统调用。BFCAllocator 就是一个 arena 风格的分配器。
- **best-fit（最佳适配）**：在所有能放得下请求的空闲块里，挑最小的一块，以减少浪费。
- **coalescing（合并）**：释放一块时，如果它物理上相邻的块也空闲，就合成一块更大的，主动修复碎片。

## 3. 本讲源码地图

本讲规格点名的三个关键文件，如今都是**薄壳（shim）**——它们只做一行 `using tsl::...` 把符号再导出，真正的实现已迁到 TF 与 XLA 共享的 `xla/tsl/framework/` 层（见 u1-l2 关于「语言层」、u1-l4 关于 vendoring 的说明）。下表把它们与真实实现一一对应：

| 规格点名（薄壳入口） | 真实实现 | 作用 |
| --- | --- | --- |
| `tensorflow/core/framework/allocator.h` | `third_party/xla/xla/tsl/framework/allocator.h` | 定义抽象 `Allocator`、`AllocatorAttributes`、`SubAllocator`、`AllocatorStats` |
| `tensorflow/core/framework/allocator_registry.h` | `third_party/xla/xla/tsl/framework/allocator_registry.h` | 分配器工厂的全局注册表 + `REGISTER_MEM_ALLOCATOR` 宏 |
| `tensorflow/core/common_runtime/bfc_allocator.h` | `third_party/xla/xla/tsl/framework/bfc_allocator.h`（声明）+ `bfc_allocator.cc`（实现） | best-fit + coalescing 内存分配器 |

此外还会用到两个「实例化点」，证明 BFCAllocator 确实被用在了 CPU 和 GPU 上：

- `tensorflow/core/common_runtime/process_state.cc`：CPU 侧（给 GPU 主机的 CPU 内存）创建 BFCAllocator。
- `tensorflow/core/common_runtime/gpu/gpu_bfc_allocator.cc`：GPU 侧 `GPUBFCAllocator` 继承 BFCAllocator 并配置选项。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **core.framework.allocator**：`Allocator` 抽象 + `SubAllocator`。
2. **core.framework.allocator_registry**：全局注册表。
3. **core.common_runtime.bfc_allocator**：本讲主角，BFC 分配器。

### 4.1 Allocator 抽象与 SubAllocator

#### 4.1.1 概念说明

在 u6-l1 里，Device 有一个面向 op 的 `Compute` 入口；但 op 计算需要临时张量、输出张量，这些字节必须有人给。如果让每个 op 直接调 `malloc`/`cudaMalloc`，会出现两个灾难：一是系统调用和驱动调用极慢，二是反复分配释放会让 GPU 显存碎片化、最终 OOM。

TF 的做法是在 Device 与系统之间插入一个**抽象接口 `Allocator`**：

- Device 只暴露 `GetAllocator(AllocatorAttributes)`，根据需求（要 CPU 内存还是 GPU 显存）返回一个具体分配器。
- op 通过 `OpKernelContext`（见 u4-l2）借分配器要内存，不直接碰系统。

这样，换一个分配策略（比如把 `BFCAllocator` 换成别的）不会影响 op 的写法；同时分配器可以在进程级缓存内存、做统计、做对齐，把脏活脏数据都收口在自己内部。

`Allocator` 接口极简，核心就两个方法：`AllocateRaw(对齐, 字节数)` 和 `DeallocateRaw(指针)`。其余方法（如 `RequestedSize`、`GetStats`）是可选的增强能力。

#### 4.1.2 核心流程

一个分配器对象的生命周期：

1. **注册**：进程启动时，工厂把某个分配器类型登记到全局注册表（见 4.2）。
2. **获取**：Device 在初始化时按 `AllocatorAttributes` 向 `ProcessState` 索要分配器（CPU）或自行创建（GPU）。
3. **分配**：op 调 `AllocateRaw(alignment, num_bytes)`，拿到一段对齐的未初始化内存。
4. **释放**：op 用完后调 `DeallocateRaw(ptr)`；BFC 这类池分配器并不真正归还系统，而是把块标记为空闲，留作下次复用。
5. **统计**：分配/释放都更新 `AllocatorStats`（在用字节、峰值、分配次数等），供 Profiler（见 u9-l3）读取。

#### 4.1.3 源码精读

**薄壳入口**——`tensorflow/core/framework/allocator.h` 只是把 tsl 层的符号 `using` 进 `tensorflow` 命名空间，没有任何逻辑：

[tensorflow/core/framework/allocator.h:36-51](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/allocator.h#L36-L51) —— 这一段 `using tsl::Allocator; using tsl::SubAllocator; ...` 说明 TF 内核代码仍按 `tensorflow::Allocator` 调用，但实体来自共享层。后续讲义引用 `Allocator` 时请直接看 tsl 实现。

**抽象接口本体**——真实的 `Allocator` 是一个抽象基类，纯虚方法只有两个分配/释放签名：

[third_party/xla/xla/tsl/framework/allocator.h:143-191](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator.h#L143-L191) —— `class Allocator` 声明了 `Name()`、纯虚 `AllocateRaw(alignment, num_bytes)`、带属性的 `AllocateRaw` 重载（默认转发到无属性版）、纯虚 `DeallocateRaw(ptr)`。注意第 148 行的常量 `kAllocatorAlignment = 64`：所有分配默认要求 64 字节对齐（满足 AVX-512 等 SIMD 指令的对齐要求）。

[third_party/xla/xla/tsl/framework/allocator.h:209-280](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator.h#L209-L280) —— 一组可选的「元信息」方法：`TracksAllocationSizes()` 表示分配器是否记得每块的请求大小；`RequestedSize`/`AllocatedSize`/`AllocationId` 让上层能反查一个指针实际占多大、唯一编号是多少；`GetStats()`/`ClearStats()` 用于性能统计。BFCAllocator 会 override 这些返回真实数据（见 4.3）。

**分配属性**——`AllocatorAttributes` 用一个 32 位整数的不同位表达「我要哪种内存」：

[third_party/xla/xla/tsl/framework/allocator.h:380-416](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator.h#L380-L416) —— `on_host()`（最低位）表示即便 op 在 GPU 上跑、这块内存也要 CPU RAM；`nic_compatible()`、`gpu_compatible()` 表达 DMA / 跨设备拷贝友好性。典型用法（注释里就有）：GPU 上的 op 想要 CPU 内存时，先 `attr.set_on_host(true)`，再 `allocator(attr)` 取一个 CPU 分配器。这正是 u6-l1 里 Device「不只暴露一个 Allocator」的原因。

**SubAllocator——真正的系统调用在这里**——BFC 这种池分配器并不直接碰 `malloc`/`cudaMalloc`，而是把「向系统要一大块」的脏活委托给 `SubAllocator`：

[third_party/xla/xla/tsl/framework/allocator.h:443-486](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator.h#L443-L486) —— 头部注释说得很清楚：高级分配器（BFC）自己做「缓存/池」管理，因此调 `SubAllocator::Alloc`/`Free` 的频率远低于它自己的 `AllocateRaw`/`Free`。纯虚 `Alloc` 返回至少 `num_bytes` 的内存，并通过出参 `bytes_received` 告诉调用方实际拿到多少；纯虚 `SupportsCoalescing()` 决定 BFC 能否把相邻两块系统返回的内存视作连续（4.3 会用到）。

**统计结构**——`AllocatorStats` 汇集了所有分配器对外汇报的运行时数字：

[third_party/xla/xla/tsl/framework/allocator.h:94-133](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator.h#L94-L133) —— 关注 `bytes_in_use`（当前在用）、`peak_bytes_in_use`（峰值）、`bytes_limit`（上限，`optional` 表示可能无上限）、`pool_bytes`（分配器持有的池子总大小，通常 ≥ `bytes_in_use`）、`largest_free_block_bytes`（堆里最大的空闲块，直接反映「还能不能放下一个大请求」）。这些字段会在 4.3 反复出现。

#### 4.1.4 代码实践

**实践目标**：在源码中确认「Device 如何取分配器」这条链路，验证 Allocator 抽象真的被 Device 使用。

**操作步骤**：

1. 用 Grep 在 `tensorflow/core/framework/device.h` / `device.cc` 中搜索 `GetAllocator`，找到 Device 暴露分配器的方法签名。
2. 在 `tensorflow/core/framework/op_kernel.cc` 或 `op_kernel.h` 中搜索 `allocate_output` / `allocate_temp`，看 `OpKernelContext`（u4-l2）如何转发到 `Allocator::AllocateRaw`。
3. 对照本节引用的 `AllocatorAttributes::set_on_host`，找一个实际调用点（例如 GPU op 申请 CPU 临时内存）。

**需要观察的现象**：op 层完全不出现 `malloc`/`cudaMalloc` 字样，所有内存获取都经 `Allocator` 抽象；不同 `AllocatorAttributes` 会路由到不同分配器实例。

**预期结果**：你能画出 `op → OpKernelContext → Allocator::AllocateRaw → (BFC) → SubAllocator::Alloc → 系统/GPU 驱动` 这条自顶向下的链路。**待本地验证**：具体 `allocate_output` 的实现行号请以本地仓库为准（不同版本有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Allocator::AllocateRaw` 要同时接收 `alignment` 和 `num_bytes` 两个参数，而不是只给字节数？
**答案**：SIMD 指令（如 AVX-512）和某些硬件要求数据起始地址是 2 的幂次对齐（如 64 字节）；若只给字节数，分配器无法保证返回的指针对齐，op 里的向量化计算会因地址未对齐而崩溃或变慢。

**练习 2**：`SubAllocator::SupportsCoalescing()` 返回 false 会怎样？
**答案**：BFCAllocator 在构造时据此设 `coalesce_regions_=false`（见 4.3.3 的构造函数），后续 `Extend` 增长内存时会用 `AddAllocationRegion`（而非 `AddOrExtendAllocationRegion`），即把新拿到的内存当作**独立的、不可与旧区合并**的区域，因为两块内存虽地址相邻但在设备地址空间里并不真正连续。

**练习 3**：`AllocatorStats::pool_bytes` 与 `bytes_in_use` 哪个更大？为什么？
**答案**：通常 `pool_bytes >= bytes_in_use`。池分配器（如 BFC）会从系统多拿一些内存留在池里备用，`pool_bytes` 是池子总量，`bytes_in_use` 是其中真正被 op 占用的部分；差额是池内的空闲块。

---

### 4.2 AllocatorFactoryRegistry 全局注册

#### 4.2.1 概念说明

和 u6-l1 的 `DeviceFactory` 完全同构：TF 用「静态全局对象 + 单例注册表」的模式，让分配器在 `main` 之前自动登记自己，运行时再按优先级挑一个「最合适」的。这样把「要不要换一个分配器」变成「要不要链接一个新的 `.cc` 文件」，无需改动核心代码。

这套机制的关键三件套：

- **`AllocatorFactory`**：抽象工厂，子类负责 `CreateAllocator()` / `CreateSubAllocator(numa_node)`。
- **`AllocatorFactoryRegistry`**：单例注册表，按 `name + priority` 存工厂，`GetAllocator()` 返回优先级最高者。
- **`REGISTER_MEM_ALLOCATOR` 宏**：展开成一个静态 `AllocatorFactoryRegistration` 全局对象，构造时把自己塞进注册表。

#### 4.2.2 核心流程

注册与查询两段式（与 u4-l1 的 OpRegistry「启动期登记、惰性求值」精神一致，但这里更简单——注册即生效）：

1. 链接期：某 `.cc` 文件里的 `REGISTER_MEM_ALLOCATOR("名字", 优先级, 工厂类)` 展开为静态全局对象。
2. `main` 之前：该全局对象构造，调用 `AllocatorFactoryRegistry::singleton()->Register(...)`，把工厂指针存入 `factories_`。
3. 运行期：`ProcessState` 初始化 CPU 分配器时调 `GetAllocator()`，注册表遍历 `factories_` 选最高优先级（同优先级行为未指定），调用其 `CreateAllocator()` 返回实例，并缓存（`first_alloc_made_` 保证只创建一次）。

#### 4.2.3 源码精读

**薄壳入口**——同样是再导出：

[tensorflow/core/framework/allocator_registry.h:31-36](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/allocator_registry.h#L31-L36) —— `using tsl::AllocatorFactory; using tsl::AllocatorFactoryRegistry; ...`。

**抽象工厂与单例注册表**——

[third_party/xla/xla/tsl/framework/allocator_registry.h:38-53](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator_registry.h#L38-L53) —— `AllocatorFactory` 纯虚 `CreateAllocator()` 与 `CreateSubAllocator(numa_node)`；`NumaEnabled()` 默认 false，子类可覆盖以声明自己按 NUMA 节点产出不同的 SubAllocator。

[third_party/xla/xla/tsl/framework/allocator_registry.h:69-130](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator_registry.h#L69-L130) —— `AllocatorFactoryRegistry`：`Register(file,line,name,priority,factory)` 登记；`GetAllocator()` 注释明确「返回最高优先级工厂构造的分配器，同优先级按未指定规则取一个」；内部 `FactoryEntry` 同时持有工厂、缓存好的 `allocator`、以及按 NUMA 节点下标存的 `sub_allocators`。注意 `first_alloc_made_` 字段：一旦首次分配完成，就锁定了选择，后续再注册的工厂不再参与。

**注册宏与启动钩子**——

[third_party/xla/xla/tsl/framework/allocator_registry.h:132-152](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/allocator_registry.h#L132-L152) —— `AllocatorFactoryRegistration` 的构造函数体里就一句 `AllocatorFactoryRegistry::singleton()->Register(...)`；宏 `REGISTER_MEM_ALLOCATOR(name, priority, factory)` 经过两层 `__COUNTER__` 展开成一行的 `static AllocatorFactoryRegistration allocator_factory_reg_N(...)`。这与 u4-l1 的 `REGISTER_OP`、u6-l1 的设备工厂注册是同一种「靠静态全局对象副作用注册」的套路。

**真实使用点**——一处实际的 `REGISTER_MEM_ALLOCATOR` 调用：

[tensorflow/core/common_runtime/threadpool_device.cc:269](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device.cc#L269) —— `REGISTER_MEM_ALLOCATOR("MklCPUAllocator", ...)` 把 Intel oneDNN（原 MKL）版 CPU 分配器登记进注册表。可见「换 CPU 分配器」确实只需新增一个注册语句。

#### 4.2.4 代码实践

**实践目标**：亲手追到「注册表如何被填写、又被谁查询」。

**操作步骤**：

1. Grep 全仓 `REGISTER_MEM_ALLOCATOR(`，列出所有注册的分配器名字与优先级。
2. Grep `AllocatorFactoryRegistry::singleton()` 或 `GetAllocator()`，找到谁在运行期查询。
3. 阅读 `tensorflow/core/common_runtime/process_state.cc` 中 `GetCPUAllocator` 相关逻辑，看它如何决定用 BFCAllocator 还是注册表里的工厂。

**需要观察的现象**：注册点分散在不同 `.cc`（CPU、MKL、TPU 等），查询点集中在 `ProcessState`；优先级数字越大越优先。

**预期结果**：你能说出「新增一个自定义分配器」需要写哪两样东西——一个 `AllocatorFactory` 子类 + 一行 `REGISTER_MEM_ALLOCATOR`。**待本地验证**：不同构建配置下注册的分配器集合不同（如是否启用 MKL、TPU）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetAllocator()` 要在 `first_alloc_made_` 之后拒绝新工厂？
**答案**：分配器一旦开始工作，已经把指针交给了 op，这些指针绑定在具体的分配器实例上；若中途换工厂，旧指针的释放路径会断裂。所以选择必须「一锤定音」。

**练习 2**：`REGISTER_MEM_ALLOCATOR` 与 `REGISTER_OP`（u4-l1）在注册时机上有何异同？
**答案**：两者都靠静态全局对象在 `main` 前登记。差别在于：`REGISTER_OP` 只是登记一个「惰性 builder」，首次 `LookUp` 时才真正构造 `OpDef`；而 `REGISTER_MEM_ALLOCATOR` 登记的就是工厂指针本身，`GetAllocator` 直接拿工厂 `Create`。

---

### 4.3 BFCAllocator：best-fit + coalescing

#### 4.3.1 概念说明

BFC 是 **Best-fit with Coalescing** 的缩写，是一种 dlmalloc 风格的 arena 分配器。它解决的核心痛点是**外部碎片**：GPU 训练中每一步都分配释放成千上万个张量，如果直接用 `cudaMalloc`/`cudaFree`，不但慢，显存很快就会被切碎到放不下一个大张量而 OOM（哪怕剩余总量还够）。

BFC 的思路是：

1. **一次性向 SubAllocator 要一大块**（arena），之后的小请求都在这块内部切饼，极少再碰系统。
2. **把内存切成等长或不等长的 Chunk**，每个 Chunk 要么整块在用、要么整块空闲。
3. **分配时 best-fit**：在能放得下请求的空闲块里挑最小的；放不下就先对齐、再切分，把多余部分还回空闲池。
4. **释放时 coalescing**：若物理相邻的块也空闲，立刻合并成一块大的，主动修复碎片。
5. **空闲块按大小分级入桶（Bin）**：把「找最小能放下的块」从线性扫描变成对数级查找。
6. **必要时做 garbage collection**：碎片严重到 OOM 时，把整块完全空闲的区域还给 SubAllocator，腾出空间重新拼一个更大的区。

类头注释把这套模型概括得很到位：

[third_party/xla/xla/tsl/framework/bfc_allocator.h:51-95](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L51-L95) —— 这段注释是理解全篇的钥匙：它说明了 AllocationRegion（来自 SubAllocator 的一大块）、Chunk（region 内无间隙覆盖的块序列）、Bin（按 size-class 索引空闲块）、以及「分配切分、释放合并」的整体模型。本讲只讲**经典 BFC 行为**（不开空间分区 `enable_spatial_partitioning`），即所有请求都用 `AllocationEnd::kLower`、空闲块 tag 为 `kLower`；空间分区是为多 rank 集合通信对称缓冲区设计的高级特性，本讲暂不展开。

#### 4.3.2 核心流程

**三个核心数据结构**：

- **`AllocationRegion`**：对应一次 `SubAllocator::Alloc` 拿到的大块内存 `[ptr, ptr+size)`。它内部用一个数组 `handles_`（每 `kMinAllocationSize=256` 字节一个槽）记录「这块地址属于哪个 Chunk」，从而能把任意指针反查回所属 Chunk。多个相邻 region 可被 `RegionManager` 合并扩展（当 `SupportsCoalescing()` 为真）。
- **`Chunk`**：region 内的一段，要么整块在用、要么整块空闲。带 `size`、`requested_size`、`ptr`、`allocation_id`（-1 表示空闲）、`prev/next`（物理相邻 Chunk 的双向链表）、`bin_num`（空闲时所在桶号）、`tag`。
- **`Bin`**：一组大小相近的**空闲** Chunk 的集合（已分配的 Chunk 永远不在任何 Bin 里）。用一个按「size 升序、地址次之」排序的 `btree_set` 存 ChunkHandle，使得「桶内取最小」就是取 `begin()`。

**桶的分级**：桶号 `b` 对应的最小容量是 `BinNumToSize(b) = 256 << b`（即 256、512、1024、…），共 `kNumBins = 21` 个桶。给定请求字节数 `n`，桶号由 `BinNumForSize(n) = Log2Floor(max(n,256)/256)` 算出——这是对数压缩，把海量不同大小映射到 21 个桶。

**分配流程**（`AllocateRaw → AllocateRawInternal`）：

1. 把请求字节数向上取整为 `kMinAllocationSize`（256）的倍数 `rounded_bytes`（`RoundedBytes`）。
2. 算出起始桶号 `bin_num = BinNumForSize(rounded_bytes)`。
3. `FindChunkPtr`：从 `bin_num` 号桶开始**向上**扫描每个桶，在每个桶内按 size 升序找**第一个放得下**的空闲块（best-fit）；找到就从桶移除并切分。
4. 若全桶都找不到：尝试 `Extend`——向 SubAllocator 再要一大块（容量按 2 倍增长），造一个覆盖整块的大 Chunk 入桶，再回到第 3 步。
5. 若 Extend 也失败（达到 `memory_limit_`）：尝试 `MergeTimestampedChunks` 紧急合并；再不行尝试 `DeallocateFreeRegions`（垃圾回收）；最终仍失败返回 `nullptr`（OOM）。

**切分（SplitChunk）**：当一个空闲块比请求大很多时，从中间切开——前半 `num_bytes` 给用户、后半作为新的空闲 Chunk 入桶。是否切分由「是否会造成过多内部碎片」决定：若块大小 ≥ 2 倍请求，或超出部分 ≥ `max_internal_fragmentation_bytes_`（默认 128MB），就切。

**释放流程**（`DeallocateRaw → DeallocateRawInternal`）：

1. 用指针反查 `ChunkHandle`，把 Chunk 的 `allocation_id` 置 -1（`MarkFree`），扣减 `bytes_in_use`。
2. 调 `TryToCoalesce`：看物理前驱 `prev`、物理后继 `next` 是否也空闲，若是就 `MergeChunks` 合并成一块更大的。
3. 把（可能已合并的）空闲 Chunk `InsertFreeChunk` 入桶，供下次复用。

**合并（MergeChunks）**：把两个物理相邻的空闲 Chunk `c1`、`c2`（`c1->next == c2`）合成一个——新 size = 两者之和，修正双向链表的 `prev/next` 指针，删掉 `c2`。这是修复碎片的关键：释放后能拼回大块，下次大请求才放得下。

#### 4.3.3 源码精读

**薄壳入口**——

[tensorflow/core/common_runtime/bfc_allocator.h:40-42](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/bfc_allocator.h#L40-L42) —— `using tsl::BFCAllocator;`，`tensorflow::BFCAllocator` 实为 `tsl::BFCAllocator`。

**Options——调参入口**——

[third_party/xla/xla/tsl/framework/bfc_allocator.h:97-143](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L97-L143) —— `Options` 四个核心开关：`allow_growth`（按需扩张而非一次性占满，默认 true）、`allow_retry_on_failure`（分配失败时是否睡眠重试）、`garbage_collection`（OOM 时是否回收空闲整区，默认 false）、`fragmentation_fraction`（控制切分阈值，见下文）。这几个开关直接对应常见的 TF 环境变量。

**常量与桶配置**——

[third_party/xla/xla/tsl/framework/bfc_allocator.h:152-153](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L152-L153) —— `kMinAllocationBits = 8`，`kMinAllocationSize = 256`，所有 Chunk 边界都对齐到 256 字节。

[third_party/xla/xla/tsl/framework/bfc_allocator.h:243-249](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L243-L249) —— `kNumBins = 21`，桶号范围 0..20；注释指出最大桶覆盖 256 MiB 以上的块。

[third_party/xla/xla/tsl/framework/bfc_allocator.h:739-747](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L739-L747) —— 桶号↔大小换算的核心：`BinNumToSize(b) = 256 << b`；`BinNumForSize(bytes)` 先把 `bytes` 与 256 取大、右移 8 位、再取 `Log2Floor`，最后用 `min(kNumBins-1, ...)` 钳到最大桶。`BinForSize` 是二者组合，给定大小直接返回桶对象。

**Chunk 与 Bin 结构**——

[third_party/xla/xla/tsl/framework/bfc_allocator.h:294-365](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L294-L365) —— `Chunk` 结构体：`size`/`requested_size`（前者是实际占用的整块、后者是用户请求的，差额即内部碎片）、`allocation_id`（-1 即空闲）、`ptr`、`prev/next`（物理相邻链表，注释举例 `prev` 应位于 `ptr - prev->size`）、`bin_num`、`tag`。`in_use()` 一行判定 `allocation_id != -1`。这套「边界标记（boundary-tag）」式簿记让任意 Chunk 都能找到物理邻居，是合并的前提。

[third_party/xla/xla/tsl/framework/bfc_allocator.h:369-397](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.h#L369-L397) —— `Bin`：`ChunkComparator` 先按 size 升序、相同 size 按指针地址 tie-break；`free_chunks` 是基于该比较器的 `btree_set`。因此「桶内最小块」就是 `*free_chunks.begin()`，「桶内最大块」就是 `*free_chunks.rbegin()`——这正是 `LargestBinnedFreeChunk`（4.3.3 末）的取法。

**构造函数——建桶与初始区大小**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:80-137](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L80-L137) —— 关键点：第 84-85 行按是否开空间分区决定 `free_chunk_tag_`；第 94-101 行，`allow_growth=true` 时初始每次向 SubAllocator 要 `min(total_memory, 2MiB)`（注释「2MiB smallest initial allocation」），`allow_growth=false` 时直接要满 `total_memory`；第 112-117 行计算切分阈值 `max_internal_fragmentation_bytes_`，未设 `fragmentation_fraction` 时默认 128MB；第 125-136 行用 placement-`new` 在预分配的 `bins_space_` 数组里就地构造 21 个 Bin，并断言 `BinForSize` 映射正确。

**RoundedBytes——请求对齐**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:385-391](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L385-L391) —— 把任意字节数向上取整到 256 的倍数：

\[

\textit{rounded} = 256 \cdot \left\lceil \frac{\textit{bytes}}{256} \right\rceil

\]

实现是整数版 `(bytes + 255) / 256 * 256`。这保证所有 Chunk 边界落在 256 字节网格上，指针反查（`AllocationRegion::IndexFor` 右移 8 位）才能成立。

**AllocateRawInternal——分配主循环**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:513-604](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L513-L604) —— 本讲最核心的一段。按顺序：① 零字节直接返回 `nullptr`（第 517 行）；② `RoundedBytes` 取整（第 524 行）；③ 算起始桶号并加锁（第 532-534 行）；④ 先 `FindChunkPtr` 在现有桶里 best-fit（第 539 行）；⑤ 失败则 `Extend` 扩张后再找（第 547 行）；⑥ 还失败则 `MergeTimestampedChunks` 紧急合并带时间戳的块（第 561 行）；⑦ 再失败则 `DeallocateFreeRegions` 垃圾回收 + 再次 Extend（第 575 行）；⑧ 全部失败打印 OOM 日志（含「可尝试 `TF_GPU_ALLOCATOR=cuda_malloc_async`」的提示，第 597-599 行）并返回 `nullptr`（第 603 行）。这条「逐级降级」的链路正是 BFC 对抗碎片的完整武器库。

**FindTaggedChunkPtr——best-fit 扫描**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:703-741](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L703-L741) —— 经典 BFC（不开空间分区）时 `requested_tag` 恒为 `kLower`，本函数退化为：从起始桶 `bin_num` 向上遍历（`for (bn = bin_num; bn < kNumBins; bn++)`），桶内按 size 升序取**第一个**能放下的空闲块（第 730 行的尺寸判定），找到就 `RemoveFreeChunkFromBin` 并交给 `AllocateChunkFromLowEnd` 切分。因为桶内升序、又从最小桶起扫，取到的就是全局最小的可放下块——即 best-fit。

**AllocateChunkFromLowEnd——切分并占用**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:783-819](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L783-L819) —— 经典 BFC 路径：若块远大于请求（`chunk->size >= rounded_bytes*2` 或超出 ≥ `max_internal_fragmentation_bytes_`），调 `SplitChunk` 切成「请求大小 + 剩余空闲块」两段（第 808-813 行）；给占用块打 `kLower` tag，`FinishChunkAllocation` 登记统计并返回 `chunk->ptr`。

**SplitChunk——从中间切开**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:909-950](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L909-L950) —— 申请新 Chunk 元数据 `h_new_chunk`；让新块指针 = `c->ptr + num_bytes`、新块 size = `c->size - num_bytes`、原块 size 缩为 `num_bytes`；把 `c <-> neighbor` 改写成 `c <-> new_chunk <-> neighbor`（第 939-946 行）；最后 `InsertFreeChunk(h_new_chunk)` 把剩余部分入桶。注释里的链表演示了物理布局如何被一分为二。

**DeallocateRawInternal——释放**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:962-995](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L962-L995) —— 指针反查 ChunkHandle（第 970 行）→ `MarkFree` 置空闲（第 978 行）→ 若未开时间戳计数则 `InsertFreeChunk(TryToCoalesce(h, false))`（第 985 行），即「先尝试与邻居合并，再把结果入桶」。注意开了 `timing_counter_` 的分支会把块先入桶再放进 `timestamped_chunks_` 队列延迟合并——这是为异步执行（见 u3-l2 的 RunAsync）准备的，未到「安全前沿」的块不能立即合并。

**MarkFree——置空闲并扣统计**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:1117-1147](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L1117-L1147) —— `allocation_id = -1`（第 1122 行），清空分配注解；记录释放时间戳（第 1128-1132 行，未开计数则置 0）；扣 `stats_.bytes_in_use -= c->size`（第 1138 行）。

**TryToCoalesce——与邻居合并**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:1149-1179](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L1149-L1179) —— 先看后继 `next` 是否空闲，是则 `RemoveFreeChunk(next)` 再 `MergeChunks(h, next)`（第 1158-1164 行）；再看前驱 `prev` 是否空闲，是则合并 `prev` 与 `h`，返回 `prev` 作为合并后的块句柄（第 1168-1175 行）。返回值是「合并后的块句柄」，调用方再把它入桶。

**MergeChunks——真正的拼接**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:1006-1042](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L1006-L1042) —— 把 `h2` 并入 `h1`：修正邻居指针使 `c1 <-> c2 <-> c3` 变 `c1 <-> c3`（第 1023-1029 行）；新 size `c1->size += c2->size`（第 1032 行）；合并 tag（`MergedChunkTag`，第 1035 行）；取较晚的释放时间戳（第 1038 行）。

**InsertFreeChunk——入桶**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:1051-1065](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L1051-L1065) —— 经典 BFC（tag 非 `kCentralGap`）时：`BinNumForSize(c->size)` 算桶号，`bin->free_chunks.insert(h)` 插入（按比较器自动排序）。

**Extend——向 SubAllocator 扩张**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:169-271](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L169-L271) —— 第 184-187 行：当前 `curr_region_allocation_bytes_` 不够时按 2 倍翻倍；第 190-204 行向 SubAllocator 要内存，失败则按 0.9 倍退步重试；第 229-234 行若 `coalesce_regions_` 则尝试把新内存并入相邻旧 region（`AddOrExtendAllocationRegion`），否则新建 region；第 238-264 行造一个覆盖整块的大 Chunk，并在能合并时把它的 `prev` 链到旧 region 末尾；第 268 行 `InsertFreeChunk(TryToCoalesce(...))` 让新块与相邻空闲块先合并再入桶。

**实例化点 1——CPU 侧**——

[tensorflow/core/common_runtime/process_state.cc:111-115](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/process_state.cc#L111-L115) —— 当 GPU 主机需要 CPU 内存池时，`new BFCAllocator(sub_allocator, cpu_mem_limit, "bfc_cpu_allocator_for_gpu", {allow_growth=true})`。注意上限来自环境变量 `TF_CPU_BFC_MEM_LIMIT_IN_MB`（默认 64GB，见第 100-104 行）。

**实例化点 2——GPU 侧**——

[tensorflow/core/common_runtime/gpu/gpu_bfc_allocator.cc:85-99](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/gpu/gpu_bfc_allocator.cc#L85-L99) —— `GPUBFCAllocator` 继承 BFCAllocator，构造时把 Options 交给几个环境变量决定：`TF_FORCE_GPU_ALLOW_GROWTH`（是否允许按需增长）、`TF_ENABLE_GPU_GARbage_COLLECTION`（是否开垃圾回收，默认开）。这两个变量是排查 GPU OOM 时最常见的旋钮。

**「最大空闲块」查询——碎片的温度计**——

[third_party/xla/xla/tsl/framework/bfc_allocator.cc:606-613](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/third_party/xla/xla/tsl/framework/bfc_allocator.cc#L606-L613) —— `LargestBinnedFreeChunk` 从最大桶往小扫，取第一个非空桶的 `rbegin()`（桶内最大块）。它喂给 `GetFragmentation`：碎片率定义为 1 −（最大空闲块 / 总空闲），值越大说明空闲内存被切得越碎、越危险。

#### 4.3.4 代码实践

**实践目标**：用文字「演算」一次分配与一次释放合并的全过程，吃透 best-fit + coalescing。这是规格点名的实践任务。

**操作步骤**：假设一个全新的 BFCAllocator，初始 `Extend` 拿到一块 **1024 字节**的 region（为方便演算，忽略 256 对齐之外的真实尺寸），此刻内存视图为一整个空闲 Chunk `C0[0,1024)`，在最大桶里。

1. **分配 a = 200 字节**：
   - `RoundedBytes(200) = 256`；起始桶 = `BinNumForSize(256)` = 0。
   - best-fit 扫描找到 `C0`（1024 ≥ 256），但它远大于请求（1024 ≥ 2×256），触发 `SplitChunk`：切成 `C0[0,256)`（占用）+ `C1[256,1024)`（768 字节空闲，入桶）。
   - 返回指针 0。视图：`C0占用(256) | C1空闲(768)`。
2. **分配 b = 500 字节**：
   - `RoundedBytes(500) = 512`；在桶里找到 `C1`（768 ≥ 512），768 ≥ 2×512? 否（768 < 1024），且超出 256 < 128MB，**不切分**，整块占用。
   - 返回指针 256。视图：`C0占用(256) | C1占用(768)`。（注意 b 多占了 768−512=256 字节，这是**内部碎片**。）
3. **释放 a（指针 0）**：
   - `MarkFree(C0)` → `TryToCoalesce`：前驱无，后继 `C1` 在用，**不合并**。`C0` 入桶。
   - 视图：`C0空闲(256) | C1占用(768)`。
4. **释放 b（指针 256）**：
   - `MarkFree(C1)` → `TryToCoalesce`：前驱 `C0` 空闲 → 合并 `C0`+`C1` 成 1024 字节的大块；后继无。入桶。
   - 视图：`C0空闲(1024)`——**整块拼回**，碎片被修复。

**需要观察的现象**：步骤 2 演示了「不切分导致的内部碎片」；步骤 4 演示了「释放即合并把两块拼回大块」。这正是 BFC 名字里 Coalescing 的价值——若无合并，释放后视图会是 `空闲(256) | 空闲(768)` 两块碎片，下次来一个 800 字节的请求就会失败（虽有 1024 总量）。

**预期结果**：你能用一句话指出 BFC 主要解决的是**外部碎片**问题（通过合并把碎片拼回大块），同时也容忍少量**内部碎片**（不切分时多占一点）以换取切分开销的减少。

**进阶（可选）**：在 `bfc_allocator.cc` 的 `SplitChunk`、`MergeChunks`、`TryToCoalesce` 各加一行 `VLOG(1)` 打印 `ptr` 与 `size`，再用一个会反复分配释放不同大小张量的小脚本跑 GPU，把 `TF_CPP_VMODULE=bfc_allocator=1` 打开，观察日志里切分与合并的真实发生。**待本地验证**：需要可用的 GPU 环境与编译版 TF。

#### 4.3.5 小练习与答案

**练习 1**：为什么空闲 Chunk 用「按 size 升序」的有序集合存，而已分配 Chunk 不入桶？
**答案**：分配时要 best-fit（找最小可放下块），按 size 升序能让「桶内第一个放得下的」就是最优，省去全量比较。已分配 Chunk 不会再被「挑选」，只需通过指针反查找到（由 `AllocationRegion` 的 `handles_` 数组负责），所以不入桶，避免桶里混入无用条目、拖慢查找。

**练习 2**：`Extend` 里 `curr_region_allocation_bytes_` 每次 ×2 增长（第 184-187 行、第 206-208 行），这种「倍增」有什么好处？
**答案**：这是经典的「几何增长」策略，使向 SubAllocator 要内存的次数从 O(n) 降到 O(log n)，分摊每次分配的成本；同时避免一开始就占满显存（配合 `allow_growth`），让多个进程能共享同一张 GPU。

**练习 3**：`DeallocateRawInternal` 里为什么有 `if (timing_counter_ != nullptr)` 两个分支？不开时间戳就立即合并，开了反而入桶再延迟合并？
**答案**：异步执行（u3-l2 的 RunAsync）下，一块内存「逻辑上已释放」但 GPU 流可能还在读它（`freed_at_count` > 安全前沿）。立即合并并复用会导致写后写数据竞争。所以开了 `timing_counter_` 后，释放的块先进桶但打上时间戳、塞进 `timestamped_chunks_` 队列，等 `MergeTimestampedChunks` 确认安全（`freed_at_count < safe_frontier_`）后才真正合并——这就是 `SetSafeFrontier` 与 `safe_frontier_` 存在的意义。

## 5. 综合实践

把本讲三个模块串起来，完成一次「定位 → 注册 → 行为」的端到端追踪：

1. **入口**：从一个 op 的 `allocate_output` 出发（u4-l2 的 OpKernelContext），画出内存请求如何到达 `BFCAllocator::AllocateRaw`，再经 `SubAllocator::Alloc` 落到 GPU 驱动。
2. **配置**：阅读 `gpu_bfc_allocator.cc`，列出影响 GPU BFC 行为的全部环境变量（`TF_FORCE_GPU_ALLOW_GROWTH`、`TF_ENABLE_GPU_GARBAGE_COLLECTION`、`TF_GPU_ALLOCATOR`），并说明各自开关的是 `Options` 的哪个字段或哪条降级路径。
3. **行为**：对照 4.3.4 的演算，自己设计一组「分配 300B → 分配 400B → 释放 300B → 分配 500B」的操作（先把请求 round 到 256 的倍数），画出每一步后的 Chunk 视图，标出哪一步发生切分、哪一步发生合并、哪一步产生了内部碎片。
4. **诊断**：解释为什么 GPU 训练时偶发 OOM、但 `nvidia-smi` 显示显存还有剩余——用本讲的「外部碎片」「`LargestBinnedFreeChunk`」「`DeallocateFreeRegions` 垃圾回收」三个概念作答。

完成后，你应当能独立阅读 `bfc_allocator.cc` 的任意方法，并回答「为什么这样设计」。

## 6. 本讲小结

- `Allocator` 是 TF 在「设备」之上插入的内存抽象：op 只认 `AllocateRaw/DeallocateRaw`，具体策略由分配器决定；`AllocatorAttributes`（`on_host` 等）让 GPU 上的 op 也能取到 CPU 内存。
- `SubAllocator` 是「真正调系统/驱动」的那一层，BFC 这类池分配器只在极少时刻调它；`SupportsCoalescing()` 决定相邻系统内存能否被视作连续。
- `AllocatorFactoryRegistry` 用「静态全局对象 + 单例 + `REGISTER_MEM_ALLOCATOR` 宏」实现启动期自动注册，运行期按优先级选工厂——与 u6-l1 的 DeviceFactory 同构。
- `BFCAllocator` 是 best-fit + coalescing 的 arena 分配器：用 `Chunk`（带边界标记的双向链表）描述块、`Bin`（21 个 size-class 桶，桶内按 size 升序）索引空闲块、`AllocationRegion` 把指针反查回 Chunk。
- 分配走「取整 → 算桶 → best-fit 扫描 → 必要时切分」，逐级降级到 Extend、紧急合并、垃圾回收；释放走「置空闲 → 与物理邻居合并 → 入桶」，释放即合并是修复外部碎片的关键。
- GPU OOM 常由碎片而非真实显存不足引起；`TF_FORCE_GPU_ALLOW_GROWTH`、`TF_ENABLE_GPU_GARBAGE_COLLECTION` 是最重要的两个调参旋钮。

## 7. 下一步学习建议

本讲讲的是「单设备内的内存分配」。接下来的学习方向：

- **u6-l3（Grappler 图优化器）**：内存布局优化（如 layout、常量折叠）发生在图执行前，与分配器的对齐、复用策略互相影响，值得对照阅读。
- **u6-l4（分布式策略 distribute）**：多卡训练时每张卡各自有 BFCAllocator，跨卡通信的缓冲区（见 u3-l2 的 `_Send`/`_Recv`）也要从分配器取，理解本讲后能更好理解集合通信的内存开销。
- **u9-l3（Profiler 与性能分析）**：`AllocatorStats` 的 `bytes_in_use`、`peak_bytes_in_use`、碎片率正是 Profiler 内存视图的数据来源，学完本讲可直接去读 Profiler 如何消费这些统计。
- **进阶源码**：若想深入，可继续读 `bfc_allocator.cc` 的空间分区（`enable_spatial_partitioning`、`ChunkTag`、`central_gap_`）与 `MergeTimestampedChunks`/`SetSafeFrontier` 的异步安全机制——它们为多 rank 对称缓冲区与异步执行服务。
