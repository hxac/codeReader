# FabricMem 内存体系：分配、槽位与虚拟内存

## 1. 本讲目标

上一讲（u5-l1）我们建立了 FabricMem 模式的整体地图：FabricMemEngine 是薄门面，编排 TransferService、LocalMemory、ControlServer、Statistic 四个组件。本讲向下钻一层，钻到「内存」本身，学完本讲你应该能够：

1. 说清 CANN VMM「申请物理内存 → 预留虚拟地址 → 建立映射」三步机制在 HIXL 源码中的具体落点。
2. 解释 `FabricMemAllocator` 如何分配 host/device 两类 FabricMem 物理内存，以及 host 内存的三级降级策略。
3. 描述一次 `RegisterMem` 从引擎入口到共享句柄导出/导入的完整调用链（本讲的综合实践任务）。
4. 掌握 `FabricMemSlotPool` 的槽位生命周期：创建、获取（带超时）、归还、失败闩锁式销毁。
5. 理解 `VirtualMemoryManager` 用 1GB 粒度位图管理 32TB 虚拟地址空间的第一适应分配算法。

## 2. 前置知识

- **VMM（Virtual Memory Management，虚拟内存管理）**：CANN 提供的一组运行时接口（`aclrtMallocPhysical` / `aclrtReserveMemAddress` / `aclrtMapMem` 等），允许把「物理内存」与「虚拟地址」解耦：先申请一块物理内存拿到不透明句柄 `aclrtDrvMemHandle`，再在进程虚拟地址空间预留一段地址，最后把两者映射起来。这是 FabricMem 统一编址的底层前提（回顾 u5-l1 的三步机制）。
- **共享句柄（ShareHandle）**：`aclrtMemExportToShareableHandleV2` 把本进程的物理内存句柄导出成一个可跨进程传递的 `aclrtMemFabricHandle`；对端用 `aclrtMemImportFromShareableHandleV2` 导入后，就能把同一块物理内存映射到自己进程的虚拟地址空间，实现零拷贝共享。
- **锁页内存（pinned memory）**：host 侧不被操作系统换出的物理内存。DMA 引擎（如 SDMA）只能直接访问锁页内存，`ACL_MEM_ALLOCATION_TYPE_PINNED` 就是这个语义。
- **NUMA**：多路服务器的主机内存按 CPU 节点分组。设备侧访问「就近 NUMA 节点」的主机内存延迟更低，FabricMemAllocator 会按物理设备号推导 NUMA 节点。
- **异步槽位（AsyncSlot）**：一次异步传输所需的全部运行时资源的集合体——context、若干 stream、notify、host 完成标志。槽位池复用它们，避免每次传输都走一遍昂贵的 ACL 创建/销毁。
- 建议先回顾 u5-l1 中 FabricMemEngine 的组件图，以及 u2-l3 中「注册内存是零拷贝前提」的一般性结论——FabricMem 模式下这条结论同样成立，但实现完全不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/hixl/fabric_mem/virtual_memory_manager.cc` | 进程级单例，管理一大段预留虚拟地址空间，用 1GB 位图为每次分配划分连续区间 |
| `src/hixl/fabric_mem/fabric_mem_allocator.cc` | 物理内存分配器：host/device 两类内存的 VMM 三步分配，维护 VA→PA 句柄映射表 |
| `src/hixl/fabric_mem/fabric_mem_memory.cc` | 本端内存注册（导出共享句柄）与远端内存导入；host 内存的本端重映射与传输地址翻译 |
| `src/hixl/fabric_mem/fabric_mem_slot_pool.cc` | 异步传输槽位池：创建/获取/归还/中止，槽位内含 context、stream、notify、host flag |
| `src/hixl/fabric_mem/fabric_mem_types.h` | `ShareHandleInfo`、`AsyncSlot`、`AsyncRecord` 等核心结构体定义 |
| `src/hixl/fabric_mem/fabric_mem_slot_pool.h` | 槽位池接口与并发契约的注释文档 |
| `src/hixl/engine/fabric_mem_engine.cc` | 引擎入口：`RegisterMem` 转发到 `FabricMemLocalMemory` |
| `src/hixl/fabric_mem/fabric_mem_transfer_service.cc` | 槽位池的初始化参数来源（`max_stream_num` 折算 `max_async_slot_num`） |

## 4. 核心概念与源码讲解

本讲按「自底向上」的顺序讲解四个最小模块：先讲虚拟地址空间管家（VirtualMemoryManager），再讲物理内存分配器（FabricMemAllocator），然后讲建立在其上的注册/导入机制（FabricMemLocalMemory / FabricMemRemoteMemory），最后讲与内存并列的传输资源池（FabricMemSlotPool）。

### 4.1 VirtualMemoryManager：进程级虚拟地址空间管家

#### 4.1.1 概念说明

VMM 三步机制中的第二步「预留虚拟地址」如果每次都直接调 ACL 接口，既慢又碎片化。HIXL 的做法是**一次性预留一大段虚拟地址空间**（默认 32TB），之后每次分配/导入内存时，从这段空间里用位图切一段出来，纯用户态操作，不再触达驱动。`VirtualMemoryManager` 就是这段空间的管理者，它是进程级单例，被 allocator 和 memory 模块共享。

#### 4.1.2 核心流程

初始化与分配的流程：

```text
VirtualMemoryManager::GetInstance()  （懒加载单例）
        │
        ├─ Initialize / 首次 ReserveMemory 触发 InitProcess
        │     ├─ vm_size_ 未设置则取默认 32TB（kBlockSize=1GB × 32768 块）
        │     ├─ aclrtReserveMemAddress 一次性预留整段（A3 走 NoUCMemory 变体）
        │     └─ bitmap_.assign(num_blocks_, false)
        │
        └─ ReserveMemory(size, mem_addr)   （互斥锁内，first-fit 首次适应）
              ├─ 向上取整 blocks_needed = ⌈size / 1GB⌉
              ├─ 从低到高扫描 bitmap 找连续空闲块
              ├─ 全部置 true，记录 allocations_[mem_addr] = blocks_needed
              └─ mem_addr = 基址 + start_block × 1GB

ReleaseMemory(mem_addr) 逆向：查 allocations_ 还原块数，位图清 false。
```

所需块数的计算刻意用除法加取模判断，而不是 `(size + kBlockSize - 1) / kBlockSize`：

\[ \text{blocks\_needed} = \left\lfloor \frac{\text{size}}{\text{1GB}} \right\rfloor + \begin{cases} 1, & \text{size} \bmod \text{1GB} \neq 0 \\ 0, & \text{否则} \end{cases} \]

这样 `size` 接近 `SIZE_MAX` 时不会因加法回绕得到一个极小的块数、绕过容量检查。

#### 4.1.3 源码精读

空间规模与默认值：1GB 一块、默认 32768 块共 32TB，基址默认在第 40TB 处（`kBlockSize * 1024 * 40`），见 [virtual_memory_manager.cc:L25-L30](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L25-L30)：定义了 `kBlockSize`、`kDefaultNumBlocks`、`kDefaultGlobalVirtualMemorySize`、`kGlobalVirtualMemoryStartAddr` 与 `kReserveFlagHugePage`（大页标志）。

预留入口按 SoC 分派：见 [virtual_memory_manager.cc:L78-L94](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L78-L94)。A3 芯片优先调用 `aclrtReserveMemAddressNoUCMemory`（在指定的 `global_start_va` 处预留且不带 UC 内存），返回 `ACL_ERROR_RT_FEATURE_NOT_SUPPORT` 时才回退到通用的 `aclrtReserveMemAddress`（让驱动自选地址）。这是换芯片代际的显式适配点，与 u4-l4 的 NotifyAddrResolver 白名单是同一类设计。

初始化主体：见 [virtual_memory_manager.cc:L105-L117](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L105-L117)，`InitProcess` 预留整段空间、记录基址、按块数初始化全 false 位图。

first-fit 分配：见 [virtual_memory_manager.cc:L134-L171](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L134-L171)。`ReserveMemory` 先做防回绕的向上取整与容量检查（超出返回 `RESOURCE_EXHAUSTED`），再线性扫描位图找第一段足够长的连续空闲块，置 true 并把「起始地址 → 块数」记入 `allocations_` 表。

释放校验：见 [virtual_memory_manager.cc:L173-L191](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L173-L191)。`ReleaseMemory` 依次校验已初始化、地址在管辖范围内、1GB 对齐、确在 `allocations_` 表中（防重复释放），然后清位图。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证 32TB 容量的来源与配置覆盖方式。
2. **操作步骤**：
   - 打开 [virtual_memory_manager.cc:L25-L28](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L25-L28)，手算 `kBlockSize * kDefaultNumBlocks` 确认默认容量。
   - 再看 [virtual_memory_manager.cc:L48-L60](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.cc#L48-L60) 的 `SetVirtualMemoryCapacity`（单位 TB），用 Grep 在 `src/hixl/` 下搜索谁调用了它，确认配置注入路径。
3. **需要观察的现象**：`initialized_` 为 true 后再改容量会怎样（答案是只有「请求值恰好等于当前值」才返回 SUCCESS，否则 `PARAM_INVALID`——容量在首次使用后不可变）。
4. **预期结果**：容量 = 1GB × 32768 = 32TB；起始地址默认 40TB 处；两者均可在初始化前由配置覆盖。待本地验证（可在有环境时用日志 `"Set virtual memory capacity"` 观察实际取值）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ReserveMemory` 必须找「连续」空闲块，而不是任意分散的块？
**答案**：因为返回的 `mem_addr` 是单一虚拟地址，后续 `aclrtMapMem` 要把一块物理内存映射到 `[mem_addr, mem_addr + size)` 这段连续虚拟区间；虚拟区间必须连续，对应的位图块也就必须连续。

**练习 2**：`ReleaseMemory` 传入一个从未分配过的地址会发生什么？
**答案**：依次经过范围校验、1GB 对齐校验后，`allocations_.find` 找不到该地址，返回 `PARAM_INVALID` 并报 "not allocated or already released"，不会误清位图。

**练习 3**：位图分配器用 first-fit 而不是 best-fit，会带来什么权衡？
**答案**：first-fit 实现简单、有 `break` 提前退出、速度快，但长期多次分配/释放后低地址端会产生碎片；由于块粒度固定 1GB 且 FabricMem 场景注册次数通常不多，碎片风险可接受。这是用简单性换性能的典型取舍。

### 4.2 FabricMemAllocator：物理内存三步分配

#### 4.2.1 概念说明

`FabricMemAllocator` 是一个纯静态工具类（无需实例化），把「VMM 三步」封装成两个入口：`MallocMem`（分配）与 `FreeMem`（释放）。它同时维护一张进程级的「虚拟地址 → 物理句柄」映射表 `g_va_to_pa_handle_map`，供 `FreeMem` 反查，也供后续注册流程判断「这块内存是否由本分配器分配」。

#### 4.2.2 核心流程

`MallocMem(type, size, ptr)` 的流程（每步失败都有 scope guard 回滚前面步骤）：

```text
1. 校验：type ∈ {MEM_HOST, MEM_DEVICE}，size > 0
2. aclrtGetDevice 取当前逻辑设备号
3. AllocatePhysicalMemory：申请物理内存 → pa_handle
   ├─ MEM_DEVICE：ACL_HBM_MEM_HUGE + location=DEVICE(device_id)
   └─ MEM_HOST：三级降级尝试
        ├─ ① P2P_HUGE1G + HOST_NUMA(按物理设备号推导的节点)   ← 最优
        ├─ ② P2P_HUGE1G + HOST(普通主机内存)                   ← NUMA 失败
        └─ ③ P2P_HUGE  + HOST(更小页)                          ← 1G 大页失败
4. VirtualMemoryManager::ReserveMemory(size) → virtual_addr   ← 预留 VA
5. aclrtMapMem(va, size, 0, pa_handle, 0)                     ← 建立映射
6. 若 MEM_HOST：aclrtMemSetAccess 授予 DEVICE READWRITE 权限
   （否则 NPU 无法访问主机 VA）
7. 登记 g_va_to_pa_handle_map[va] = pa_handle；*ptr = va
```

`FreeMem(ptr)` 严格逆序：查映射表取 `pa_handle` → 移除映射 → `aclrtUnmapMem` → `ReleaseMemory` 释放 VA → `aclrtFreePhysical` 释放物理内存。

host 内存 NUMA 节点推导规则：`location.id = (physical_device_id / 4) * 2`，即每 4 个设备（一个芯片）共享一个 NUMA 步进为 2 的节点号（对应 `kDevicesPerChip = 4`、`kNumaNodeStep = 2`）。

#### 4.2.3 源码精读

VA→PA 映射表与设备访问授权函数：见 [fabric_mem_allocator.cc:L23-L49](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L23-L49)。匿名命名空间内定义映射表 `g_va_to_pa_handle_map`（带专用互斥锁）和 `SetDeviceAccessForHostMappedVa`——host VA 的 VMM 映射默认只授权 host 侧，这里补一条 `ACL_RT_MEM_ACCESS_FLAGS_READWRITE` + `ACL_MEM_LOCATION_TYPE_DEVICE` 的访问描述，让 NPU 也能读写该区间（注释说明这是对齐 memfabric 的 `HalMemSetAccess` 行为）。

三步分配主体：见 [fabric_mem_allocator.cc:L52-L87](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L52-L87)。注意三个 `HIXL_DISMISSABLE_GUARD`（free_pa / release_va / unmap）依次保护已完成的步骤，全部成功后才逐个 `HIXL_DISMISS_GUARD` 解除，任何一步失败自动逆序回滚——这是「不留半初始化状态」的惯用法，与 u2-l1 中 `Hixl::Initialize` 的回滚思路一致。

host 内存三级降级：见 [fabric_mem_allocator.cc:L103-L138](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L103-L138)。`AllocatePhysicalMemory` 先用 `aclrtGetPhyDevIdByLogicDevId` 把逻辑设备号翻译为物理设备号并推导 NUMA 节点；NUMA 分配失败依次尝试普通 host 内存、更小页尺寸，每次降级都打日志，最终仍失败才返回错误。device 分支则是简单的 HBM 大页申请。

释放与反查：见 [fabric_mem_allocator.cc:L89-L101](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L89-L101) 与 [fabric_mem_allocator.cc:L140-L148](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L140-L148)。`GetPaHandleFromVa` 是注册流程的关键判断点：查到说明内存出自本分配器（handle 已知），查不到则注册时需走 `aclrtMemRetainAllocationHandle` 引用外部内存。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「三步」与「回滚」的对应关系。
2. **操作步骤**：
   - 对照 [fabric_mem_allocator.cc:L63-L74](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_allocator.cc#L63-L74)，把每个 ACL 调用与其 guard 配对成表：`AllocatePhysicalMemory`↔`free_pa_guard`、`ReserveMemory`↔`release_va_guard`、`aclrtMapMem`↔`unmap_guard`。
   - 思考：若 `aclrtMapMem` 失败，会依次执行哪些清理？（提示：guard 析构逆序。）
3. **需要观察的现象**：无硬件环境纯读代码即可完成；有环境时可运行 `examples/cpp/fabric_mem_d2d.cpp`，在日志中找 `"MallocFabricMemory success, va:..."` 与 `"Malloc host memory for numa:..."`。
4. **预期结果**：`aclrtMapMem` 失败时依次执行 unmap_guard（空操作，因为 map 已失败——但 guard 逻辑仍会尝试 unmap 已预留的 VA，注释层面实际会调 `aclrtUnmapMem`；随后释放 VA、释放物理内存，最终返回错误且无泄漏。注：unmap 失败路径的精确行为待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MEM_HOST 分配后必须额外 `SetDeviceAccessForHostMappedVa`，而 MEM_DEVICE 不需要？
**答案**：VMM 的 host 侧映射默认只授予 CPU 访问权限；FabricMem 的目标是让 NPU 直接经 HCCS 读写远端 DRAM，所以必须显式给 device 加 READWRITE 授权。device 内存（HBM）本身就在设备地址空间内，映射后设备天然可访问。

**练习 2**：`FreeMem` 为什么不能直接调 `aclrtFreePhysical(ptr)`？
**答案**：`ptr` 是虚拟地址，而 `aclrtFreePhysical` 需要物理句柄 `aclrtDrvMemHandle`。必须先经 `g_va_to_pa_handle_map` 反查句柄，并且释放前要先 unmap VA、归还虚拟区间，最后才能释放物理内存——顺序颠倒会留下悬空映射。

**练习 3**：三级降级策略中为什么把「NUMA + 1G 大页」放在第一优先级？
**答案**：NUMA 就近让设备访问 host 内存延迟最低、带宽最高；1G 大页减少页表项、降低 TLB miss。两者都是性能最优选项，但受主机配置（NUMA 拓扑、大页储备）限制，所以才需要向后降级。

### 4.3 FabricMemLocalMemory / FabricMemRemoteMemory：注册、导出与导入

#### 4.3.1 概念说明

有了分配器和虚拟地址管家，`fabric_mem_memory.cc` 解决的问题是「如何让远端进程访问本端内存」。`FabricMemLocalMemory` 管本端注册：把任意一块（本分配器分配的或外部的）内存导出为 Fabric 共享句柄，并把句柄登记进 `share_handles_` 台账；`FabricMemRemoteMemory` 管远端导入：拿到对端发来的共享句柄列表后，逐个导入并映射到本进程新的虚拟地址。注意这里**没有 CS 层的内存授权校验**（对比 u2-l3 的 HixlMemStore）——控制面由 FabricMem 自己的 ControlServer 承担（u5-l1），地址翻译在传输前完成。

#### 4.3.2 核心流程

本端注册 `RegisterMem(mem, type, mem_handle)`：

```text
1. 查重叠：FindExistingHandleForOverlap 用 CheckAddrOverlap 比对已注册区间
   └─ 完全重复 → 直接返回已有 handle（幂等）
2. 取物理句柄：
   ├─ GetPaHandleFromVa 命中 → 内存由 FabricMemAllocator 分配，handle 已知
   └─ 未命中 → aclrtMemRetainAllocationHandle 引用外部内存（is_retained = true）
3. aclrtMemExportToShareableHandleV2(pa_handle, ..., FABRIC, &share_handle)
   └─ 带 DISABLE_PID_VALIDATION 标志：允许跨进程导入
4. 若 MEM_HOST：ImportHostMemoryForRegister
   ├─ VirtualMemoryManager::ReserveMemory(len) 预留本端第二段 VA
   ├─ aclrtMemImportFromShareableHandleV2 导入自己的共享句柄
   └─ aclrtMapMem 把导入句柄映射到新 VA
   （host 注册内存需要一个「fabric 视角」的本端地址，供传输时替换 local_addr）
5. 登记 share_handles_[pa_handle] = {va, len, share_handle, imported_*, is_retained, type}
6. mem_handle = pa_handle
```

远端导入 `Import(remote_share_handles, device_id)`：对每个句柄重复「ReserveMemory 新 VA → Import → MapMem」，建立 `new_va_to_old_va_` 新旧地址映射表；此后传输侧把 op 中的**远端原始地址**翻译为**本进程映射地址**再下发 SDMA。

#### 4.3.3 源码精读

共享句柄台账条目 `ShareHandleInfo`：见 [fabric_mem_types.h:L36-L44](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h#L36-L44)。字段含原始 VA 与长度、导出的 `share_handle`、host 重导入产生的 `imported_handle`/`imported_va`、是否引用外部内存的 `is_retained`、内存类型。

注册主体：见 [fabric_mem_memory.cc:L98-L140](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L98-L140)。按上面流程实现，重叠检查幂等返回、retain 外部内存、导出共享句柄、host 内存额外本端重导入，最后入台账并回填 `mem_handle`。失败清理函数 `CleanupRegisterMemFailure` 见 [fabric_mem_memory.cc:L42-L55](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L42-L55)。

host 内存重导入：见 [fabric_mem_memory.cc:L62-L73](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L62-L73)。这是 FabricMem 的一个精妙细节：把本端 host 内存的共享句柄**再导入回本进程**，得到一段带 fabric 语义的新 VA——传输时用这个地址，NPU 侧才能走 Fabric 统一编址路径。

传输前地址翻译：见 [fabric_mem_memory.cc:L195-L202](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L195-L202) 与 [fabric_mem_memory.cc:L179-L193](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L179-L193)。`TranslateLocalHostOpAddrs` 把每条 op 的 `local_addr` 在锁内替换为 host 重导入 VA 加偏移；未注册的地址直接 `PARAM_INVALID`——这与 u2-l3 的结论一致：注册是传输前的安全闸。

远端导入：见 [fabric_mem_memory.cc:L226-L260](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L226-L260)。`FabricMemRemoteMemory::Import` 循环处理每个远端句柄，任何一步失败由 `fail_guard` 触发 `ClearLocked` 全量回滚（保证导入要么全部成功要么干净失败）。

解注册逆操作：见 [fabric_mem_memory.cc:L142-L163](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_memory.cc#L142-L163)。按台账记录逆序清理：unmap 本端 host 映射、释放导入句柄、仅当 `is_retained` 时才 free 物理句柄（分配器分配的内存由调用方自己 FreeMem 释放，注册方只管引用）。

引擎入口：见 [fabric_mem_engine.cc:L193-L199](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L193-L199)，`FabricMemEngine::RegisterMem` 仅做参数检查后转发 `local_memory_.RegisterMem`，是典型的薄门面（u5-l1 结论的代码印证）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：追踪一次 FabricMem 内存注册的关键函数调用链（本讲综合实践的预热，见第 5 节）。
2. **操作步骤**：
   - 从 [fabric_mem_engine.cc:L193-L199](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L193-L199) 出发，沿 `FindExistingHandleForOverlap → GetPaHandleFromVa / aclrtMemRetainAllocationHandle → aclrtMemExportToShareableHandleV2 → ImportHostMemoryForRegister` 的顺序逐个函数阅读。
   - 用 Grep 搜索 `GetShareHandles` 的调用方，确认共享句柄列表是经哪条控制面路径发给对端的（提示：ControlServer / channel manager）。
3. **需要观察的现象**：记录每一步的输入输出（输入 `MemDesc{addr, len}`，中间产物 `pa_handle`、`share_handle`、`imported_va`，输出 `MemHandle`）。
4. **预期结果**：得到一条完整调用链笔记；注册 MEM_HOST 比 MEM_DEVICE 多出「本端重导入」一步。控制面传递细节待确认（涉及 u5-l3 的 channel manager 内容）。

#### 4.3.5 小练习与答案

**练习 1**：`RegisterMem` 对同一块内存注册两次，第二次会发生什么？
**答案**：`FindExistingHandleForOverlap` 检测到与已注册区间重叠（完全一致），`is_duplicate` 为 true，直接返回第一次的 handle，不重复导出——幂等语义，与 u2-l3 HixlServer 的幂等注册行为对齐。

**练习 2**：`is_retained` 标志为什么必须记进台账？
**答案**：注册可能针对两类内存：分配器分配的（物理句柄归分配器生命周期管理）和外部内存（注册时用 `aclrtMemRetainAllocationHandle` 增加了引用）。解注册时只有后者需要 `aclrtFreePhysical` 归还引用，否则要么泄漏引用、要么误释放他人的内存。

**练习 3**：远端导入后 `new_va_to_old_va_` 映射表在传输时如何使用？
**答案**：用户下发的 `TransferOpDesc.remote_addr` 是对端进程的原始 VA，本进程无法直接访问；传输服务先查该表把原始 VA 翻译成本进程导入映射后的新 VA，SDMA 用新 VA 访问；host 侧本端地址则由 `TranslateLocalHostOpAddrs` 翻译。两端都翻译完才能下发传输。

### 4.4 FabricMemSlotPool：传输槽位池

#### 4.4.1 概念说明

内存解决了「数据放哪」，槽位解决「传输用什么跑」。一次 FabricMem 异步传输需要一组运行时资源：独立的 `aclrtContext`、若干条控制流 stream（AICPU unfold 模式还有配对的 worker RTSQ stream 与 device-only notify）、每流一个 8 字节 host 完成标志。这些资源的 ACL 创建/销毁都很昂贵，`FabricMemSlotPool` 把它们打包成 `AsyncSlot` 池化复用：传输开始时获取，完成后归还，失败时闩锁式销毁（不归还池中）。

#### 4.4.2 核心流程

槽位生命周期状态流转：

```text
                ┌──────────────────────────────────────────┐
                │            slot_pool_（vector）           │
                │   entry.available == true → 在空闲队列    │
                │   entry.available == false → 已被借出     │
                └──────────────────────────────────────────┘
     AcquireWithTimeout(slot, timeout_us)        Release(slot, destroy=false)
  ├─ 有空闲 → 取出，Reset host_flags → 归还：available=true，notify_one 唤醒等待者
  ├─ 无空闲但未满 → CreateSlotEntryLocked 新建   Release(slot, destroy=true)
  ├─ 池满 → 条件变量等待直至超时(TIMEOUT)         → DestroySlotEntryLocked(abort) 并从池中移除
  └─ 新建失败（ACL 错误）→ 立即返回，不空转重试

失败路径 AbortSlot(slot, requests)：（头文件注释规定的固定五步）
  1. abort 控制流 stream + stop device-only RTSQ worker stream
  2. 删除 AICPU TransferContext，让仍在内核里的 kernel 退出
  3. 销毁槽位的 stream/notify（而不是归还池）
  4. 释放请求的描述符/状态缓冲与 per-transfer host flag
  5. 销毁槽位 context
```

并发契约：所有状态变更都在 `pool_mutex_` 内完成；池满时等待者经 `pool_cv_` 被唤醒；「新建失败」与「池满」区分对待——前者立即返回错误，后者才等待（避免对 ACL 调用做无意义的忙重试）。

#### 4.4.3 源码精读

槽位结构 `AsyncSlot`：见 [fabric_mem_types.h:L46-L65](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h#L46-L65)。注释解释了控制流与 worker RTSQ 的配对关系、notify 的桥接作用，以及 `transfer_ctx_key`、`owns_host_flags` 等字段的语义。

类契约文档：见 [fabric_mem_slot_pool.h:L27-L31](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.h#L27-L31) 与 [fabric_mem_slot_pool.h:L50-L58](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.h#L50-L58)。头文件注释明确「获取到的是池内条目的视图（view），槽位本体由池拥有并复用」，以及 AbortSlot 五步的顺序约束与原因（内核还在读描述符缓冲，必须先让内核退出再释放缓冲）。

槽位创建：见 [fabric_mem_slot_pool.cc:L197-L206](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L197-L206)。`CreateSlotEntryLocked` 先建 context，再用 `TemporaryRtContext` 切入该 context 创建 stream 与 host flag（保证资源归属于槽位自己的 context），全部成功才置 `available = true`。stream 用 `ACL_STREAM_FAST_LAUNCH | ACL_STREAM_FAST_SYNC` 配置创建（[fabric_mem_slot_pool.cc:L64-L76](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L64-L76)），并设 `ACL_STOP_ON_FAILURE` 失败模式。

获取的三分支：见 [fabric_mem_slot_pool.cc:L225-L261](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L225-L261)。`TryAcquireSlotLocked` 优先复用空闲条目（借出前 `ResetSlotHostFlags` 清掉上一次传输残留的完成值——host flag 是池内复用资源），否则在未满时新建；池满返回 FAILED 由上层决定等待。

带超时获取：见 [fabric_mem_slot_pool.cc:L276-L301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L276-L301)。`AcquireWithTimeout` 的循环里区分「资源创建失败（立即返回）」与「池满（条件变量等待 + 超时返回 TIMEOUT）」，注释说明了为什么不把 ACL 创建错误忙等到超时。

归还与销毁：见 [fabric_mem_slot_pool.cc:L361-L393](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L361-L393)。`Release` 按 ctx 匹配池内条目：普通归还只置 `available` 并入空闲队列；`destroy_slot` 为 true 时 abort stream 后整体销毁。`ClearReleasedSlot` 只清视图引用，印证「槽位本体由池拥有」。

失败中止：见 [fabric_mem_slot_pool.cc:L464-L488](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L464-L488)。`AbortSlot` 实现头文件规定的五步：先 `DetachSlotEntry` 把条目从池里摘除（防止中止期间被他人获取），再 abort/stop stream、删除 AICPU TransferContext、释放请求资源、最后销毁 context。

池容量来源：见 [fabric_mem_transfer_service.cc:L88-L107](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L88-L107)。`max_async_slot_num = max_stream_num / streams_per_slot`，且 AICPU unfold 模式下每槽位 stream 数翻倍（控制流 + worker RTSQ），池相应减半——注释解释了这条折算规则。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：理解池满时传输请求的行为。
2. **操作步骤**：
   - 阅读 [fabric_mem_slot_pool.cc:L276-L301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L276-L301)，找出超时返回的错误码与日志关键字（"Get fabric mem transfer slot timed out"）。
   - 用 Grep 搜索 `AcquireWithTimeout` 的调用方，记录传入的 `timeout_us` 来自哪里。
3. **需要观察的现象**：推导「并发异步传输数超过 `max_async_slot_num` 时」第 N+1 个请求的时间行为（阻塞等待而不是失败，除非超时）。
4. **预期结果**：等待者在条件变量上挂起，某个在传传输 Release 后被唤醒；超过超时阈值返回 `TIMEOUT`。具体超时配置值待确认（属 u5-l3 传输服务内容）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 host_flags 借出前要 `ResetSlotHostFlags` 清零？
**答案**：host flag 是 8 字节完成标志，池化复用意味着上一次传输写入的完成值还留在里面；若不清零，新传输可能把旧值误判为「已完成」，造成提前返回、数据未就绪的正确性事故。

**练习 2**：AICPU 的 worker RTSQ stream 销毁时为什么用 `aclrtStreamStop` 而不是 `aclrtStreamAbort`？
**答案**：RTSQ stream 以 `ACL_STREAM_DEVICE_USE_ONLY` 配置创建，这种流不支持 `aclrtSetStreamFailureMode`（代码注释记录了 207000 错误），同样也不支持 abort，只能 stop。[fabric_mem_slot_pool.cc:L78-L84](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L78-L84) 与 [fabric_mem_slot_pool.cc:L405-L410](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_slot_pool.cc#L405-L410) 两处注释都强调了这一点。

**练习 3**：`AbortSlot` 为什么必须在释放描述符缓冲之前先删除 AICPU TransferContext？
**答案**：还可能在跑的 AICPU 内核持有该 TransferContext 并读取描述符缓冲；先 DeleteTransferContext 迫使内核退出（步骤 2），确认内核不再触碰缓冲后才能 free（步骤 4），否则内核会读到已释放内存。这就是头文件注释里 "stop the kernel before freeing its buffers" 规则。

## 5. 综合实践

**任务：写出一次 FabricMem 内存注册从 allocator 到 slot pool 的关键函数调用链，并标注每步的资源产物。**

结合 `examples/cpp/fabric_mem_d2d.cpp`（若可在 A3 环境运行，则先跑通该样例；无环境则纯源码追踪，标注待本地验证），完成下面三张产出：

1. **注册链路图**（覆盖 4.2、4.3 模块）：

```text
用户代码 Hixl::RegisterMem(MemDesc{addr, len}, MEM_HOST/MEM_DEVICE)
  → FabricMemEngine::RegisterMem                    （fabric_mem_engine.cc:193，薄门面）
    → FabricMemLocalMemory::RegisterMem             （fabric_mem_memory.cc:98）
      → FindExistingHandleForOverlap                （幂等检查）
      → FabricMemAllocator::GetPaHandleFromVa       （fabric_mem_allocator.cc:140，判断内存来源）
        ├─ 命中：分配器分配的内存，handle 直接可用
        └─ 未命中：aclrtMemRetainAllocationHandle   （引用外部内存）
      → aclrtMemExportToShareableHandleV2           （导出 Fabric 共享句柄）
      → [仅 MEM_HOST] ImportHostMemoryForRegister
          → VirtualMemoryManager::ReserveMemory     （virtual_memory_manager.cc:134）
          → aclrtMemImportFromShareableHandleV2 + aclrtMapMem
      → share_handles_ 台账登记
  （随后控制面把 GetShareHandles 的列表发给对端）
对端 → FabricMemRemoteMemory::Import                （fabric_mem_memory.cc:226）
      → 每句柄：ReserveMemory → Import → MapMem → new_va_to_old_va_
```

2. **传输取槽链路图**（覆盖 4.4 模块）：`TransferService → SlotPool::AcquireWithTimeout → TryAcquireSlotLocked（复用或 CreateSlotEntryLocked 新建）→ 传输 → Release`，并标注失败路径走 `AbortSlot` 五步。

3. **资源产物清单表**：对链路上每个中间产物（`pa_handle`、`share_handle`、`imported_va`、`AsyncSlot.ctx/streams/host_flags`）写明「谁创建、谁持有、何时释放」。完成后自查两个问题：host 内存为什么注册后比 device 内存多一段 VA？槽位借出前哪个字段必须清零？

## 6. 本讲小结

- `VirtualMemoryManager` 是进程级单例：一次性预留默认 32TB 虚拟空间（1GB 一块、默认基址 40TB 处），用 first-fit 位图分配连续块，防回绕取整与重复释放校验保证健壮性。
- `FabricMemAllocator` 封装 VMM 三步（MallocPhysical → ReserveMemory → MapMem），scope guard 逆序回滚保证无泄漏；host 内存走「NUMA+1G 大页 → 普通 host → 小页」三级降级，且映射后必须给 device 补 READWRITE 授权。
- `FabricMemLocalMemory::RegisterMem` 幂等（重叠检查）、可注册外部内存（retain 机制）、对 host 内存额外做本端重导入获得 fabric 视角 VA；`FabricMemRemoteMemory::Import` 把对端共享句柄映射到本进程并维护新旧地址翻译表——传输前两端地址都要翻译。
- `FabricMemSlotPool` 把 context/stream/notify/host flag 打包成 `AsyncSlot` 池化复用；获取分「有空闲 / 可扩容 / 池满等待超时」三分支，归还区分普通复用与闩锁销毁；失败中止遵循「先停内核再释放缓冲」的五步固定顺序。
- 与 CS 路径（u2-l3 的 HixlMemStore）对比：FabricMem 的注册不走 CS 控制面，共享句柄交换由自己的 ControlServer 承担，但「未注册地址不能传输」的安全闸语义完全一致。

## 7. 下一步学习建议

本讲打通了 FabricMem 的「内存 + 传输资源」底座，下一讲 **u5-l3 FabricMem 传输服务：host 与 aicpu 路径** 将把这些资源用起来：`FabricMemTransferService` 如何在 host 服务与 AICPU 服务之间选择、`ChannelManager` 如何管理通道、以及 `FabricMemAicpuDispatcher` 如何在本讲的槽位与 notify 之上下发 SDMA 任务。建议提前阅读 [fabric_mem_transfer_service.cc:L88-L133](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L88-L133)（InitCommon 与 Finalize），留意 `slot_pool_`、`channel_manager_`、`local_memory_` 三者的初始化顺序与清理顺序的镜像关系。
