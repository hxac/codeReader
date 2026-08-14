# VMM：虚拟/物理地址分离管理

## 1. 本讲目标

本讲是 SVM 系列的第三篇，承接 u4-l1（SVM 初始化）与 u4-l2（申请/释放/赋值主链路）。u4-l2 已经揭示了 SVM 的「两阶段设计」——先把虚拟地址 mmap 出来，再进内核申请物理页建页表。那套机制藏在 `halMemAlloc` 内部，对调用者是一个「黑盒」。

本讲要讲的 **VMM（Virtual Memory Management）** 接口，就是把这套两阶段设计「拆开」暴露给上层：让调用者**自己**决定何时申请虚拟地址、何时申请物理内存、何时把二者绑定。学完本讲你应当能够：

1. 说清「整块申请」(`halMemAlloc`) 与「VMM 分离申请 + 动态映射」两套方式的本质差异与各自适用场景。
2. 掌握 VMM 六个公共接口的职责：`halMemAddressReserve/Free`（虚拟地址）、`halMemCreate/Release`（物理内存句柄）、`halMemMap/Unmap`（动态映射），并写出它们的典型调用顺序。
3. 理解 `drv_mem_handle_t` 句柄如何充当虚拟地址与物理内存之间的「桥梁」，以及单进程映射与跨进程共享（export/import）两条路径的分支条件。
4. 认识虚拟地址真正的「发源地」——`va_allocator` 中的 **MGA（Multi-alignment Gen Allocator）** 多对齐分配器，理解它如何按 4K/64K/2M/1G 分池、按水位阈值懒扩张/懒回收。
5. 了解 `svm_register` 如何在 VMM 映射地址之上，把一段地址「注册」给对端设备，实现 H↔D、D↔D 的跨设备直接访问。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个概念。

**虚拟地址（VA）与物理地址（PA）。** 进程看到的地址是虚拟地址，NPU 芯片上的真实存储介质（HBM/DDR）有物理地址。二者通过**页表（page table）**关联：页表把一段 VA 映射到一段 PA，CPU/NPU 访问 VA 时，硬件查页表找到真实物理位置。只要页表没建立，VA 就是个「空壳」，访问会缺页或报错。

**mmap 与字符设备。** Linux 里 `mmap` 把一段进程虚拟地址「挂」到一个文件或设备上。SVM 把虚拟地址 `mmap` 到字符设备 `/dev/davinci_manager`（见 u4-l1），这一步只占住虚拟地址段，并不分配物理页——这正是「两阶段」的第一阶段。

**不透明句柄（opaque handle）。** VMM 用 `drv_mem_handle_t *` 表示一块已申请的物理内存。对调用者它是一个「不透明指针」——你不知道里面有什么，只能拿它去 `halMemMap`。它的地位类似 CUDA 里的 `cudaMemHandle` / 文件描述符：是物理资源的一个**引用凭证**。

**引用计数。** 同一个物理内存句柄可以被映射（map）到多个虚拟地址、多个设备，因此句柄内部带 `ref` 引用计数：`halMemCreate/import` 时为 1，每次 `halMemMap` 加 1，每次 `halMemUnmap/Release` 减 1，归零才真正释放物理内存。

**回顾 u4-l2 的两阶段。** `halMemAlloc` 内部依次做：① 从预留范围分虚拟地址并 mmap（`VA_ONLY`）；② `ioctl` 进内核申请物理页、建页表（`POPULATE_ONLY`）。VMM 做的事，就是把这两步以及它们之间的「句柄」分别做成独立接口交给上层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ascend_hal/svm/v3/api/master/svm_vmm.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c) | VMM 的全部公共接口实现：reserve/free/create/release/map/unmap，以及 export/import 跨进程共享、句柄与映射管理。本讲主力文件。 |
| [src/ascend_hal/svm/v3/api/master/svm_vmm.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.h) | VMM 内部头：句柄创建/查询、`svm_vmm_ops` 映射回调钩子。 |
| [src/ascend_hal/svm/v3/api/master/svm_register.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c) | 把 VMM 映射地址注册给对端设备的实现（register/unregister to peer）。 |
| [src/ascend_hal/svm/v3/assign/va_allocator/mga.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c) | MGA 多对齐通用分配器：虚拟地址空间的实际分配/回收引擎。 |
| [src/ascend_hal/svm/v3/assign/va_allocator/mga.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.h) | MGA 的属性结构 `mga_attr`（含 expand/shrink 回调、水位阈值）与对外接口。 |
| [src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c) | 一个具体的 MGA 使用者：用 expand 回调真正去 `svm_reserve_va`（即 mmap 到字符设备）扩张虚拟地址段。 |
| [pkg_inc/ascend_hal_base.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h) | 六个 VMM 公共接口的 Doxygen 声明（契约层）。 |
| [src/ascend_hal/svm/README.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md) | SVM 模块说明，含 VMM 的业务流程图与使用顺序。 |

## 4. 核心概念与源码讲解

### 4.1 svm_vmm（一）：VMM 分离申请模型与生命周期

#### 4.1.1 概念说明

**VMM 解决的核心问题是「内存碎片」与「物理内存复用」。**

`halMemAlloc` 是「整块申请」：一次调用同时拿到 VA 和 PA，二者绑定在一起，释放时也一起回收。这种用法简单，但有个代价——每次申请都从物理内存池里切走一段，频繁申请释放不同大小会产生碎片；而且同一块物理内存在它的生命周期内只能挂在固定的虚拟地址上。

VMM 把这个过程拆成三个独立动作，让上层获得完全的控制权：

- **Reserve（预留虚拟地址）**：只占一段虚拟地址，**不分配任何物理页**。
- **Create（申请物理内存）**：在设备上申请一段物理内存，拿到一个**句柄**，**不挂任何虚拟地址**。
- **Map（映射）**：把某个句柄（物理内存）挂到某段已预留的虚拟地址上，建立页表，此时地址才可读写。

对应的反向操作是 `Free`（归还虚拟地址）、`Release`（释放物理内存句柄）、`Unmap`（解除映射）。三对接口的组合价值在于：**同一块物理内存（句柄）可以先 map 到地址 A，unmap 后再 map 到地址 B；也可以同时 map 到多个地址、多个设备**——从而重复使用物理内存，减少碎片。SVM README 对此的总结是：

> 基于 VMM 功能，业务模块可一次性申请所需的虚拟地址和物理地址，并根据实际需求动态建立映射关系，重复使用同一块物理内存，从而有效减少因物理内存频繁切分而导致的内存碎片。
> —— [src/ascend_hal/svm/README.md:129-134](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L129-L134)

#### 4.1.2 核心流程

VMM 的典型生命周期（也是 [README:136-141](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L136-L141) 给出的业务流程）如下：

```
        ┌───────────────────────┐         ┌───────────────────────┐
        │ halMemAddressReserve  │         │     halMemCreate      │
        │  占一段虚拟地址 VA      │         │ 申请物理内存，得 handle │
        └──────────┬────────────┘         └───────────┬───────────┘
                   │                                  │
                   │             halMemMap            │
                   └─────────────┬────────────────────┘
                                 ▼
                     建立 VA ↔ handle(物理内存) 页表
                     （此时 VA 可读写）
                                 │
                          … 使用内存 …
                                 │
                          halMemUnmap(VA)      解除映射（handle.ref--）
                                 │
                  ┌──────────────┴───────────────┐
                  ▼                              ▼
        halMemAddressFree(VA)          halMemRelease(handle)
          归还虚拟地址                    释放物理内存（ref==0 才真释放）
```

关键点：Reserve 与 Create **相互独立**，可以先做任意一个；Map 是唯一把二者绑定的步骤；Unmap 之后，VA 和 handle 都还「活着」，可以选择再次 Map（复用），或分别 Free / Release。这种「先分别申请、再按需映射」的能力，是 VMM 相对 `halMemAlloc` 的根本区别。

#### 4.1.3 源码精读

VMM 把虚拟地址、物理内存、映射三件事分别用不同的 **flag 能力位**标记，能力位定义在 [svm_flag.h:20-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/svm_flag.h#L20-L43)：

| 能力位 | 含义 | 谁来置位 |
| --- | --- | --- |
| `SVM_FLAG_CAP_VMM_VA_FREE` | 这是一段 VMM 预留的虚拟地址，可被 `halMemAddressFree` 回收 | reserve 时 |
| `SVM_FLAG_CAP_VMM_MAP` / `VMM_UNMAP` | 该地址支持 VMM 的 map/unmap 操作 | map 时 |
| `SVM_FLAG_CAP_VMM_PA_FREE` | 这是一个 VMM 物理内存句柄，可被 `halMemRelease` 释放 | create 时 |
| `SVM_FLAG_CAP_VMM_EXPORT` | 该物理内存可被 export 成跨进程共享句柄 | create 时 |

这套能力位是 u4-l2 讲过的「`SVM_FLAG_CAP_*` 能力位闭环」在 VMM 场景的具体化：**申请时一次性装配、使用时按位校验**，保证 free 只能释放 reserve 出来的地址、release 只能释放 create 出来的句柄，避免误操作。

整个 VMM 的「中枢数据结构」是物理内存句柄 `drv_mem_handle_t`，它就是连接 VA 与 PA 的桥梁：

[svm_vmm.c:75-93](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L75-L93) —— 句柄类型枚举与句柄结构体：

```c
enum SVM_VMM_HANDLE_TYPE {
    SVM_VMM_HANDLE_NORMAL_TYPE = 0U,   // 本进程 halMemCreate 申请的
    SVM_VMM_HANDLE_EXPORT_TYPE,        // 已被 export 成共享句柄的
    SVM_VMM_HANDLE_IMPORT_TYPE,        // 从别的进程 import 进来的
    SVM_VMM_HANDLE_MAX_TYPE
};

typedef struct drv_mem_handle {
    enum SVM_VMM_HANDLE_TYPE type;
    u32 devid;
    int clr_cs_flag;
    u64 ref;                  // 引用计数：create/import=1, map/retain+1, unmap/release-1
    u64 key;                  // export/import 时有效（共享 key）
    u64 va;                   // 这块物理内存对应的「源」虚拟地址
    u32 map_route;
    int src_prop_valid;
    struct drv_mem_prop src_prop;
    struct svm_global_va src_info;   // import 句柄用，从 casm 查到
} drv_mem_handle_t;
```

这段代码做了什么：定义了句柄的三种身份（自己申请的 / 导出共享的 / 导入别人的）和引用计数 `ref`。`ref` 是 VMM「同一物理内存可多次映射」的关键——只要 `ref > 0`，物理内存就不会被真正释放。

VMM 模块的初始化用一个带优先级的 constructor 注册 CRIU（检查点/恢复）回调，用于进程迁移时重建映射：

[svm_vmm.c:3016-3022](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L3016-L3022) —— VMM 模块自注册：

```c
void __attribute__((constructor(SVM_INIT_PRI_FINAL - 1))) svm_vmm_init(void)
{
    int ret = svm_criu_register_ops(&g_vmm_criu_ops);
    ...
}
```

#### 4.1.4 代码实践

**实践目标**：建立 VMM 三对接口与生命周期的直观对应。

**操作步骤**：

1. 打开 [svm_vmm.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c)，用编辑器搜索定位六个公共入口：`halMemAddressReserve`、`halMemAddressFree`、`halMemCreate`、`halMemRelease`、`halMemMap`、`halMemUnmap`。
2. 再打开 [pkg_inc/ascend_hal_base.h:2760-2859](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2760-L2859)，对照每个接口的 Doxygen 注释（`@param`/`@attention`）。
3. 在纸上把六个接口画成上面的生命周期图，标注：哪个接口「只动 VA」「只动 PA」「把二者绑定」。

**需要观察的现象**：

- 注释里多次出现 `Only support ONLINE scene`（如 [halMemAddressReserve 文档:2763](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2763)），说明 VMM 全家桶只在单进程在线场景可用，不支持 hccl 跨片直接访问（跨片只能 `aclrtMemcpyAsync`）。
- `halMemMap` 的 `offset` 与 `flag` 都标注 `currently unused, must be zero`，是为未来兼容预留的参数。

**预期结果**：能复述「Reserve→Create→Map→Unmap→Free/Release」的顺序，并解释为何 Unmap 之后不一定要立刻 Free/Release（可以再 Map 复用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `halMemAddressReserve` 之后、`halMemMap` 之前，对返回的虚拟地址读写是危险的？
**参考答案**：Reserve 只占住虚拟地址并 mmap 到字符设备，并未建立页表、未分配物理页（u4-l2 的 `VA_ONLY` 阶段）。此时地址是「空壳」，读写会触发缺页或未定义行为；必须 `halMemMap` 把物理内存句柄挂上去、建好页表后才可访问。

**练习 2**：`drv_mem_handle_t::ref` 引用计数在哪些接口里 +1、-1？
**参考答案**：`halMemCreate`/`halMemImportFromShareableHandle` 时初始化为 1；`halMemMap` 成功后 `svm_atomic64_inc(&handle->ref)`（[svm_vmm.c:1709](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1709)）；`halMemUnmap`→`vmm_free_pa` 与 `halMemRelease` 时 `svm_atomic64_sub`（[svm_vmm.c:986](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L986)），归零才真正释放物理内存。

---

### 4.2 svm_vmm（二）：虚拟地址申请/释放 Reserve/Free

#### 4.2.1 概念说明

`halMemAddressReserve` 只做一件事：从 SVM 的预留虚拟地址范围里切出一段，返回起始指针。它**不碰物理内存**，开销低、速度快，适合「先把地址空间规划好」的场景，比如预先预留一大段 VA，再按需把不同的物理内存映射进去。

它的对端 `halMemAddressFree` 则把这段 VA 归还给分配器（注意：归还前必须先 `halMemUnmap` 掉所有挂在上面的映射）。

#### 4.2.2 核心流程

```
halMemAddressReserve(ptr, size, alignment, addr, flag)
   │
   ├─ vmm_address_reserve_para_check   // 校验：size≠0、alignment 必须为 0、
   │                                    //   指定地址须按 2M(或 1G) 对齐
   ├─ vmm_get_va_align(pg_type)        // 由 flag 低 8 位的页类型推对齐度
   │                                    //   normal/huge→2M, giant→1G
   └─ vmm_malloc_va(align, &va, size, flag)
         ├─ 装配 svm_flag：VA_ONLY | VMM_VA_FREE | VMM_MAP | MUST_WITH_PRIV ...
         ├─ svm_malloc(&start, size, align, flag, &location)  // 真正向 va_allocator 要地址
         └─ vmm_create_svmm_inst(start, ...)                  // 为这段 VA 建一个管理实例
                                                            //   （后续 map 的段信息挂在这里）
```

#### 4.2.3 源码精读

公共入口先做参数校验、按页类型选对齐度，再调内部 `vmm_malloc_va`：

[svm_vmm.c:833-869](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L833-L869) —— `halMemAddressReserve` 入口：

```c
drvError_t halMemAddressReserve(void **ptr, size_t size, size_t alignment, void *addr, uint64_t flag)
{
    void *malloc_va = addr;
    u64 len = (u64)size;
    u32 pg_type;
    int ret;

    pg_type = (u32)(flag & 0xFF);                 // flag 低 8 位编码页类型 drv_mem_pg_type
    ret = vmm_address_reserve_para_check(ptr, size, alignment, addr, pg_type);
    ...
    if (((flag & MEM_RSV_TYPE_DEVICE_SHARE) != 0) || (size >= SVM_VA_RESERVE_ALIGN)) {
        len = svm_align_up((u64)size, SVM_VA_RESERVE_ALIGN);   // 大块或共享场景向上对齐
    }

    svm_use_pipeline();
    ret = vmm_malloc_va(vmm_get_va_align(pg_type), &malloc_va, len, flag);
    svm_unuse_pipeline();
    ...
}
```

这段代码做了什么：从 `flag` 低 8 位取出页类型（`drv_mem_pg_type` 枚举），按页类型决定对齐度，把可能的大块向上对齐，最后交由 `vmm_malloc_va` 完成实际分配。`alignment` 参数目前必须为 0（向后兼容预留），真正起作用的对齐度由 `pg_type` 决定。

真正「要地址」的内部函数 `vmm_malloc_va` 在装配 flag 后调用通用 `svm_malloc`，并立即为这段 VA 建立一个**段管理实例**（svmm_inst），这个实例将承载后续每次 map 的「段信息」：

[svm_vmm.c:715-756](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L715-L756) —— `vmm_malloc_va`：

```c
static int vmm_malloc_va(u64 align, void **va, u64 size, u64 flag)
{
    ...
    svm_flag |= SVM_FLAG_CAP_VMM_VA_FREE;        // 标记：可被 halMemAddressFree 回收
    svm_flag |= SVM_FLAG_CAP_VMM_MAP;            // 标记：支持 halMemMap
    svm_flag |= SVM_FLAG_ATTR_VA_ONLY;           // 关键：只申请虚拟地址，不要物理页
    svm_flag |= SVM_FLAG_MUST_WITH_PRIV;
    ...
    ret = svm_malloc(&start, size, align, svm_flag, &location);   // 向 va_allocator 要地址
    if (ret != 0) { return ret; }

    ret = vmm_create_svmm_inst(start, size, svm_flag);            // 建 VA 的段管理实例
    if (ret != 0) { (void)svm_free(start); return ret; }
    (*va) = (void *)(uintptr_t)start;
    return 0;
}
```

这段代码做了什么：核心是 `SVM_FLAG_ATTR_VA_ONLY`——这正是 u4-l2 讲过的「两阶段开关」，它告诉底层 `svm_malloc`「只走第一阶段，不要 populate 物理页」。同时装配 `VMM_VA_FREE`/`VMM_MAP` 能力位，使这段地址后续能被 `halMemAddressFree`/`halMemMap` 识别。

释放端 `halMemAddressFree` 走严格的能力位校验，确保只能释放 reserve 出来的地址：

[svm_vmm.c:758-778](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L758-L778) —— `vmm_free_va`：

```c
static int vmm_free_va(void *va)
{
    struct svm_prop prop;
    u64 start = (u64)(uintptr_t)va;
    int ret;

    ret = svm_get_prop(start, &prop);                       // 查这段地址的属性
    ...
    if (!svm_flag_cap_is_support_vmm_va_free(prop.flag)) {  // 必须带 VMM_VA_FREE 能力位
        svm_err("Addr cap is not support vmm va free. (va=0x%llx)\n", start);
        return DRV_ERROR_INVALID_VALUE;
    }

    ret = svm_free(start);                                  // 归还给 va_allocator
    return (ret == DRV_ERROR_CLIENT_BUSY) ? DRV_ERROR_BUSY : ret;
}
```

这段代码做了什么：先用 `svm_get_prop` 反查地址属性，校验它确实带 `VMM_VA_FREE`（即确实是 reserve 来的），再 `svm_free` 归还。这是能力位「申请时装配、释放时校验」闭环的释放半边。若地址仍被映射占用，底层会返回 `DRV_ERROR_CLIENT_BUSY`，转译为对外的 `DRV_ERROR_BUSY`。

#### 4.2.4 代码实践

**实践目标**：理解 reserve 的对齐规则与 `VA_ONLY` 的作用。

**操作步骤**：

1. 阅读 [vmm_address_reserve_para_check（svm_vmm.c:780-822）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L780-L822)，列出它拒绝哪些输入（`size==0`、`alignment!=0`、指定地址未按 `VMM_ALLOC_RECOMMENDED_GRANULARITY` 即 2M 对齐等）。
2. 对照公共头文档 [halMemAddressReserve（ascend_hal_base.h:2760-2784）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2760-L2784) 的 `@attention`，其中规定：size > 512MB 时指定地址须 1GB 对齐；size ≤ 512MB 时须按 `power(2, ceil(log2(size)))` 对齐。

**需要观察的现象**：

- `vmm_get_va_align`（[svm_vmm.c:824-831](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L824-L831)）用一个静态数组把页类型映射到对齐度：`MEM_NORMAL_PAGE_TYPE`/`MEM_HUGE_PAGE_TYPE`→2M，`MEM_GIANT_PAGE_TYPE`→1G。
- 当 `size >= SVM_BYTES_PER_GB` 时，reserve 会自动把页类型升档为 `MEM_GIANT_PAGE_TYPE`（[svm_vmm.c:848-851](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L848-L851)），这是为兼容老版本「想申请 1GB 对齐 VA 但传了 HUGE」的用法。

**预期结果**：能解释「为什么 reserve 比 alloc 轻量」——因为它只走 `VA_ONLY` 第一阶段，不进内核申请物理页。**待本地验证**：在真实 NPU 环境用 `halMemAddressReserve` 连续 reserve 多段、再 free，观察耗时远低于等量的 `halMemAlloc`。

#### 4.2.5 小练习与答案

**练习 1**：`halMemAddressReserve` 的 `flag` 参数低 8 位编码什么？为什么要这样设计？
**参考答案**：低 8 位编码 `drv_mem_pg_type`（页类型），用来决定虚拟地址的对齐度（2M 或 1G）。这样设计让一个 `flag` 同时承载「页类型」与高位「保留类型」（如 `MEM_RSV_TYPE_DEVICE_SHARE`，见 [ascend_hal_define.h:824-827](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h#L824-L827)），节省参数。

**练习 2**：如果对一段尚未 unmap 的 VMM 地址调用 `halMemAddressFree` 会怎样？
**参考答案**：底层 `svm_free` 会因地址仍被映射占用返回 `DRV_ERROR_CLIENT_BUSY`，`vmm_free_va` 将其转译为 `DRV_ERROR_BUSY` 返回给调用者（[svm_vmm.c:777](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L777)），地址不会被释放，避免产生悬空映射。

---

### 4.3 svm_vmm（三）：物理内存句柄与动态映射 Create/Release/Map/Unmap

#### 4.3.1 概念说明

如果说 Reserve/Free 管的是「虚拟地址壳子」，那么 Create/Release 管的就是「物理内存实体」，Map/Unmap 则是把壳子和实体「拧在一起」或「拆开」。

- `halMemCreate`：按属性（`drv_mem_prop`：Host 侧/Device 侧、页类型、内存类型、numa、模块 id）在设备上申请一块物理内存，返回**不透明句柄** `drv_mem_handle_t *`。
- `halMemRelease`：释放句柄对应的物理内存（引用计数归零才真释放）。
- `halMemMap`：把句柄挂到一段已 reserve 的虚拟地址上，建页表。
- `halMemUnmap`：解除映射。

映射分两条路径：**单进程映射**（本进程 create 的句柄，走 `svm_smm_client_map` 直接建本机页表）与**跨进程共享映射**（从别处 import 来的句柄，走 `svm_casm_mem_map` 经共享 key 建立）。分支依据就是句柄类型 `IMPORT` vs 其它。

#### 4.3.2 核心流程

Create 链路：

```
halMemCreate(&handle, size, prop, flag=0)
   └─ halMemCreateInner
         ├─ svm_master_init            // 确保进程级初始化（u4-l1）
         ├─ vmm_prop_check(prop)        // 校验属性合法性
         ├─ 校验 size 是 granularity 的整数倍
         └─ vmm_malloc_pa(&handle, size, prop)
               ├─ 装配 svm_flag：VMM_PA_FREE | VMM_EXPORT | 页大小属性 ...
               ├─ svm_module_mem_malloc(devid, ...)   // 进内核申请物理内存
               └─ vmm_normal_handle_create(...)        // 造 NORMAL 句柄，ref=1
```

Map 链路（带分支）：

```
halMemMap(ptr, size, offset=0, handle, flag=0)
   └─ vmm_mmap_para_check              // 校验对齐、offset/flag 必须为 0
   └─ vmm_mmap
         ├─ svm_get_prop(ptr)          // 取这段 VA 的属性
         ├─ 校验带 VMM_MAP 能力位
         ├─ if (handle->type == IMPORT)  → vmm_cross_app_mmap   // 跨进程
         │     └─ svm_casm_mem_map(... key ...)
         └─ else                        → vmm_single_app_mmap   // 单进程
               └─ _vmm_single_app_mmap
                     ├─ svm_svmm_add_seg(...)        // 在 VA 的 svmm_inst 里登记一段
                     ├─ svm_smm_client_map(...)      // 建页表
                     └─ vmm_ops_post_map(...)        // 通知注册的回调（如 prefetch）
         handle->ref++                                  // 映射成功引用 +1
```

#### 4.3.3 源码精读

Create 的内部实现 `vmm_malloc_pa` 与 reserve 的 `vmm_malloc_va` 结构对称——一个要物理内存、一个要虚拟地址：

[svm_vmm.c:928-966](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L928-L966) —— `vmm_malloc_pa`：

```c
static int vmm_malloc_pa(drv_mem_handle_t **handle, u64 size, const struct drv_mem_prop *prop)
{
    ...
    svm_flag |= SVM_FLAG_CAP_VMM_PA_FREE;     // 标记：可被 halMemRelease 释放
    svm_flag |= SVM_FLAG_CAP_VMM_EXPORT;      // 标记：可被 export 成共享句柄
    svm_flag |= SVM_FLAG_BY_PASS_CACHE;
    svm_flag |= (pg_type == MEM_GIANT_PAGE_TYPE) ? SVM_FLAG_ATTR_PA_GPAGE :
                 ((pg_type == MEM_NORMAL_PAGE_TYPE) ? 0 : SVM_FLAG_ATTR_PA_HPAGE);
    ...
    ret = svm_module_mem_malloc(devid, numa_id, svm_flag, &start, size, module_id);  // 进内核申请物理内存
    ...
    *handle = vmm_normal_handle_create(devid, start, prop);   // 造 NORMAL 句柄
    ...
}
```

这段代码做了什么：装配 `VMM_PA_FREE`/`VMM_EXPORT` 能力位（供 release/export 校验），根据真实页类型（经 `vmm_get_real_pg_type` 归一化为 normal/huge/giant）设置页大小属性，调 `svm_module_mem_malloc` 真正进内核申请物理内存，最后造一个 `NORMAL` 类型、`ref=1` 的句柄。注意它与 `vmm_malloc_va` 的对称美：两者都「装配能力位 → 调底层申请 → 建管理结构」，只是一个走 VA 分配器、一个走物理内存分配器。

句柄创建函数本身很直接：

[svm_vmm.c:245-263](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L245-L263) —— `vmm_normal_handle_create`：

```c
drv_mem_handle_t *vmm_normal_handle_create(u32 devid, u64 va, const struct drv_mem_prop *src_prop)
{
    drv_mem_handle_t *handle = vmm_handle_alloc();
    if (handle != NULL) {
        handle->type = SVM_VMM_HANDLE_NORMAL_TYPE;
        handle->ref = 1ULL;                   // 初始引用计数
        handle->devid = devid;
        handle->key = 0;
        handle->va = va;
        handle->map_route = MEM_MAP_DEFAULT_PATH;
        handle->src_prop_valid = 1;
        handle->src_prop = *src_prop;
    }
    return handle;
}
```

Map 的分支调度是本节核心，单进程与跨进程在此分道扬镳：

[svm_vmm.c:1684-1713](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1684-L1713) —— `vmm_mmap` 分发：

```c
static int vmm_mmap(void *va, u64 size, drv_mem_handle_t *handle, u64 offset)
{
    ...
    if (!svm_flag_cap_is_support_vmm_map(prop.flag)) {   // VA 必须支持 VMM_MAP
        return DRV_ERROR_PARA_ERROR;
    }

    if (handle->type == SVM_VMM_HANDLE_IMPORT_TYPE) {
        ret = vmm_cross_app_mmap(start, size, handle, offset);   // 跨进程：经共享 key
    } else {
        ret = vmm_single_app_mmap(start, size, handle, offset);  // 单进程：直接建页表
    }

    if (ret == 0) {
        svm_atomic64_inc(&handle->ref);                         // 映射成功，引用 +1
    }
    return ret;
}
```

这段代码做了什么：先校验目标 VA 带 `VMM_MAP` 能力位，再按句柄类型二选一——`IMPORT` 句柄走跨进程路径（经 casm 共享 key 在对端建立映射），其余走单进程路径（直接本机建页表）。映射成功后句柄引用计数 +1，这就实现了「一个物理内存可挂多个 VA」。

单进程映射的内部细节展示了「段（seg）」的概念——每次 map 都在 VA 的 svmm_inst 里登记一段：

[svm_vmm.c:1299-1338](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1299-L1338) —— `_vmm_single_app_mmap`（节选）：

```c
static int _vmm_single_app_mmap(void *va_handle, u64 va, u64 size, struct svm_prop *src_prop, drv_mem_handle_t *handle)
{
    ...
    svm_flag = vmm_get_single_app_mmap_svm_flag(va, size, src_prop->flag);  // 装配该段支持的能力位
    src_info.va = (u64)(uintptr_t)handle;                  // 把句柄指针暂存到 src_info.va
    ret = svm_svmm_add_seg(svmm_inst, src_prop->devid, va, svm_flag, &src_info);  // 登记一段
    ...
    src_info.va = src_prop->start;                         // 真正建页表时用真实源 VA
    svm_dst_va_pack(src_prop->devid, PROCESS_CP1, va, size, &dst_info);
    ret = svm_smm_client_map(&dst_info, &src_info, smm_flag);   // 建页表
    ...
    ret = vmm_ops_post_map(seg_handle, ...);               // 通知 post_map 回调
    ...
}
```

这段代码做了什么：把目标 VA 与物理内存源信息打包成一个「段」加入 svmm_inst，调 `svm_smm_client_map` 真正建立页表，最后跑 `vmm_ops_post_map` 钩子（供 prefetch 等子模块在映射完成后做事）。注意一个巧妙处：`src_info.va` 先被借用来「暂存句柄指针」（因为段结构里要记下来源句柄），建页表前再还原成真实源 VA——这就是后面 `vmm_restore_real_src_va` 存在的原因。

> 补充：跨进程共享通过 `halMemExportToShareableHandle`(V2) 把本进程句柄导出成一个 `key`，另一进程用 `halMemImportFromShareableHandle`(V2) 凭 `key` 重建一个 `IMPORT` 句柄，再 `halMemMap` 即走 `vmm_cross_app_mmap`（`svm_casm_mem_map`）。这条路径是 u4-l4 内存共享主题的预演，本讲只点到「分支条件 = 句柄类型」。

#### 4.3.4 代码实践

**实践目标**：在源码层面跑通一次「reserve → create → map → unmap → release → free」全链路追踪。

**操作步骤**：

1. 在 [svm_vmm.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c) 中依次定位并阅读：`halMemAddressReserve`(833) → `vmm_malloc_va`(715) → `halMemCreate`(1077) → `vmm_malloc_pa`(928) → `halMemMap`(1822) → `vmm_mmap`(1684) → `_vmm_single_app_mmap`(1299)。
2. 反向追踪释放：`halMemUnmap`(1842) → `vmm_munmap`(1715) → `_vmm_single_app_unmap`(1429)（注意它在 unmap 内部就调了 `vmm_free_pa` 使 `handle->ref--`）→ `halMemRelease`(1116) → `vmm_free_pa`(968) → `halMemAddressFree`(871)。
3. 重点看 [vmm_free_pa（svm_vmm.c:968-999）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L968-L999) 里的引用计数判断：`if (svm_atomic64_sub(&handle->ref, 1ULL) > 0ULL) return 0;`——只有 ref 归零才真正释放物理内存。

**需要观察的现象**：

- 单进程 unmap 链路里，`_vmm_single_app_unmap` 的顺序是：`vmm_ops_pre_unmap`（钩子）→ `svm_svmm_del_seg`（删段）→ `vmm_free_pa`（句柄 ref--）→ `svm_smm_client_unmap`（拆页表）。即使最后一步 `svm_smm_client_unmap` 失败，前面的删段/减引用也**不回滚**（注释 `/* not rollback */`，[svm_vmm.c:1477](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1477)）。
- `halMemUnmap` 入口会校验 `ptr` 按 `VMM_ALLOC_RECOMMENDED_GRANULARITY`(2M) 对齐（[svm_vmm.c:1851-1855](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1851-L1855)）。

**预期结果**：能画出一次完整 VMM 周期内「VA 状态、PA 句柄 ref、页表」三者的变化时间线。**待本地验证**：在有硬件的环境，对同一 handle 连续 `halMemMap` 到两个不同 VA，再分别 unmap，观察 `halMemRelease` 只有在两次 unmap 后才真正成功。

#### 4.3.5 小练习与答案

**练习 1**：`halMemMap` 如何决定走单进程还是跨进程路径？两条路径分别调哪个底层函数建页表？
**参考答案**：看句柄类型 `handle->type`——`SVM_VMM_HANDLE_IMPORT_TYPE` 走 `vmm_cross_app_mmap`→`svm_casm_mem_map`（经共享 key）；其余（NORMAL/EXPORT）走 `vmm_single_app_mmap`→`svm_smm_client_map`（直接本机建页表）。见 [svm_vmm.c:1702-1706](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1702-L1706)。

**练习 2**：为什么 unmap 时 `svm_smm_client_unmap` 失败也不回滚前面的删段操作？
**参考答案**：段（seg）是用户态 svmm_inst 里的登记信息，删段是「记账」；一旦决定 unmap，业务上已认定这段映射不再使用，即使底层拆页表失败，也不能让用户态仍保留一段「已废弃但仍在册」的段（会导致后续 map/unmap 状态混乱），故选择不回滚并在日志告警。

---

### 4.4 va_allocator（MGA）：虚拟地址空间分配引擎

#### 4.4.1 概念说明

前面三节的 `svm_malloc`/`svm_free` 总是「向某处要虚拟地址」——那个「某处」就是 `va_allocator`，而它的核心实现是 **MGA（Multi-alignment Gen Allocator，多对齐通用分配器）**。

MGA 解决的问题是：虚拟地址申请有**多种对齐需求**（4K、64K、2M、1G），如果只用一个分配器，按最大对齐（1G）管理会浪费，按最小对齐（4K）管理又难满足大块。MGA 的做法是**同时维护 4 个对齐池**，申请时按所需对齐度路由到对应池子；池子背后共享同一片「基座」地址空间，按需懒扩张/懒回收。

#### 4.4.2 核心流程

MGA 用「基座池 + 子池」两层结构。基座池（`base_align_type`，通常是 1G）从操作系统真正 reserve 大块 VA；4K/64K/2M 子池从基座池切出的区间里再细分：

```
mga_va_alloc(mga_inst, align, size, &va)
   └─ _mga_va_alloc(inst, align_type, size, va)   // align_type 由 align 换算
         ├─ pthread_rwlock_wrlock                 // 写锁，保证扩张对当前线程可见
         ├─ mga_alloc(inst, 0, align_type, size, va)
         │     └─ svm_ga_alloc(ga_inst[align_type], ...)   // 先在对应子池直接分
         │        （失败时）
         │        ├─ 若是基座池：mga_shrink_all_sub_ga      // 收拢所有子池空闲块回基座
         │        └─ 若是子池：mga_expand_sub_ga_once       // 从基座再切一块给该子池
         │     再 svm_ga_alloc 重试
         │
         └─ （仍失败且 total_size < expand_thres）
                mga_expand(inst)                   // 真正向 OS reserve 一段新 VA（经 expand 回调）
                → mga_alloc 重试
```

回收 `mga_va_free` 对称地归还到子池，并在总占用低于 `shrink_thres` 时把空闲块还给 OS。

扩张/回收带**水位线滞后（hysteresis）**：只在 `total_size < expand_thres` 时才扩张、`total_size > shrink_thres` 时才回收，避免在阈值附近抖动。

#### 4.4.3 源码精读

MGA 实例结构持有 4 个对齐池的子分配器和一个读写锁：

[mga.c:17-33](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L17-L33) —— 对齐类型枚举与实例结构：

```c
enum mga_align_type {
    MGA_ALIGN_TYPE_4K = 0U,
    MGA_ALIGN_TYPE_64K,
    MGA_ALIGN_TYPE_2M,
    MGA_ALIGN_TYPE_1G,
    MGA_ALIGN_TYPE_MAX
};

struct mga_inst {
    struct mga_attr attr;
    u32 base_align_type;            // 基座对齐（通常 1G）
    u64 total_size;                 // 当前向 OS reserve 的总量
    pthread_rwlock_t rwlock;
    void *ga_inst[MGA_ALIGN_TYPE_MAX];   // 4 个子分配器（gen_allocator）
};
```

这段代码做了什么：一个 MGA 实例 = 4 个 `gen_allocator` 子池 + 一份属性 + 一把读写锁。`base_align_type` 由 `attr.max_align_size` 决定（见 `mga_inst_init`），是「真正向 OS 要地址」的那个池；其余三个子池的地址空间是从基座池借的。

分配的主循环 `mga_alloc` 体现了「先试 → 不够就扩张 → 再试」的策略：

[mga.c:271-295](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L271-L295) —— `mga_alloc`：

```c
static int mga_alloc(struct mga_inst *inst, u32 ga_flag, u32 align_type, u64 size, u64 *va)
{
    void *ga_inst = inst->ga_inst[align_type];
    int ret;

    ret = svm_ga_alloc(ga_inst, ga_flag, va, size);     // 1) 子池直接分
    if (ret == DRV_ERROR_NONE) { return DRV_ERROR_NONE; }

    if (align_type == inst->base_align_type) {
        mga_shrink_all_sub_ga(inst);                    // 2a) 基座不够：收拢子池
    } else {
        ret = mga_expand_sub_ga_once(inst, align_type, size);  // 2b) 子池不够：从基座切
        if (ret != DRV_ERROR_NONE) {
            mga_shrink_all_sub_ga(inst);                //    切不动就先收拢再切一次
            ret = mga_expand_sub_ga_once(inst, align_type, size);
            if (ret != DRV_ERROR_NONE) { return DRV_ERROR_OUT_OF_MEMORY; }
        }
    }

    return svm_ga_alloc(ga_inst, ga_flag, va, size);    // 3) 重试
}
```

这段代码做了什么：分配三级降级策略——子池直接分 → 子池不够就向基座借（`mga_expand_sub_ga_once`：基座 `svm_ga_alloc` 一块，`svm_ga_add_range` 加进子池）→ 借不到先收拢所有子池的空闲块（`mga_shrink_all_sub_ga`）再借。最外层 `_mga_va_alloc` 还会在「仍失败且未达扩张阈值」时调 `mga_expand` 向 OS 要全新的 VA 段。

扩张的真正动作在 `mga_expand`，它回调 `attr.expand`——这个回调由 MGA 的使用者提供，负责「真正去 reserve 一段 VA」：

[mga.c:233-255](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L233-L255) —— `mga_expand`：

```c
static int mga_expand(struct mga_inst *inst)
{
    u64 va = 0, size;
    int ret;

    size = inst->attr.expand_gran;                 // 每次扩张的粒度（如 1TB）
    ret = inst->attr.expand((void *)inst, &size, &va);   // 使用者提供的扩张回调
    if (ret != 0) { ...; return ret; }

    ret = svm_ga_add_range(inst->ga_inst[inst->base_align_type], va, size);  // 加入基座池
    ...
    inst->total_size += size;
    return DRV_ERROR_NONE;
}
```

那么「使用者提供的扩张回调」具体做了什么？看 `va_non_dev_default_allocator.c`——它的 `expand` 回调才是虚拟地址真正「落地」（mmap 到字符设备）的地方：

[va_non_dev_default_allocator.c:28-53](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c#L28-L53) —— `va_non_dev_default_va_expand`：

```c
static int va_non_dev_default_va_expand(void *mga_inst, u64 *size, u64 *va)
{
    u64 fixed_va = 0;
    u32 flag = 0;
    int ret;

    flag |= SVM_VA_RESERVE_FLAG_WITH_MASTER;
    flag |= SVM_VA_RESERVE_FLAG_WITH_CUSTOM_CP;
    flag |= SVM_VA_RESERVE_FLAG_WITH_HCCP;

    if (!va_reserve_has_dev()) {                    // 未打开设备时用固定起点
        fixed_va = VA_NON_DEV_DEFAULT_VA_START + mga_get_total_size(mga_inst);
    }

    ret = svm_reserve_va(fixed_va, *size, flag, va);   // ★ 真正 reserve+mmap 到字符设备
    ...
}
```

这段代码做了什么：MGA 本身只管「地址空间记账」，真正的「向 OS 要一段可用的虚拟地址」发生在使用者的 `expand` 回调里——`svm_reserve_va` 才是把 VA `mmap` 到 `/dev/davinci_manager` 的地方（呼应 u4-l1 的字符设备通道）。该使用者的属性配置见 [va_non_dev_default_allocator.c:63-87](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c#L63-L87)：`max_align_size=1G`、`expand_gran=1TB`、`expand_thres=128TB`、`shrink_thres=8TB`。

MGA 的对外接口只有两个——分配与回收：

[mga.c:337-371](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L337-L371) —— `mga_va_alloc` / `mga_va_free`：

```c
int mga_va_alloc(void *mga_inst, u64 align, u64 size, u64 *va)
{
    struct mga_inst *inst = (struct mga_inst *)mga_inst;
    u32 align_type = mga_align_size_to_type(align);   // align(4K/64K/2M/1G) → 池子下标
    ...
    return _mga_va_alloc(inst, align_type, size, va);
}

int mga_va_free(void *mga_inst, u64 va, u64 size, u64 align)
{
    ...
    return _mga_va_free(inst, va, size, align_type);
}
```

这段代码做了什么：把「对齐字节数」换算成「池子下标」（`mga_align_size_to_type`，见 [mga.c:45-59](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L45-L59)），再交给带锁的内部函数。调用者只需传「我要多大、按多少对齐」，MGA 自动路由到正确的池子。

> 说明：本节聚焦 MGA 这一层。其下层的 `gen_allocator`（红黑区间树管理 `svm_ga_alloc/add_range/recycle`）、`cache_malloc`、`malloc_mng` 的协同是 u4-l5（SVM v3 分配器架构）的主题，本讲不展开。

#### 4.4.4 代码实践

**实践目标**：理解「MGA 记账 + expand 回调落地」的分层，以及水位线滞后。

**操作步骤**：

1. 阅读 [mga_inst_init（mga.c:73-102）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L73-L102)，确认初始化时一次性创建了 4 个 `ga_inst` 子分配器。
2. 对照 [va_non_dev_default_allocator.c:63-87](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c#L63-L87)，把四个属性值（`max_align_size`/`expand_gran`/`expand_thres`/`shrink_thres`）填进下表：

   | 属性 | 值 | 含义 |
   | --- | --- | --- |
   | `max_align_size` | 1 GB | 基座池对齐度 |
   | `expand_gran` | 1 TB | 每次向 OS 要多少 |
   | `expand_thres` | 128 TB | 总量低于此值才允许扩张 |
   | `shrink_thres` | 8 TB | 总量高于此值才触发回收 |

3. 在 [_mga_va_alloc（mga.c:302-319）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L302-L319) 与 [_mga_va_free（mga.c:321-335）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L321-L335) 中找到 `total_size < expand_thres` 与 `total_size > shrink_thres` 两个判断。

**需要观察的现象**：

- 扩张发生在分配失败之后（懒扩张），而非启动时一次性 reserve 全部；回收发生在释放之后且总量超阈值时（懒回收）。
- 这套设计与近期提交「Remove the unnecessary heaplist rescan in svm VA alloc」一致——分配器在不断剔除冗余的 rescan 以加快分配路径。

**预期结果**：能解释「为什么 MGA 要分 4 个对齐池」——避免大对齐管理小请求的浪费，同时满足 1G 大页请求。**待本地验证**：在高压力分配/释放循环下，观察 `mga_get_total_size` 是否在 8TB~128TB 区间内趋于稳定（滞后效应）。

#### 4.4.5 小练习与答案

**练习 1**：MGA 自己会调用 `mmap` 吗？虚拟地址真正「落地」在哪一层？
**参考答案**：不会。MGA 只做地址空间记账（`svm_ga_alloc/add_range`）。真正 `mmap` 到字符设备 `/dev/davinci_manager` 的是使用者在 `attr.expand` 回调里调用的 `svm_reserve_va`（见 [va_non_dev_default_allocator.c:43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_non_dev_default_allocator.c#L43)）。MGA 通过回调把「记账」与「落地」解耦，使同一套 MGA 能用于 dev/non-dev/pcie-th 等不同地址区。

**练习 2**：为什么扩张/回收要带 `expand_thres`/`shrink_thres` 水位线，而不是「池子空了就扩张、满了就回收」？
**参考答案**：避免在阈值附近反复扩张-回收抖动（thrashing）。用水位线滞后区，使总储备量在 [shrink_thres, expand_thres] 之间稳定，减少向 OS 反复 reserve/release 的开销。

---

### 4.5 svm_register：把映射地址注册给对端设备

#### 4.5.1 概念说明

`svm_register` 是 VMM 之上的一个**增强能力**：把一段已经 map 好的虚拟地址「注册」给另一个设备，让那个设备也能直接访问这段地址（H↔D、D↔D）。它是 [README:85-90](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L85-L90) 描述的「host 和 device 间内存共享（`halHostRegister`）」在 VMM 场景的底层支撑之一。

注册与映射的区别：`halMemMap` 是「把物理内存挂到本进程某段 VA、建本端页表」；`svm_register` 则是「在这之上，再让对端设备也建立对同一物理内存的访问能力」。注册信息按「源设备×目标设备」二维维护，每个目标设备有独立的段管理实例。

#### 4.5.2 核心流程

```
svm_register_to_peer(va, size, devid, &dst_va)
   ├─ svm_use_pipeline / _svm_register_to_peer
   │     ├─ if va 在 SVM 虚拟地址范围内 → svm_register_svm_to_peer
   │     └─ else（用户 malloc 的 VA）    → svm_register_user_malloc_to_peer
   │
   └─ svm_register_svm_to_peer
         ├─ svm_get_prop(va) 取属性
         ├─ 校验：flag 支持 register、且是 H↔D 场景
         ├─ svm_handle_get(va) 拿到 VA 句柄
         ├─ if flag 支持 VMM_UNMAP → svm_vmm_register     // VMM 映射地址
         │   else                  → svm_normal_register   // 普通地址
         └─ svm_register(svmm_inst, va, size, devid, &src_info)
               ├─ svm_svmm_add_seg(...)        // 在目标设备的 register svmm_inst 登记一段
               └─ svm_smm_client_map(...)      // 让目标设备建立访问映射
```

#### 4.5.3 源码精读

对外入口用「VA 是否落在 SVM 管理范围」分两条路，再在 SVM 路径内按「是否 VMM 地址」二次分流：

[svm_register.c:961-965](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L961-L965) —— 顶层分发：

```c
static int _svm_register_to_peer(u64 va, u64 size, u32 devid, u64 *dst_va)
{
    return svm_va_is_in_range(va, size) ? svm_register_svm_to_peer(va, size, devid, dst_va) :
                                          svm_register_user_malloc_to_peer(va, size, devid, dst_va);
}
```

VMM 地址的注册路径 `svm_vmm_register` 从 VA 句柄取出对应的 svmm_inst，再为「目标设备」创建/复用一个专门的 register 段管理实例：

[svm_register.c:618-672](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L618-L672) —— `svm_vmm_register`（节选）：

```c
static int svm_vmm_register(void *va_handle, u64 va, u64 size, u32 devid)
{
    ...
    vmm_svmm_inst = vmm_get_svmm(va_handle);          // 这段 VA 的 VMM 段实例
    seg_handle = svm_svmm_seg_handle_get(vmm_svmm_inst, va);
    ...
    register_svmm_inst = svm_vmm_get_register_svmm(seg_handle, devid);   // 目标设备已有的 register 实例？
    if (register_svmm_inst == NULL) {                 // 首次注册到该设备：创建
        register_svmm_inst = svm_vmm_create_register_svmm(seg_handle, devid, seg_va, src_info.size, svm_flag);
        ...
    }
    ...
    ret = svm_register(register_svmm_inst, va, size, devid, &src_info);   // 登记段 + 让对端建映射
    if (ret == 0) {
        u64 flag = svm_flag | SVM_FLAG_CAP_LDST;      // 注册成功，补上 LDST 能力位
        svm_svmm_mod_seg_svm_flag(seg_handle, flag);
    }
    ...
}
```

这段代码做了什么：VMM 地址注册时，先找到该 VA 对应的 VMM 段实例，再为「目标设备」获取或新建一个 register 段管理实例（`register_node->svmm_inst[devid]`，即按目标 devid 维护），最后调 `svm_register` 真正登记并让对端设备建立访问。注册成功后给段补上 `SVM_FLAG_CAP_LDST`（load/store）能力位，标记这段地址现在支持对端直接访存。

真正干活的 `svm_register` 三步走：登记段 → 让对端建映射 → 跑 post_map 钩子：

[svm_register.c:547-584](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L547-L584) —— `svm_register`：

```c
static int svm_register(void *svmm_inst, u64 va, u64 size, u32 devid, struct svm_global_va *src_info)
{
    ...
    ret = uda_get_udevid_by_devid_ex(src_info->udevid, &src_info->udevid);  // 逻辑号→全局唯一号
    ...
    ret = svm_svmm_add_seg(svmm_inst, devid, va, svm_flag, src_info);       // 1) 登记段
    ...
    svm_dst_va_pack(devid, PROCESS_CP1, va, size, &dst_info);
    ret = svm_smm_client_map(&dst_info, src_info, smm_flag);                // 2) 对端建访问映射
    if (ret != 0) {
        (void)svm_svmm_del_seg(svmm_inst, devid, va, src_info->size, true); // 失败回滚段
        return ret;
    }
    ret = register_ops_post_map(src_info, devid, va);                       // 3) post_map 钩子
    ...
}
```

这段代码做了什么：注册的本质是在「目标设备的 register 视图」里新增一段，并经 `svm_smm_client_map` 让目标设备建立对源物理内存的访问页表。注意源端的 `udevid` 经 UDA（u3-l3）翻译成全局唯一号，这正是「UDA 是寻址层」结论的又一次体现。

#### 4.5.4 代码实践

**实践目标**：理解 register 与 map 的区别，以及 register 维度的「按目标设备」管理。

**操作步骤**：

1. 阅读 [svm_register_svmm_create（svm_register.c:460-483）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L460-L483)，注意 `register_node->svmm_inst[SVM_MAX_DEV_NUM]` 是一个「按目标 devid 索引」的数组——一个 register_node 同时记录「这段地址注册给了哪些设备」。
2. 对照 [svm_vmm_register（svm_register.c:618-672）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L618-L672) 与 [svm_register（svm_register.c:547-584）](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L547-L584)，区分「VMM 段实例（管本端 map）」与「register 段实例（管对端访问）」两套 svmm_inst。

**需要观察的现象**：

- 反注册 `svm_unregister`（[svm_register.c:586-616](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L586-L616)）要求传入的 va 必须是「注册时的起始 va」，否则返回 `DRV_ERROR_PARA_ERROR`。
- 设备下线时 `svm_register_recycle`（[svm_register.c:399-406](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L399-L406)）会遍历所有 register_node，回收该设备相关的注册段。

**预期结果**：能说清「map 让本端可读写、register 让对端也可读写」，二者是叠加关系。**待本地验证**：双卡环境，进程在 dev0 申请并 map 一段 VMM 内存，再 `svm_register_to_peer` 给 dev1，从 dev1 上的算子直接读取该段，确认无需 Host 中转拷贝。

#### 4.5.5 小练习与答案

**练习 1**：`svm_register_svm_to_peer` 如何决定走 VMM 注册路径还是普通注册路径？
**参考答案**：取 VA 属性后判断 `svm_flag_cap_is_support_vmm_unmap(prop.flag)`——VMM 映射地址（带 `VMM_UNMAP` 能力位）走 `svm_vmm_register`，否则走 `svm_normal_register`。见 [svm_register.c:950-954](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_register.c#L950-L954)。

**练习 2**：为什么 register_node 要维护 `svmm_inst[SVM_MAX_DEV_NUM]` 数组而不是单个实例？
**参考答案**：因为同一段地址可能注册给多个不同的目标设备，每个目标设备的「访问视图」（段、映射、权限）相互独立。按目标 devid 数组索引，才能正确区分和回收每个设备各自的注册状态。

---

## 5. 综合实践

**任务：对比「整块申请」与「VMM 分离申请 + 动态映射」，并写出 VMM 六接口的标准使用顺序。**

1. **概念对比（源码阅读型）**：回到 u4-l2 的 `halMemAlloc` 主链路与本讲的 VMM 链路，填写下表（依据真实源码，不要凭记忆）：

   | 维度 | `halMemAlloc`（整块） | VMM（reserve+create+map） |
   | --- | --- | --- |
   | 谁决定 VA 与 PA 的绑定时机 | 驱动内部一次性绑定 | 调用者分步控制 |
   | 物理内存能否换 VA 复用 | 否（与 VA 绑死） | 能（unmap 后可再 map 到别的 VA） |
   | 是否需要中间句柄 | 否 | 是（`drv_mem_handle_t`） |
   | 适合场景 | 普通一次性申请 | 频繁切分、需复用物理内存、跨进程共享 |
   | 关键 flag | `POPULATE_ONLY` 走完两阶段 | reserve 用 `VA_ONLY`，map 时才建页表 |

   参考依据：`halMemAlloc` 的两阶段（u4-l2）与本讲 [vmm_malloc_va](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L715-L756) 的 `VA_ONLY`、[README:129-145](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L129-L145) 对 VMM 复用物理内存的说明。

2. **写出六接口标准顺序**：依据 [README:136-141](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L136-L141) 与本讲 4.1.2 的生命周期图，写出三对接口（reserve/free、create/release、map/unmap）的最小可用调用序列，并标注每一步之后「VA 状态 / 句柄 ref / 页表」三项的变化。

3. **追踪一个复用场景**：设想「同一块物理内存先 map 到 VA_A，unmap 后再 map 到 VA_B」。在源码中确认可行性：
   - `halMemMap` 成功后 `handle->ref++`（[svm_vmm.c:1709](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1709)）；
   - `halMemUnmap(VA_A)` 时 `_vmm_single_app_unmap` 调 `vmm_free_pa` 使 ref--（[svm_vmm.c:1472](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_vmm.c#L1472)），ref 仍 >0 故物理内存不释放；
   - 于是 `halMemMap(VA_B, ..., handle)` 仍可成功。

   写出该场景下完整的接口调用序列与每步 ref 值。

## 6. 本讲小结

- **VMM = 把 `halMemAlloc` 的两阶段设计拆开**：Reserve（只要 VA，`VA_ONLY`）、Create（只要物理内存，返回 `drv_mem_handle_t` 句柄）、Map（把句柄挂到 VA 上建页表）三步独立，反向为 Free/Release/Unmap。
- **`drv_mem_handle_t` 是 VA 与 PA 的桥梁**，带 `ref` 引用计数与 NORMAL/EXPORT/IMPORT 三种身份；`halMemMap` 按句柄类型分单进程（`svm_smm_client_map`）与跨进程（`svm_casm_mem_map`）两条路径。
- **能力位闭环贯穿全程**：reserve 装 `VMM_VA_FREE`/`VMM_MAP`、create 装 `VMM_PA_FREE`/`VMM_EXPORT`，free/release/unmap 时逐一校验，杜绝误操作。
- **虚拟地址的发源地是 va_allocator 的 MGA**：多对齐（4K/64K/2M/1G）分池 + 基座/子池两层 + 水位线懒扩张/懒回收；MGA 只记账，真正 `mmap` 落地在使用者的 `expand` 回调（`svm_reserve_va`）。
- **svm_register 是 VMM 之上的跨设备访问增强**：在已 map 的地址之上，按目标 devid 维护独立 register 视图，让对端设备经 UDA 翻译后直接访问同一物理内存。
- **复用与反碎片是 VMM 的核心价值**：同一物理内存可多次 map 到不同 VA/设备，减少物理内存频繁切分造成的碎片（[README:134](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L134)）。

## 7. 下一步学习建议

- **u4-l4（内存拷贝与共享机制）**：本讲多次提到 export/import 跨进程共享（`svm_casm_*`），下一讲将系统讲解 `halMemcpy` 与 `halShmem*` 的共享模型，把跨进程路径补全。
- **u4-l5（SVM v3 地址空间与分配器架构）**：本讲把 MGA 以下的 `gen_allocator`、`cache_malloc`、`malloc_mng` 留作黑盒，下一讲会展开这些多级分配器的红黑区间树、缓存与协同设计。
- **延伸阅读**：可对照 CUDA 的「virtual memory management API」（`cuMemAddressReserve`/`cuMemCreate`/`cuMemMap`），VMM 的接口划分与之高度对应，有助于建立跨平台的「VA/PA 分离」心智模型。
