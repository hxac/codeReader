# 对称内存堆 API：malloc / calloc / align / free

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `aclshmem_malloc` 系列 8 个堆 API 的职责划分，以及 `aclshmem_` 与 `aclshmemx_` 两套前缀、`mem_type` 参数之间的关系。
2. 解释「对称内存」的准确含义：不是各 PE 地址相同，而是**各 PE 堆内偏移相同**，并能在源码中指出「远端地址 = 对端堆基址 + 本地堆内偏移」这套寻址是怎么建立起来的。
3. 理解为什么 `malloc / calloc / align / free` 都是**集体调用**：每次调用内部都执行一次控制面 barrier，各 PE 必须**同序、同参数个数**地调用，否则轻则偏移错位、重则整个组阻塞到超时。
4. 读懂 `memory_manager` 这个 best-fit 双树分配器的实现，并能在纸上推演分配与释放过程。
5. 独立编写一个验证对称性的小程序：两个 PE 以相同顺序分配相同大小的缓冲区，用 `aclshmemx_get_heap_base` 与指针偏移验证偏移一致。

## 2. 前置知识

本讲建立在前几讲的概念之上，先快速回顾，再补充两个新术语。

**已学过的概念（回顾）**：

- **PE（Processing Element）**：SHMEM 中的通信参与者编号，`aclshmem_my_pe()` 查询自己，`aclshmem_n_pes()` 查询总数（u1-l4）。
- **初始化三阶段**（u2-l2）：`aclshmemx_init_attr` 内部依次完成 Bootstrap 建链 → HYBM 建堆 → 子模块就绪。本讲关注的正是第二阶段产物——对称堆——在运行期如何被使用。
- **控制面 barrier**（u2-l3）：`aclshmemi_control_barrier_all()` 底层就是 bootstrap 插件（Config Store 或 MPI）提供的 barrier 能力，走 CPU 侧 TCP 星型拓扑，与数据面完全分离。本讲会看到 malloc/free 也复用它。

**本讲新术语**：

- **集体调用（collective call）**：一个函数要求**所有 PE 都以相同的次数、相同的顺序**参与，缺一个 PE 其余进程就会在内部同步点等待。SHMEM 的 init、finalize、malloc、free、barrier 都是集体调用。
- **GVA（Global Virtual Address）窗口**：初始化时 HYBM 在本进程的 NPU 虚拟地址空间里预留一大段地址，并把**所有 rank 的堆**按固定间隔映射进来。于是「访问别的 PE 的堆」在本地看来只是访问窗口内另一段普通地址。
- **best-fit 分配器**：分配时在所有空闲块中挑选「能装下请求的最小块」，与 first-fit（找到第一个能装下的就用）相对，优点是减少大块被切碎。
- **对称堆 vs 普通 device 内存**：`aclrtMalloc` 分配的内存只有本地进程能解引用，远端不可见；`aclshmem_malloc` 分配的内存位于对称堆内，同偏移的地址在每个 PE 上语义对应，可以被 put/get 直接寻址。

一个帮助直觉的比喻：对称堆像一排编号相同的储物柜，每个 PE 一面墙，**柜子编号（偏移）在每面墙上含义一致**。你把东西放进自己墙上的 3 号柜，就天然知道它在任何一面墙上的「3 号柜」。至于每面墙物理上钉在哪里（基址），由初始化负责登记在表里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/host/mem/shmem_host_heap.h` | 堆 API 的全部对外声明：`aclshmem_malloc/calloc/align/free` 与带 `mem_type` 的 `aclshmemx_` 变体、`aclshmemx_get_heap_base` |
| `include/host_device/shmem_common_types.h` | `aclshmem_mem_type_t` 枚举、堆对齐常量 `ACLSHMEM_HEAP_ALIGNMENT_SIZE`、全局状态结构 `aclshmem_device_host_state_t`（heap_base、heap_size、各引擎基址表都在这里） |
| `src/host/mem/shmem_mm.cpp` | API 入口实现：参数校验、调用分配器、**每次操作后的控制面 barrier**、HOST_SIDE 懒初始化 |
| `src/host/mem/shmem_mgr.cpp` | `memory_manager` 分配器实现：best-fit 双树、对齐分配、释放合并 |
| `src/host/mem/shmemi_mgr.h` / `shmemi_mm.h` | 上述两者的内部声明（`shmemi_` 前缀，不对外） |
| `src/host/init/shmem_init.cpp` | 建堆时机：`heap_size` 计算、`memory_manager_initialize` 挂接分配器、子模块自己也在做对称 malloc |
| `src/host/init/backends/shmem_init_backend.cpp` | 每个 rank 的堆在 GVA 窗口中的排布（`reach_info_init`）、DEBUG 模式的分配对称性校验（`is_alloc_size_symmetric`） |
| `examples/hccs_sio_link/main.cpp` | 实践参考：真实示例如何用 `aclshmemx_get_heap_base` 算堆内偏移 |
| `examples/rdma_demo/main.cpp` | 实践参考：最小 malloc → 用 → free 的完整生命周期 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：API 全景 → 对称性从哪里来 → 集体调用语义 → 分配器内部 → HOST_SIDE 扩展。

### 4.1 模块一：堆 API 全景与 mem_type

#### 4.1.1 概念说明

SHMEM 的堆 API 分两层：

- **标准层 `aclshmem_`**：`malloc / calloc / align / free` 四件套，**只操作 device（NPU HBM）对称堆**，无 `mem_type` 参数。每个 API 旁边都有一个 `#define` 短名别名（如 `shmem_malloc`），这是本仓库的声明惯例（u1-l3 已总结）。
- **扩展层 `aclshmemx_`**：同名四件套外加一个 `aclshmem_mem_type_t mem_type` 参数（默认 `DEVICE_SIDE`），可以选择把对称堆放在 device 或 host 内存。另有 `aclshmemx_get_heap_base` 查询堆基址。

`mem_type` 的定义在公共类型头里，Host 与 Device 侧共用：

```cpp
enum aclshmem_mem_type_t {
    HOST_SIDE = 0, // The memory address allocated by shmem is on the host side
    DEVICE_SIDE    // The memory address allocated by shmem is on the device side
};
```

#### 4.1.2 核心流程

8 个 API 的语义对照：

| API | 语义 | 初始化内容 | mem_type 可选 |
| --- | --- | --- | --- |
| `aclshmem_malloc(size)` | 分配 size 字节 | 未初始化 | 否（固定 device） |
| `aclshmem_calloc(nmemb, size)` | 分配 nmemb×size 字节 | 清零（`aclrtMemset`） | 否 |
| `aclshmem_align(alignment, size)` | 按 alignment（2 的幂）对齐分配 | 未初始化 | 否 |
| `aclshmem_free(ptr)` | 释放；NULL 直接返回 | — | 否 |
| `aclshmemx_malloc(size, mem_type)` | 同上 | 未初始化 | 是，默认 DEVICE_SIDE |
| `aclshmemx_calloc(count, size, mem_type)` | 同上 | 清零 | 是 |
| `aclshmemx_align(alignment, size, mem_type)` | 同上 | 未初始化 | 是 |
| `aclshmemx_get_heap_base(mem_type)` | 返回本地对称堆起始地址 | — | 是 |

调用这些 API 的前置条件：**必须已完成 `aclshmemx_init_attr` 初始化**，否则入口处发现内存管理器为空指针，直接报错返回 `NULL`。

#### 4.1.3 源码精读

标准层四件套的声明，每个都带注释说明语义与边界条件（size 为 0 返回 NULL、alignment 必须是 2 的幂、free 接受 NULL）：

- [include/host/mem/shmem_host_heap.h:28-29](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L28-L29)：声明 `aclshmem_malloc` 并定义短名别名 `shmem_malloc`。
- [include/host/mem/shmem_host_heap.h:40-41](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L40-L41)：`aclshmem_calloc`，元素计数×元素大小、内容清零。
- [include/host/mem/shmem_host_heap.h:51-52](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L51-L52)：`aclshmem_align`，地址是 alignment 的整数倍。
- [include/host/mem/shmem_host_heap.h:60-61](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L60-L61)：`aclshmem_free`。

扩展层带 `mem_type` 参数（C++ 默认参数，所以不传就是 device 堆）：

- [include/host/mem/shmem_host_heap.h:71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L71)：`aclshmemx_malloc(size, mem_type = DEVICE_SIDE)`。同文件 L81、L91、L99 依次是 calloc/align/free 的扩展版。

`mem_type` 枚举定义在 Host/Device 共用的类型头中（这说明 device 侧 kernel 也能理解这个类型）：

- [include/host_device/shmem_common_types.h:89-92](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L89-L92)：`HOST_SIDE = 0`、`DEVICE_SIDE`。

堆基址查询接口，注释明确写了三个关键约束——必须在初始化后调用、返回值仅本地有效、未初始化时返回 NULL：

- [include/host/mem/shmem_host_heap.h:102-114](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h#L102-L114)：`aclshmemx_get_heap_base` 的完整声明与说明。

#### 4.1.4 代码实践

**实践目标**：建立「声明 → 别名 → 实现文件」的快速定位能力。

**操作步骤**：

1. 打开 [include/host/mem/shmem_host_heap.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/mem/shmem_host_heap.h)，数一数共有几个 `ACLSHMEM_HOST_API` 函数。
2. 用 Grep 在 `src/` 下搜索 `void\* aclshmem_malloc` 与 `void\* aclshmemx_malloc`，确认两者都实现在 `src/host/mem/shmem_mm.cpp`。
3. 注意一个细节：**标准层四件套有 `#define shmem_xxx` 短名别名，扩展层 `aclshmemx_` 四件套没有**。思考为什么（提示：`shmem_` 前缀要对齐 OpenSHMEM 标准命名，而 `x` 后缀本身就是本库的私有扩展，再造短名没有意义）。

**需要观察的现象**：8 个 API 声明全部集中在一个头文件里，没有分散；实现也集中在 `shmem_mm.cpp` 一个文件里。

**预期结果**：8 个函数（标准 4 + 扩展 4）+ 1 个 `get_heap_base`（外加 1 个本讲不展开的 `aclshmemx_get_buffer_ptr`，它服务于 u8-l3 的 user buffer heap 机制）。

### 4.2 模块二：对称性从哪里来——堆布局与偏移寻址

#### 4.2.1 概念说明

u1-l1 讲过「远端地址 = 对端堆基址 + 本地堆内偏移」，本讲把这句话落到源码。要回答三个问题：

1. **本地堆基址记在哪？** —— 全局状态结构 `g_state`（类型 `aclshmem_device_host_state_t`）的 `heap_base` 字段。
2. **对端堆基址从哪查？** —— 同一个结构里的三张基址表 `p2p_device_heap_base / rdma_device_heap_base / sdma_device_heap_base`，每张表是一个按 PE 编号索引的数组，分别对应「在 P2P / RDMA / SDMA 引擎视角下，对端堆映射到本进程地址空间的起点」。
3. **偏移为什么天然一致？** —— 因为每个 PE 跑的是**同一个程序**，只要每次 malloc 的顺序和大小都相同，本地分配器给出的偏移就相同（模块四会看到分配器是确定性的）。malloc 内置的 barrier（模块三）负责把这个「同序同大小」从约定变成运行期保证。

#### 4.2.2 核心流程

初始化期间（u2-l2 的第二阶段）堆布局的建立过程：

```text
local_mem_size（用户在 init attr 里指定，如 1 GiB）
        + ACLSHMEM_EXTRA_SIZE（内部结构预留，当前约 6 MiB，按 2 MiB 页对齐）
        = heap_size                       ← g_state.heap_size

GVA 窗口排布（reach_info_init）：
aligned = ALIGN_UP(heap_size, 1 GiB)      ← 每个 rank 的窗口按 1 GiB 向上取整
窗口基址 gva 起依次排开：
    rank 0 的堆 → [gva + 0·aligned, gva + 1·aligned)
    rank 1 的堆 → [gva + 1·aligned, gva + 2·aligned)
    rank i 的堆 → gva + i·aligned          ← 存入 p2p_device_heap_base[i]

运行期寻址：
本地 aclshmem_malloc(n) 得到 ptr，偏移 o = ptr − g_state.heap_base
远端 PE j 上同一变量的地址 = 基址表[j] + o
```

举例：`local_mem_size = 1 GiB` 时，`heap_size = 1 GiB + 6 MiB`，向上对齐到 1 GiB 的整数倍得 `aligned = 2 GiB`——每个 rank 在 GVA 窗口里占 2 GiB，其中前 1 GiB+ 是堆本体，尾部留空。

#### 4.2.3 源码精读

全局状态结构中与堆相关的字段——本地基址、host 侧基址、三张按引擎区分的远端基址表、堆大小：

- [include/host_device/shmem_common_types.h:377-396](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L377-L396)：`aclshmem_device_host_state_t` 的 `heap_base`（本地堆起点）、`host_heap_base`（host 侧堆起点）、`p2p/rdma/sdma_device_heap_base`（三张远端基址表数组）、`heap_size`（堆总大小）。这个结构同时会被镜像下发到 device 侧（u4-l1 的 `update_device_state`），所以 kernel 里也能拿到同一套基址。

堆窗口 1 GiB 对齐常量与每 rank 排布逻辑：

- [include/host_device/shmem_common_types.h:285](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L285)：`ACLSHMEM_HEAP_ALIGNMENT_SIZE = (1UL << 30UL)`，即 1 GiB，注释说明与 DEVMM_HEAP_SIZE 对齐。
- [src/host/init/backends/shmem_init_backend.cpp:465-475](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/backends/shmem_init_backend.cpp#L465-L475)：`reach_info_init` 中，先 `ALIGN_UP(heap_size, 1 GiB)` 得到每个 rank 的窗口步长，再循环写入 `p2p_device_heap_base[i] = gva + aligned * i`。同一段还顺带把每个 rank 可达的引擎类型写进 `topo_list`（供传输层选路，u5 单元展开）。

堆大小与分配器的挂接点（初始化第二阶段的收尾动作）：

- [src/host/init/shmem_init.cpp:957](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L957)：`heap_size = local_mem_size + ACLSHMEM_EXTRA_SIZE`，EXTRA 是给同步池等内部对象预留的空间（其充足性由 [shmem_init.cpp:50-52](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L50-L52) 的 static_assert 保证）。
- [src/host/init/shmem_init.cpp:1002-1013](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1002-L1013)：`reserve_heap()` 预留 GVA → `setup_heap()` 实体化并交换 slice → `memory_manager_initialize(allocator_base, heap_size)` 在堆上挂起分配器。此后 malloc 才可用。

偏移翻译的对外接口——把「本地对称地址 + 目标 PE」直接换算成对端地址：

- [include/host/data_plane/shmem_host_rma.h:66-74](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/data_plane/shmem_host_rma.h#L66-L74)：`aclshmem_ptr(ptr, pe)`，注释明确它做的正是「把本地对称地址翻译成指定 PE 上的对应地址」，返回地址的可访问方式取决于传输与拓扑（P2P 可达时就是 GVA 窗口内的直接指针）。

真实示例如何消费这套偏移（本讲综合实践的参考模板）：

- [examples/hccs_sio_link/main.cpp:43-55](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/hccs_sio_link/main.cpp#L43-L55)：先 `aclshmemx_get_heap_base()` 拿本地基址，再 `aclshmem_ptr(local_heap_base, peer)` 拿对端基址。
- [examples/hccs_sio_link/main.cpp:226-231](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/hccs_sio_link/main.cpp#L226-L231)：`offset = symm_addr − heap_base`——正是「堆内偏移」的标准算法，拿到 offset 后加到任何一条通路的对端基址上即可。

#### 4.2.4 代码实践

**实践目标**：用纸笔推演 GVA 窗口布局，把「对称 = 偏移相同」变成可计算的具体数字。

**操作步骤**：

1. 设 `local_mem_size = 1 GiB`，按上文公式计算 `heap_size`、`aligned`，以及 4 个 PE（rank 0~3）各自的窗口起始位置（以 `gva` 为原点）。
2. 打开 [src/host/init/backends/shmem_init_backend.cpp:465-475](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/backends/shmem_init_backend.cpp#L465-L475)，对照你的计算检查循环体里 `gva + aligned * i` 的含义。
3. 思考：如果 `local_mem_size` 恰好是 1 GiB 减 1 字节，`aligned` 是多少？浪费了多少地址空间？

**需要观察的现象 / 预期结果**：`heap_size = 1 GiB + 6 MiB`；`aligned = 2 GiB`；rank i 窗口起点为 `gva + i × 2 GiB`。若 `local_mem_size` 为 1 GiB−1B，`heap_size` 对齐后仍是 2 GiB，窗口利用率约 50%——这说明 `local_mem_size` 最好直接填 1 GiB 的整数倍，避免窗口浪费（地址空间浪费不影响物理内存占用，但会限制可预留的 GVA 总量）。

### 4.3 模块三：malloc/free 是集体调用——barrier 与对称性校验

#### 4.3.1 概念说明

这是初学者最容易踩坑的一点：`aclshmem_malloc` 看起来是本地函数，**实际上每次调用都会触发一次全组 barrier**。原因有两个：

1. **保证偏移对齐的运行期顺序**。分配器是各 PE 独立运行的（模块四），只有当「每个 PE 的第 k 次堆操作」彼此对齐——即所有 PE 都完成第 k 次操作之后，任何 PE 才开始第 k+1 次——同序同大小的调用序列才会产生相同的偏移序列。malloc 末尾的 barrier 实现了这种逐操作对齐；free 则在释放**之前**先 barrier。
2. **给 DEBUG 构建提供对称性校验的机会**。DEBUG_MODE 下 malloc 会用控制面 allgather 收集所有 PE 本次请求的大小并比对，发现不一致立即报错——把「各 PE 必须同序同大小分配」从口头约定变成启动期就能暴露的硬错误。

由此推出三条使用纪律：

- 所有 PE 调用 malloc/free 的**次数必须相同**，哪怕某 PE 不需要这块数据也要「陪跑」分配（否则 barrier 凑不齐人数，阻塞至 bootstrap 超时，默认 120 秒，u2-l1）。
- 分配**顺序与大小**必须相同，否则偏移错位、数据写错地方（DEBUG 构建会直接报 `Asymmetric alloc size detected`）。
- 不要在 malloc/free 附近引入单侧分支逻辑，例如 `if (my_pe == 0) aclshmem_malloc(...)` 是典型错误。

#### 4.3.2 核心流程

`aclshmem_malloc(size)` 在一个 PE 上的时序：

```text
入口：内存管理器为空？→ 报错返回 NULL（未初始化）
  1. ptr = 分配器.allocate(size)          ← 本地操作，改本地空闲树
  2. aclshmemi_control_barrier_all()       ← 等全组 PE 都到达本次分配
     失败 → 回滚：分配器.release(ptr)，返回 NULL
  3. [仅 DEBUG_MODE] is_alloc_size_symmetric(size)
     allgather 收集所有 PE 的 size → 逐个与 pe0 比对 → 不等则报错
返回 ptr
```

`aclshmem_free(ptr)` 的顺序略有不同——**先 barrier 再 release**：先确保全组都走到这一次 free 操作，本地分配器状态才推进。

#### 4.3.3 源码精读

malloc 的完整骨架（本地分配 → 集体 barrier → 失败回滚 → DEBUG 校验）：

- [src/host/mem/shmem_mm.cpp:37-62](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L37-L62)：`aclshmem_malloc`。L44 本地分配，L46 `aclshmemi_control_barrier_all()`（u2-l3 走读过的控制面 barrier），L47-53 失败时回滚释放，L54-60 是 `DEBUG_MODE` 下的对称性检查。

calloc 在此之上多做两件事——乘法溢出断言与清零：

- [src/host/mem/shmem_mm.cpp:64-94](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L64-L94)：`SHM_ASSERT_MULTIPLY_OVERFLOW(nmemb, size, heap_size)` 防 `nmemb×size` 溢出；L75 `aclrtMemset(ptr, ..., 0, total_size)` 清零——注意清零发生在 barrier **之前**，memset 失败同样回滚。

align 与 free：

- [src/host/mem/shmem_mm.cpp:96-114](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L96-L114)：`aclshmem_align`，转调分配器的 `aligned_allocate`（模块四）。
- [src/host/mem/shmem_mm.cpp:116-138](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L116-L138)：`aclshmem_free`。L122 对 NULL 宽容处理；L126 **barrier 在 release 之前**；L132 才真正 `release(ptr)`。

DEBUG 对称性校验的实现——一次 allgather 就能发现「谁分得不一样」：

- [src/host/init/backends/shmem_init_backend.cpp:887-945](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/backends/shmem_init_backend.cpp#L887-L945)：`is_alloc_size_symmetric`。收集 `all_size[npes]`，以 pe0 的值为基准 `std::find_if` 找出第一个不同的 PE，错误日志里同时打印参考值、肇事 PE 与本 PE 的值。

还有一处容易被忽略的事实：**初始化期间库自己就在做对称 malloc**，它们同样占用堆内偏移、同样走 barrier：

- [src/host/init/shmem_init.cpp:648-653](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L648-L653)：`aclshmemi_signal_init` 用 `aclshmem_malloc(512)` 在堆上切出信号计数器。
- [src/host/team/shmem_team.cpp:80-99](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/team/shmem_team.cpp#L80-L99)：`aclshmemi_team_init_sync_pool` 先后 `aclshmem_malloc(SYNC_POOL_SIZE)`（NPU 级同步池，5 MiB）与 `aclshmem_malloc(SYNC_COUNTERS_SIZE)`（128 KiB）。
- 这些调用发生在 [shmem_init.cpp:1019-1021](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1019-L1021) 的 signal/team/sync 子模块初始化阶段——在用户代码运行之前。

推论：**用户第一次 `aclshmem_malloc` 拿到的偏移不是 0**。按当前宏取值推算：512（signal）+ 5242880（sync pool）+ 131072（sync counters）= 5374464（0x520200）。

#### 4.3.4 代码实践

**实践目标**：验证「第一次 malloc 偏移不为 0」，并理解 barrier 的集体约束。

**操作步骤**：

1. 仿照 [examples/rdma_demo/main.cpp:38-48](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/rdma_demo/main.cpp#L38-L48) 的骨架（aclInit → aclrtSetDevice → aclshmemx_init_attr → malloc），编写：

   ```cpp
   // 示例代码：观察第一次 malloc 的堆内偏移
   void *base = aclshmemx_get_heap_base();
   void *p1 = aclshmem_malloc(64);
   printf("pe %d heap_base=%p p1=%p offset=0x%lx\n",
          aclshmem_my_pe(), base, p1,
          (uint64_t)((uint8_t *)p1 - (uint8_t *)base));
   ```

2. 用 `examples/init/run.sh` 同样的方式拉起 2 个 PE 运行。
3. 对比两个 PE 打印的 `offset` 是否相等；与 0x520200 的推算值对照。
4. （选做，破坏性实验）在其中一个 PE 上把 64 改成 128：两个 PE 的 offset 将不再相等；若用 DEBUG 构建运行，还能看到 `Asymmetric alloc size detected` 日志。

**需要观察的现象**：两个 PE 的 `heap_base` 不同（各自的 GVA 窗口），但 `offset` 完全一致；offset 是一个明显非零的值。

**预期结果**：offset ≈ 0x520200（精确值取决于编译期宏 `SYNC_POOL_SIZE / SYNC_COUNTERS_SIZE`，若与推算不符，回到 [include/host_device/shmem_common_types.h:210-226](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host_device/shmem_common_types.h#L210-L226) 重新代入计算）。本实践需要真实 NPU 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `aclshmem_free` 把 barrier 放在 `release` 之前，而 `aclshmem_malloc` 把 barrier 放在 `allocate` 之后？

**答案**：两次目的不同。malloc 的本地分配不依赖其他 PE 的状态，先做完本地动作再进 barrier，可以让「分配耗时」在组内并行重叠；barrier 只需保证第 k 次操作全组完成后才有人开始第 k+1 次。free 则必须先 barrier——若先释放，其他 PE 可能仍在依赖「第 k 次操作前的堆布局」进行中的分配/寻址，先推进本地空闲树会破坏逐操作对齐的约定。

**练习 2**：某程序写了 `if (aclshmem_my_pe() == 0) { buf = aclshmem_malloc(1024); }`，会发生什么？

**答案**：两阶段恶果。第一，PE0 在 malloc 内部的 `aclshmemi_control_barrier_all` 上等不到其他 PE，阻塞至 bootstrap 超时（默认 120 秒）后返回失败并回滚。第二，即使改成所有 PE 都调用但大小不同（PE0 分 1024、其余分 512），barrier 能通过，但 PE0 之后所有缓冲区的堆内偏移与其余 PE 错位 512 字节，后续 put/get 写错区域；DEBUG 构建下 `is_alloc_size_symmetric` 会直接报错。

### 4.4 模块四：memory_manager——best-fit 双树分配器

#### 4.4.1 概念说明

`memory_manager` 是一个**纯本地**的分配器：它只管理 `[base_, base_+size_)` 这一段连续堆，不知道其他 PE 的存在（跨 PE 一致性由模块三的 barrier 在 API 层保证）。它的两个特点：

- **双索引结构**：同一批空闲块同时挂在两棵平衡树上——`address_idle_tree_`（`std::map<offset, size>`，按地址序）与 `size_idle_tree_`（`std::set<memory_range>`，按「大小优先、偏移次之」排序）。地址树服务于 O(log n) 的邻居查找（释放合并），大小树服务于 O(log n) 的 best-fit 查找。
- **最小 16 字节粒度**：所有请求向上取整到 16 字节的整数倍（`allocated_size_align_up`），保证返回地址与块大小都按 16 字节对齐。

线程安全用 `pthread_spinlock_t` 自旋锁保护——堆操作临界区极短，自旋锁比互斥锁更合适。

#### 4.4.2 核心流程

`allocate(size)` 五步（best-fit）：

```text
1. 合法性：0 < size ≤ heap_size
2. aligned_size = ceil(size / 16) × 16
3. 在 size_idle_tree_ 中 lower_bound({0, aligned_size})
   → 第一个「size ≥ aligned_size」的空闲块（同大小时取最小 offset）
   → 即能装下请求的最小块：best-fit
4. 把该块从两棵空闲树摘除，记入 address_used_tree_
5. 若块有剩余（target_size > aligned_size），把尾部残余 [offset+aligned_size, ...) 重新挂回两棵空闲树
返回 base_ + target_offset
```

`aligned_allocate(alignment, size)` 的差别在第 3 步：候选块起始地址若不满足 alignment 对齐，计算 `head_skip` 向前跳到对齐边界，块头部残余切成小空闲块单独挂回；循环继续检查下一个候选直到找到「跳过后仍装得下」的块。

`release(addr)` 的关键是**前后合并**：释放块 [offset, size) 时，在地址树里查 `offset+size` 处是否有后邻空闲块、`lower_bound(offset)` 前一位是否是紧贴的前邻空闲块，分别合并成一个大块再挂回，避免堆碎片化。

#### 4.4.3 源码精读

分配器类定义与两棵树的声明：

- [src/host/mem/shmemi_mgr.h:34-60](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmemi_mgr.h#L34-L60)：`memory_manager` 的公开方法（allocate / aligned_allocate / change_size / release / allocated_size）与私有成员（`base_`、`size_`、自旋锁、`address_idle_tree_`、`address_used_tree_`、`size_idle_tree_`）。

构造函数——初始状态是「一整块空闲」：

- [src/host/mem/shmem_mgr.cpp:24-29](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L24-L29)：`address_idle_tree_[0] = size`、`size_idle_tree_.insert({0, size})`，即整个堆是一块从偏移 0 开始的空闲块。这也解释了为什么分配是确定性的：同样的调用序列在同样的初始状态下必然产生同样的偏移序列。

best-fit 主流程：

- [src/host/mem/shmem_mgr.cpp:36-74](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L36-L74)：`allocate`。L43 尺寸取整，L47 大小树 `lower_bound` 定位 best-fit 候选，L63-70 摘除候选、登记已用、尾部残余回挂，L73 返回 `base_ + target_offset`。

对齐分配与头部跳过：

- [src/host/mem/shmem_mgr.cpp:76-124](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L76-L124)：`aligned_allocate`。L83 校验 alignment 是 2 的幂；L94 循环跳过「对齐后装不下」的候选；L109-112 头部残余切块回挂。
- [src/host/mem/shmem_mgr.cpp:248-263](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L248-L263)：`alignment_matches`，head_skip 的计算：`aligned_offset = ceil(offset / alignment) × alignment`。

释放与前后合并：

- [src/host/mem/shmem_mgr.cpp:168-218](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L168-L218)：`release`。L190-203 找前邻（`prev.end == offset` 则合并），L205-212 找后邻（`offset + size` 处恰有空闲块则合并），L213-214 合并后的大块同时挂回两棵树。

16 字节取整：

- [src/host/mem/shmem_mgr.cpp:241-246](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mgr.cpp#L241-L246)：`allocated_size_align_up`，掩码取整实现。

#### 4.4.4 代码实践

**实践目标**：纸上推演分配器状态，理解「同序同大小 → 同偏移」的确定性来源。

**操作步骤**：

在纸上维护两棵空闲树（初始：`{[0, H)}`，H 为堆大小），依次执行：

```text
A = malloc(64)    // 提示：64 已是 16 的倍数
B = malloc(1)     // 注意取整为 16
C = align(4096, 100)
free(A)
D = malloc(32)
```

每一步记录：两棵空闲树的内容、`address_used_tree_` 的内容、各指针相对 `base_` 的偏移。

**需要观察的现象**：B 只占 16 字节；free(A) 后 D 会不会落进 A 腾出来的洞（best-fit 倾向复用最小合适的洞）？

**预期结果**（以初始堆无其他占用为前提）：A 在偏移 0、占 64；B 在偏移 64、占 16；C 从下一个对齐边界起；free(A) 后 `[0,64)` 成为空闲块，D（32 字节）因 best-fit 恰好落回偏移 0 的洞。在真实 SHMEM 程序里，堆头部还有模块三所述的内部占用，绝对偏移不同，但**相对规律一致**。

#### 4.4.5 小练习与答案

**练习 1**：`aclshmem_malloc(1)`、`malloc(16)`、`malloc(17)` 各占用多少堆空间？连续三次 `malloc(1)` 返回的指针偏移差是多少？

**答案**：分别占用 16、16、32 字节（17 向上取整到 32 的下界——即 ceil(17/16)×16 = 32）。连续三次 `malloc(1)` 各占 16 字节，偏移依次差 16。

**练习 2**：分配器为什么需要两棵空闲树，一棵不行吗？

**答案**：只用地址树，找 best-fit 需要 O(n) 全扫描；只用大小树，释放时的前后邻居合并无法 O(log n) 定位（大小树不按地址序）。两棵树各司其职：大小树做 best-fit 查找，地址树做邻居合并，均为 O(log n)。代价是每次插入/删除要同时维护两棵树（代码中成对出现 `emplace`/`erase`）。

**练习 3**：`change_size` 与 `allocated_size` 是公开方法却在 shmem_host_heap.h 里找不到对应 API，它们可能被谁使用？

**答案**：它们是内部工具（`shmemi_` 体系内部的查询/调整接口），不进入对外头文件。`allocated_size` 可用于内部校验指针合法性，`change_size` 支持原地扩缩。这体现了 u1-l3 总结的命名分层：对外 API 走 `aclshmem_`/`aclshmemx_` 前缀并集中在 include/host，内部能力留在 `src/host` 的类里。

### 4.5 模块五：HOST_SIDE——宿主内存对称堆与懒初始化

#### 4.5.1 概念说明

`mem_type = HOST_SIDE` 把对称堆放到 **host 侧锁页内存**，服务于 D2H/H2D 类数据通路（如 [examples/rma_d2h_demo/main.cpp:81](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/rma_d2h_demo/main.cpp#L81) 的用法）。两个要点：

- **能力门槛**：HOST_SIDE 依赖较新 CANN 的 `aclrtMemFabric` 系列接口，旧版本 CANN 上 `support_host_mem_type` 直接报错拒绝。
- **懒初始化**：device 堆在 init 时就 `setup_heap` 完成；host 堆只在 init 时 `reserve_heap(HOST_SIDE)` 预留，**真正的 setup 与分配器挂接推迟到第一次 HOST_SIDE malloc**（`getory_manager` 里完成）。这避免了不用 host 堆的程序白白付出建堆成本。

#### 4.5.2 核心流程

```text
aclshmemx_malloc(size, HOST_SIDE)
  ├─ support_host_mem_type?          ← 旧 CANN 直接失败
  ├─ getory_manager(HOST_SIDE)
  │    ├─ host 管理器已存在？→ 直接返回
  │    └─ 首次：setup_heap(HOST_SIDE) → memory_manager_initialize(host_heap_base, ...)
  │             → update_device_state 把 host 基址表同步给 kernel
  ├─ 分配器.allocate(size)
  └─ aclshmemi_control_barrier_all()  ← 同样是集体调用
```

#### 4.5.3 源码精读

- [src/host/mem/shmem_mm.cpp:169-178](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L169-L178)：`support_host_mem_type`，未定义 `USE_ACLRT_MEM_FABRIC_HANDLE`（旧 CANN）时对 HOST_SIDE 请求报错。
- [src/host/mem/shmem_mm.cpp:180-205](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L180-L205)：`getory_manager`（注意源码中的函数名即如此拼写）。HOST_SIDE 分支完成懒初始化四步：已有管理器直接复用 → 否则 `setup_heap(HOST_SIDE)` → `memory_manager_initialize` → `update_device_state` 把包含 host 基址表的状态重新镜像到 device（u4-l1 详述该机制）。DEVICE_SIDE 分支则要求 device 管理器已就绪。
- [src/host/mem/shmem_mm.cpp:207-227](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L207-L227)：`aclshmemx_malloc` 全貌——与标准 `aclshmem_malloc` 结构相同，只是管理器按 `mem_type` 获取，barrier 语义不变。
- [src/host/mem/shmem_mm.cpp:140-154](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L140-L154)：`aclshmemx_get_heap_base` 按 `mem_type` 分别返回 `g_state.host_heap_base` 或 `g_state.heap_base`，未初始化返回 NULL。
- 懒初始化的铺垫在初始化流程里：[src/host/init/shmem_init.cpp:1015-1018](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1015-L1018) 只对 host 堆做 `reserve_heap(HOST_SIDE)`，注释明确写着「setup_heap 留到 host 上 malloc 时再做」。

#### 4.5.4 代码实践

**实践目标**：通过源码阅读区分两种 `mem_type` 的初始化路径差异。

**操作步骤**：

1. 阅读 [examples/rma_d2h_demo/main.cpp:81](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/rma_d2h_demo/main.cpp#L81)，确认 HOST_SIDE 缓冲的申请方式。
2. 对照 [shmem_mm.cpp:180-205](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L180-L205)，写出「第一次 HOST_SIDE malloc」比「第一次 DEVICE_SIDE malloc」多出的三步。
3. 回答：host 堆与 device 堆的 `heap_size` 是同一个值吗？

**需要观察的现象 / 预期结果**：多出的三步是 `setup_heap(HOST_SIDE)`、`memory_manager_initialize(host_heap_base, g_state.heap_size, HOST_SIDE)`、`update_device_state`。两个堆使用同一个 `g_state.heap_size` 作为容量（见 [shmem_mm.cpp:190](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/mem/shmem_mm.cpp#L190)）。运行验证需 NPU + 新版 CANN 环境，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`aclshmem_malloc(1024)` 与 `aclshmemx_malloc(1024)`（不传第三参）有区别吗？

**答案**：语义等价——`mem_type` 默认 `DEVICE_SIDE`，`aclshmemx_malloc` 走 `getory_manager(DEVICE_SIDE)` 直接返回已就绪的 device 管理器，随后同样分配 + barrier。区别只在实现路径：标准版直接引用 `aclshmemi_memory_manager`，扩展版多一层 mem_type 分派，从而具备 HOST_SIDE 能力。

**练习 2**：为什么 host 堆要设计成懒初始化，而 device 堆不用？

**答案**：几乎所有 SHMEM 程序都会用 device 对称堆（put/get 的主战场），init 时建堆是必然成本；而 HOST_SIDE 只被 D2H/H2D 类场景使用，属于可选能力。懒初始化让不使用 host 堆的程序免去 setup_heap + 状态同步的开销，且旧 CANN 环境下也不会在 init 阶段就失败，把兼容性问题推迟到真正发起 HOST_SIDE 请求时才暴露。

## 5. 综合实践

**任务**：编写并运行一个两 PE 程序，验证「同序同大小 malloc ⇒ 堆内偏移一致」，并用 `aclshmem_ptr` 做交叉验证。这是本讲规格指定的核心实践。

**完整示例代码**（标注为示例代码，基于 [examples/rdma_demo/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/rdma_demo/main.cpp) 与 [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/examples/init/main.cpp) 的骨架裁剪，default 模式）：

```cpp
// 示例代码：heap_symmetry_check.cpp —— 验证对称堆偏移一致性
#include <cstdio>
#include <cstdint>
#include "acl/acl.h"
#include "shmem.h"

static void report(const char *tag, void *ptr, int pe)
{
    void *base = aclshmemx_get_heap_base();   // 本地堆基址
    uint64_t offset = (uint64_t)((uint8_t *)ptr - (uint8_t *)base);
    printf("[pe %d] %-4s base=%p ptr=%p offset=0x%lx\n", pe, tag, base, ptr, offset);
}

int main(int argc, char *argv[])
{
    // run.sh 约定：argv = 程序名 n_pes pe_id ip_port g_npus f_pe f_npu
    int n_pes = atoi(argv[1]);
    int pe_id = atoi(argv[2]);
    const char *ipport = argv[3];

    aclInit(nullptr);
    aclrtSetDevice(pe_id);

    aclshmemx_init_attr_t attributes;
    test_set_attr(pe_id, n_pes, 1024UL * 1024 * 1024, ipport, {}, &attributes); // 参考示例的填充函数
    if (aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_DEFAULT, &attributes) != 0) {
        return -1;
    }

    // 关键：两个 PE 以相同顺序、相同大小分配
    void *bufA = aclshmem_malloc(64);
    void *bufB = aclshmem_malloc(128);
    void *bufC = aclshmem_align(4096, 512);

    report("bufA", bufA, pe_id);
    report("bufB", bufB, pe_id);
    report("bufC", bufC, pe_id);

    // 交叉验证：把 bufA 翻译到对端，比对「对端基址 + 相同偏移」
    int peer = (pe_id + 1) % n_pes;
    void *peerA = aclshmem_ptr(bufA, peer);
    uint64_t offA = (uint64_t)((uint8_t *)bufA - (uint8_t *)aclshmemx_get_heap_base());
    printf("[pe %d] peer=%d peerA=%p (expect peer_base + 0x%lx)\n", pe_id, peer, peerA, offA);

    // 数据侧自证：PE0 写、PE1 读（借 aclrtMemcpy 经 host 中转读远端不可行，
    // 这里用「写本地 + 对端读」留到 u3-l1 的 put/get 再完成，本讲只验证地址层）
    aclshmem_free(bufC);
    aclshmem_free(bufB);
    aclshmem_free(bufA);

    aclshmem_finalize();
    aclrtResetDevice(pe_id);
    aclFinalize();
    return 0;
}
```

**操作步骤**：

1. 把文件放入 `examples/` 下新建目录，仿照 `examples/rdma_demo` 的 `CMakeLists.txt` 与 `run.sh` 配置（default 模式两进程）。
2. 运行 `run.sh`，收集两个 PE 的输出。
3. 检查三项：① 两个 PE 的三个 offset 分别相等；② `bufB.offset − bufA.offset = 0x40`（64 字节）；③ PE0 打印的 `peerA` 与 PE1 打印的 `bufA` 地址应满足「同一偏移、不同基址」的关系（`peerA` 是对端地址在本进程视角的映射，与 PE1 自己打印的 `bufA` 数值是否相同取决于引擎映射方式，重点看偏移一致性）。
4. 进阶：交换 `bufA`/`bufB` 的大小（64↔128）重新运行，观察 offset 如何随之改变，体会「顺序也参与决定偏移」。
5. 注意收尾顺序：先 free 再 finalize，且 free/finalize 在所有 PE 上对称执行（都是集体调用）。

**需要观察的现象**：两 PE 输出的 offset 逐字节一致；offset 明显非零（模块三的内部占用）；`aclshmem_ptr` 对相邻 PE 返回非空地址。

**预期结果**：地址层对称性成立。若第 ③ 项中两进程打印的地址数值不同，不代表失败——只要「ptr − 各自 base」的偏移一致，对称语义就成立。数据写入/读出的端到端验证依赖 put/get 接口，将在 u3-l1 完成闭环。本实践需要真实 NPU 环境，**待本地验证**。

## 6. 本讲小结

- 堆 API 分两层：`aclshmem_` 四件套只操作 device 堆；`aclshmemx_` 四件套多一个 `mem_type`（`HOST_SIDE` / `DEVICE_SIDE`），另有 `aclshmemx_get_heap_base` 查询基址——共 9 个对外接口。
- 对称性的本质是**偏移一致**：每个 rank 的堆被 `ALIGN_UP(heap_size, 1 GiB)` 为步长排进本地 GVA 窗口（`p2p_device_heap_base[i] = gva + aligned × i`），远端地址 = 对应引擎基址表[pe] + 本地堆内偏移。
- `malloc / calloc / align / free` 都是**集体调用**：每次操作内嵌一次控制面 barrier，保证各 PE 的第 k 次堆操作一一对齐；`if (my_pe == 0) malloc(...)` 是典型错误，会导致全组阻塞或偏移错位。
- DEBUG 构建下 `is_alloc_size_symmetric` 用一次 allgather 即可定位「哪个 PE 分得不一样」。
- 堆在用户使用前并非空仓：初始化期间库自己已对称分配了 signal（512 B）、sync pool（5 MiB）、sync counters（128 KiB），用户第一次 malloc 的偏移约为 0x520200。
- `memory_manager` 是纯本地的 best-fit 双树分配器（地址树管合并、大小树管查找、16 字节粒度、自旋锁保护），确定性分配是「同序同大小 ⇒ 同偏移」能够成立的技术根基。
- HOST_SIDE 堆依赖新版 CANN 的 fabric 接口，采用懒初始化：第一次 HOST_SIDE malloc 才 setup + 挂分配器 + 同步 device 状态。

## 7. 下一步学习建议

本讲回答了「堆怎么用、对称性怎么保证」，但刻意绕开了两个更深的机制：

1. **u2-l5（HYBM 混合内存与 slice 交换机制）**：`setup_heap` 里「分配物理 slice → 控制面交换 → mmap 到其他 rank」的具体实现，即本讲模块二中 GVA 窗口映射是如何真正建立起来的。推荐先读 `src/host/mem/heap/hybm_mem_segment.cpp` 与 `hybm_mem_slice.cpp`。
2. **u3-l1（Host 侧 RMA）**：本讲综合实践中悬而未决的数据闭环——用 `aclshmem_putmem / getmem` 真正在两个 PE 的对称缓冲之间搬数据并校验。

巩固建议：把本讲的破坏性实验（不对称分配）在 DEBUG 构建下跑一次，亲眼看一次 `Asymmetric alloc size detected` 日志，对集体调用语义的记忆会深刻得多。
