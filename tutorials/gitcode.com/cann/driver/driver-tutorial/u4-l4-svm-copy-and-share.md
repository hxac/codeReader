# 内存拷贝与共享机制

## 1. 本讲目标

本讲是 SVM（共享虚拟内存）单元的第四篇，承接 u4-l2（申请/释放/赋值）与 u4-l3（VMM 虚拟/物理分离）。在前面我们已经能「申请到一段可读写的设备内存」，但真实业务里数据还需要被**搬运**（从 Host 搬到 Device、在两张卡之间搬），还需要被**共享**（让另一个进程/另一张卡直接看到同一块物理内存，而不必拷贝）。本讲就解决这两件事。

学完后你应该掌握：

- 理解 `halMemcpy` / `drvMemcpy` 的**同步阻塞**语义，以及 H2H/H2D/D2H/D2D 四个方向的分发机制。
- 理解为什么「普通主机内存」无法被设备 DMA 直接访问，以及 `svm_cpy_host_pool`（主机缓冲池）如何用一块预注册的 SVM 内存来优化这条路径。
- 理解 `halShmemCreateHandle` / `halShmemOpenHandleByDevId` 如何通过 CASM（跨应用共享内存）层与一个「字符串化的句柄」实现设备间/进程间内存共享。
- 看懂「拷贝」与「共享」两条链路如何在一处汇合：拷贝路径能「看穿」共享地址，直接定位到真正持有物理内存的设备。

---

## 2. 前置知识

在进入本讲前，请确保理解以下概念（u4-l1 ~ u4-l3 已建立）：

- **Host 与 Device、devid**：Host 是主机侧 CPU 进程，Device 是 NPU。每张卡/每个逻辑设备有一个 `devid`，其中 Host 自身也被视作一个特殊的「host devid」（`svm_get_host_devid()`）。
- **SVM 管理的虚拟地址范围**：通过 `halMemAlloc` 申请的地址落在 SVM 预留的虚拟地址区间内（`svm_va_is_in_range()` 返回 true）；而 `malloc` 出来的普通主机堆地址**不在**这个区间。
- **能力位 `SVM_FLAG_CAP_*`**：u4-l2 讲过，每段内存在申请时会被「装配」一组能力位（能否被同步拷贝、能否 memset、能否 IPC 共享……），使用时再做校验，形成闭环。本讲会频繁见到 `SVM_FLAG_CAP_SYNC_COPY`、`SVM_FLAG_CAP_IPC_CLOSE` 等。
- **字符设备 + ioctl 陷入内核**：SVM 的用户态操作最终都经 `/dev/davinci_manager` 的 ioctl 进入内核态驱动完成。
- **UDA 设备号翻译**（u3-l3）：逻辑 `devid` ↔ 全局唯一 `udevid` 之间的翻译，本讲在「看穿共享地址」时会用到。

一个贯穿全讲的直觉：**DMA 引擎只能搬动「设备认识」的地址**。Host 的一段普通堆内存，设备并不认识；要让设备能搬它，要么先「注册（register/pin）」给设备，要么先把数据搬进一块「设备认识的」Host 内存里。本讲的「拷贝优化」与「共享」都是在围绕这个约束做文章。

---

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `src/ascend_hal/svm/v3/` 下（v3 对应 ascend950，参见 u4-l1 的 v2/v3 划分）：

| 文件 | 作用 |
| --- | --- |
| [api/master/svm_cpy.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c) | 拷贝主链路：对外入口 `halMemcpy`/`drvMemcpy`/`halMemcpy2D`/`halMemcpyBatch`，方向判定与分发表，以及共享地址的「看穿」解析。 |
| [api/master/svm_ipc.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c) | 内存共享主链路：`halShmemCreateHandle`/`Open`/`Close`/`Destroy`，以及把内核 `key` 编码成可跨进程传递的字符串 `name`。 |
| [api/master/svm_cpy_host_pool.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c) | 主机缓冲池：预注册的 SVM 主机内存池，按桶（128B/4KB/512KB）+ 位图管理，为 H2D/D2H 拷贝提供「中转站」。 |
| [op/memcpy/svm_memcpy.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy/svm_memcpy.c) | 底层同步拷贝分发：`svm_sync_copy`，按设备查已注册的 `svm_copy_ops` 操作集，最终陷入内核。 |
| [op/memcpy_local_client/svm_memcpy_client.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy_local_client/svm_memcpy_client.c) | 本地拷贝客户端：同设备/主机内的拷贝，区分「进程内」与「发消息让设备自己搬」。 |
| [share/casm/casm.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/share/casm/casm.c) | CASM（跨应用共享内存）内核 ioctl 封装：`create_key`/`get_src_va`/`op_task` 白名单等。 |
| [share/casm_map/casm_map.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/share/casm_map/casm_map.c) | 共享映射建立：`svm_casm_mem_map` = pin + 建页表映射。 |
| [inc/op/svm_memcpy.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/op/svm_memcpy.h) | 拷贝信息结构体 `svm_copy_va_info` 与方向判定 `copy_dir_get_by_devid`。 |
| [pkg_inc/ascend_hal_base.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h) | 对外公共接口声明（`halShmem*`、`halMemcpy2D` 等）。 |

> 阅读建议：先看 4.1 建立方向与阻塞的整体观，再看 4.2 理解「普通主机内存」如何被中转，最后看 4.3 的共享机制，并注意 4.3.3 末尾「拷贝看穿共享地址」如何把两者串起来。

---

## 4. 核心概念与源码讲解

### 4.1 svm_cpy：同步拷贝主链路与方向分发

#### 4.1.1 概念说明

`svm_cpy.c` 是 SVM 对外暴露的**内存拷贝门面**，承载 `halMemcpy`（按 `void*` 主机指针）、`drvMemcpy`（按 `DVdeviceptr` 设备虚拟地址）、`halMemcpy2D`（带 pitch 的二维拷贝）、`halMemcpyBatch`（批量拷贝）等一系列入口。

它要解决的核心问题是：**一次拷贝的两端可能分别位于 Host 或任意一张 Device 上**，因此必须先判定「方向」，再把活派给对应方向的处理函数。方向共有四种，定义在内核/用户共享头中：

```c
enum svm_cpy_dir {
    SVM_H2H_CPY,   // Host -> Host
    SVM_H2D_CPY,   // Host -> Device
    SVM_D2H_CPY,   // Device -> Host
    SVM_D2D_CPY,   // Device -> Device
    SVM_MAX_CPY_DIR
};
```

> 定义见 [src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h:84](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h#L84)（内核与用户态共享的同一份头文件）。

另一个关键性质是**同步阻塞**：`halMemcpy`/`drvMemcpy` 这一组接口在数据全部搬运完毕前不会返回。注意 SVM 里另有一组「异步拷贝」`halMemCpyAsync`/`halMemCpyAsyncWaitFinish`，但它们受编译宏 `CFG_FEATURE_ASYNC_COPY` 控制，且需要单独 `WaitFinish` 才算完成；本讲聚焦默认的同步路径。

#### 4.1.2 核心流程

一次同步拷贝的主干流程（以 `drvMemcpy` 为例）：

1. **参数与能力校验**：非空、`dest_max >= byte_count`、两端地址都带有 `SVM_FLAG_CAP_SYNC_COPY` 能力位。
2. **封装地址信息**：把裸地址 `(va, size)` 打包成 `struct svm_copy_va_info`，并解析出该地址归属的 `devid`（在 SVM 区间内则取内存属性里的 devid，否则视为 host devid）。
3. **（可选）解析共享地址**：若目标设备经 PCIe 连接，尝试把共享 VA「看穿」为真正持有物理内存的设备地址（详见 4.3）。
4. **判定方向**：`copy_dir_get_by_devid(src_devid, dst_devid)`。
5. **查表分发**：按方向查 `g_sync_copy[dir]` 函数指针表，执行具体搬运。
6. **底层执行**：最终经 `svm_sync_copy` → 已注册的 `svm_copy_ops->sync_copy` → ioctl 陷入内核，**阻塞**至完成。

```
drvMemcpy(dst, dest_max, src, byte_count)
   │
   ├── drvMemcpyInner: 校验 + 能力位 + 封装 svm_copy_va_info
   │
   └── svm_memcpy_sync(src_info, dst_info)
          │
          ├── （PCIe 时）svm_copy_try_resolve_shared_info  // 看穿共享地址
          │
          └── svm_mem_sync_copy(src_info, dst_info)
                 │  dir = copy_dir_get_by_devid(...)
                 └── g_sync_copy[dir](src_info, dst_info)
                        │
                        ├── H2D: svm_mem_sync_copy_h2d  ──┐
                        ├── D2H: svm_mem_sync_copy_d2h  ──┤
                        ├── D2D: svm_mem_sync_copy_d2d  ──┤ 都最终走向
                        └── H2H: svm_mem_sync_copy_h2h  ──┘ svm_sync_copy (ioctl, 阻塞)
```

H2D 与 D2H 还各自带一层「主机缓冲池」快速路径与「注册到 master」回退路径，见 4.2。

#### 4.1.3 源码精读

**(1) 地址信息结构与方向判定**

`struct svm_copy_va_info` 是拷贝链路里到处传递的「地址信封」，除了 `va/size/devid`，还带了 `host_tgid`（共享场景下真正属主的进程 id）和 `is_share`（是否是共享地址）——这两个字段是 4.3「看穿」机制的关键：

```c
struct svm_copy_va_info {
    u64 va;
    u64 size;
    u32 devid;
    int host_tgid;
    bool is_share;
};
```
[inc/op/svm_memcpy.h:19-25](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/op/svm_memcpy.h#L19-L25)

方向判定逻辑很直白：拿 src/dst 的 devid 与 host devid 比较：

```c
static inline enum svm_cpy_dir copy_dir_get_by_devid(u32 src_devid, u32 dst_devid)
{
    u32 host_devid = svm_get_host_devid();
    if ((src_devid == host_devid) && (dst_devid != host_devid))      return SVM_H2D_CPY;
    else if ((dst_devid == host_devid) && (src_devid != host_devid)) return SVM_D2H_CPY;
    else if ((dst_devid != host_devid) && (src_devid != host_devid)) return SVM_D2D_CPY;
    else                                                            return SVM_H2H_CPY;
}
```
[inc/op/svm_memcpy.h:44-57](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/op/svm_memcpy.h#L44-L57)

**(2) 方向分发表**

svm_cpy.c 用一张「以方向为下标的函数指针表」做分发，这是典型的表驱动设计：

```c
static int (*const g_sync_copy[SVM_MAX_CPY_DIR])(...) = {
    [SVM_H2H_CPY] = svm_mem_sync_copy_h2h,
    [SVM_H2D_CPY] = svm_mem_sync_copy_h2d,
    [SVM_D2H_CPY] = svm_mem_sync_copy_d2h,
    [SVM_D2D_CPY] = svm_mem_sync_copy_d2d};

int svm_mem_sync_copy(struct svm_copy_va_info *src_info, struct svm_copy_va_info *dst_info)
{
    enum svm_cpy_dir dir = copy_dir_get_by_devid(src_info->devid, dst_info->devid);
    if (g_sync_copy[dir] == NULL) {
        return DRV_ERROR_NOT_SUPPORT;
    }
    return g_sync_copy[dir](src_info, dst_info);
}
```
[api/master/svm_cpy.c:577-592](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L577-L592) ——这张表是理解整个拷贝模块的「目录」。

**(3) D2D：同设备 vs 跨设备**

D2D 分两类：同一张卡内部搬（走本地客户端 `svm_memcpy_local_client`），跨卡搬（按连接类型再分）。注意 UB（超总线）连接下跨卡不能直接搬，需要中转：

```c
static int svm_mem_sync_copy_d2d(struct svm_copy_va_info *src_info, struct svm_copy_va_info *dst_info)
{
    if (src_info->devid == dst_info->devid) {
        return svm_mem_sync_copy_same_d2d(src_info, dst_info);   // 同设备：本地客户端
    } else {
        return svm_mem_sync_copy_diff_d2d(src_info, dst_info);   // 跨设备：看连接类型
    }
}
```
[api/master/svm_cpy.c:568-575](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L568-L575)

跨设备时，PCIe 与 HCCS 可直接 `svm_sync_copy`，而 UB 要先 D2H 到一块临时主机内存再 H2D（`svm_ub_sync_copy_diff_d2d`）：

```c
static int svm_mem_sync_copy_diff_d2d(struct svm_copy_va_info *src_info, struct svm_copy_va_info *dst_info)
{
    u32 hd_connect_type = svm_get_device_connect_type(src_info->devid);
    if ((hd_connect_type == HOST_DEVICE_CONNECT_TYPE_PCIE) || (hd_connect_type == HOST_DEVICE_CONNECT_TYPE_HCCS)) {
        return svm_sync_copy(src_info, dst_info);
    } else if (hd_connect_type == HOST_DEVICE_CONNECT_TYPE_UB) {
        return svm_ub_sync_copy_diff_d2d(src_info, dst_info);   // 中转
    } else {
        return DRV_ERROR_INNER_ERR;
    }
}
```
[api/master/svm_cpy.c:556-566](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L556-L566)

**(4) 阻塞语义的源头**

「同步阻塞」最终落在 `svm_sync_copy`：它按设备查已注册的操作集 `g_copy_ops[devid]->sync_copy` 并调用之。该函数指针的实现（PCIe 适配在 `op/pci_adapt/svm_pci_memcpy.c`、UB 适配在 `op/ub_adapt/svm_ub_memcpy.c`）最终都通过 ioctl 把搬运请求交给内核态驱动，并**在 ioctl 返回前等待搬运完成**——这正是「阻塞」的物理来源：

```c
int svm_sync_copy(struct svm_copy_va_info *src_info, struct svm_copy_va_info *dst_info)
{
    enum svm_cpy_dir dir = copy_dir_get_by_devid(src_info->devid, dst_info->devid);
    u32 devid = (dir == SVM_H2D_CPY) ? dst_info->devid : src_info->devid;
    ...
    if ((g_copy_ops[devid] == NULL) || (g_copy_ops[devid]->sync_copy == NULL)) {
        return DRV_ERROR_NOT_SUPPORT;
    }
    return g_copy_ops[devid]->sync_copy(devid, src_info, dst_info);   // 阻塞 ioctl
}
```
[op/memcpy/svm_memcpy.c:86-101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy/svm_memcpy.c#L86-L101)

> 一句话：**没有「提交—等待」两段式，整个调用栈从 `halMemcpy` 一路同步到内核 ioctl，ioctl 返回即代表数据就位**——这就是 H2D/D2H/D2D 同步拷贝「阻塞」的根因。

**(5) 本地客户端：进程内 vs 发消息给设备**

`svm_memcpy_local_client` 区分「目标就是主机自身」与「目标在设备上」两种情形。前者直接在进程内用 SDMA 或 `memcpy_s` 搬；后者打包成一条消息发给设备，让设备自己执行拷贝：

```c
int svm_memcpy_local_client(u32 devid, u64 dst, u64 dst_max, u64 src, u64 count)
{
    if (devid != svm_get_host_devid()) {
        return svm_memcpy_local_event(devid, dst, dst_max, src, count);  // 发消息给设备
    } else {
        return svm_memcpy_local(dst, dst_max, src, count);               // 进程内搬
    }
}
```
[op/memcpy_local_client/svm_memcpy_client.c:56-63](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy_local_client/svm_memcpy_client.c#L56-L63)

其中进程内路径 `svm_memcpy_local` 会优先尝试硬件 SDMA（`halSdmaCopy`），失败再退回 `memcpy_s`，并且对超大块按 `split_size` 分段搬运：

```c
static int _svm_memcpy_local(u64 dst, u64 dst_max, u64 src, u64 count)
{
    int ret;
    ret = halSdmaCopy(dst, dst_max, src, count);
    if (ret != 0) {
        ret = svm_memcpy_s((void *)(uintptr_t)dst, dst_max, (void *)(uintptr_t)src, count);  // memcpy_s
    }
    return ret;
}
```
[op/memcpy_local/svm_memcpy_local.c:25-35](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy_local/svm_memcpy_local.c#L25-L35)（`svm_memcpy_s` 在 [inc/svm_user_adapt.h:39](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/svm_user_adapt.h#L39) 被宏定义为安全的 `memcpy_s`）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「H2D 同步拷贝为何是阻塞式的」，并看清 `drvMemcpy` 的完整调用栈。

**操作步骤**（源码阅读型实践）：

1. 打开 [api/master/svm_cpy.c:669-687](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L669-L687) 的 `drvMemcpy` → `drvMemcpyInner`，确认它做完校验后调用 `svm_memcpy_sync`。
2. 跟到 [svm_cpy.c:594-625](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L594-L625) 的 `svm_memcpy_sync`，注意它对 H2H 与同设备 D2D 直接走 `svm_mem_sync_copy`，其余才尝试共享地址解析。
3. 跟到 [svm_cpy.c:584-592](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L584-L592) 的 `svm_mem_sync_copy`，看到按方向查表 `g_sync_copy[dir]`。
4. 对 H2D，进入 [svm_cpy.c:486-498](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L486-L498) 的 `svm_mem_sync_copy_h2d`，最终落到 `_svm_mem_sync_copy` → `svm_sync_copy`。
5. 打开 [op/memcpy/svm_memcpy.c:86-101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/op/memcpy/svm_memcpy.c#L86-L101)，确认 `svm_sync_copy` 调用的是 `g_copy_ops[devid]->sync_copy`，这是一次阻塞 ioctl。

**需要观察的现象 / 预期结果**：整条链路上**没有任何「提交后立即返回、稍后再 wait」的结构**；唯一的返回点在底层 `sync_copy`（ioctl）之后。把这条链路画成时序图，你会看到调用栈一直「向下」走到内核才回来——这就是阻塞的直观证据。

> 运行层面验证（待本地验证）：若有 ascend950 环境，可写一个最小程序，在调用 `halMemcpy` 前后各取一次时钟，再搬运一个较大 buffer（如 100MB），观察耗时与 PCIe/UB 带宽相当，说明调用确实等到搬运完成才返回。

#### 4.1.5 小练习与答案

**练习 1**：`halMemcpy` 与 `drvMemcpy` 都做同步拷贝，它们的参数类型和能力位校验有何不同？

**参考答案**：`halMemcpy` 接收 `void *dst/src`（主机指针视角），校验 `SVM_FLAG_CAP_SYNC_COPY_EX`，并支持一个可选的 `struct memcpy_info *info` 走 D2H 特殊路径（见 [svm_cpy.c:738-782](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L738-L782)）；`drvMemcpy` 接收 `DVdeviceptr`（设备虚拟地址，u64），校验 `SVM_FLAG_CAP_SYNC_COPY`，没有 info 路径（见 [svm_cpy.c:627-667](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L627-L667)）。

**练习 2**：为什么 `g_sync_copy` 要做成「以方向为下标的数组」而不是一串 `if/else`？

**参考答案**：表驱动把「方向→处理函数」的映射数据化，新增方向或替换实现只需改表项；同时 `SVM_MAX_CPY_DIR` 作为哨兵让越界/未实现方向（`NULL`）能被统一判为 `DRV_ERROR_NOT_SUPPORT`，代码更易维护、易扩展。

---

### 4.2 svm_cpy_host_pool：主机缓冲池优化

#### 4.2.1 概念说明

回到那个贯穿直觉：**DMA 引擎只能搬「设备认识的地址」**。当用户用 `halMemcpy` 把一段**普通主机内存**（`malloc` 出来的、不在 SVM 区间内）拷到设备时，设备并不认识这段地址，没法直接 DMA。

朴素做法是每次拷贝都把这段主机内存「注册（pin/register）」给设备（`svm_register_to_master`），但这有可观的 per-call 开销。`svm_cpy_host_pool` 给出了一个更便宜的优化：**预先申请一大块「设备认识的」SVM 主机内存，切成固定大小的小块（chunk），用位图管理**。拷贝时，先用进程内的 `memcpy_s` 把用户数据搬进一个空闲 chunk，再发起一次正常的「SVM 主机内存 → 设备」DMA 拷贝。因为这块池子是常驻、预注册的，分摊到每次拷贝的代价极低。

这是一个典型的「中转缓冲（staging buffer）」模式：用一次廉价的 CPU 拷贝，换取免去一次昂贵的设备注册。

#### 4.2.2 核心流程

池子的设计参数（按常见拷贝尺寸分桶）：

| 桶 | chunk 大小 | chunk 数量 | 典型用途 |
| --- | --- | --- | --- |
| 0 | 128 B | 10000 | 小参数、小 tensor 元数据 |
| 1 | 4 KB | 1000 | 一页左右的中等数据 |
| 2 | 512 KB | 8 | 较大块 |

每个桶是一片连续的 SVM 主机内存（`MEM_HOST`），用位图（bitmap）记录每个 chunk 是否空闲。分配/回收就是「找零位→置位 / 清位」。

```
申请 slot:
  get_bucket(size)          // 选最小能放下的桶；>512KB 返回 NULL（不支持）
  lock(mutex)
  bitmap_find_next_zero_area → slot_idx   // 找一个空闲位
  bitmap_set(slot_idx)                  // 占用
  free_num--
  slot.va = base_va + slot_idx * chunk_size
  unlock(mutex)

归还 slot:
  lock(mutex)
  bitmap_clear(slot_idx)
  free_num++
  unlock(mutex)
```

**何时用池子、何时回退**（见 H2D 入口）：

```
svm_mem_sync_copy_h2d(src, dst):
  if (src 不在 SVM 区间内):              // 普通主机内存
      ret = svm_mem_sync_copy_h2d_by_pool(src, dst)   // 先试池子
      if (ret != BUSY && ret != NOT_SUPPORT):
          return ret                     // 池子成功，结束
  return _svm_mem_sync_copy(H2D, ...)    // 否则回退到「注册到 master」
```

即：**池子命中 → 廉价路径；池子没这号桶/池子满/未初始化 → 回退到逐次注册**。

池子的生命周期跟随 Host 设备的打开/关闭：通过 SVM 的「post-init / pre-uninit 回调表」自注册（参见 u4-l1 的 post-init 机制），只在 host devid 上初始化一次。

#### 4.2.3 源码精读

**(1) 桶与位图的静态布局**

池子几乎是全静态的——三个桶、三张位图都直接定义为全局数组，桶参数用指定初始化器一次写死：

```c
#define SVM_CPY_HOST_POOL_BUCKET_NUM 3
#define SVM_CPY_HOST_POOL_128_SIZE  (128ULL)
#define SVM_CPY_HOST_POOL_128_NUM   10000U
#define SVM_CPY_HOST_POOL_4K_SIZE   (4ULL * SVM_BYTES_PER_KB)
#define SVM_CPY_HOST_POOL_4K_NUM    1000U
#define SVM_CPY_HOST_POOL_512K_SIZE (512ULL * SVM_BYTES_PER_KB)
#define SVM_CPY_HOST_POOL_512K_NUM  8U
...
static struct svm_cpy_host_pool_bucket g_svm_cpy_host_pool[SVM_CPY_HOST_POOL_BUCKET_NUM] = {
    {.chunk_size = SVM_CPY_HOST_POOL_128_SIZE, .chunk_num = SVM_CPY_HOST_POOL_128_NUM,  .bitmap = g_svm_cpy_host_pool_128_bitmap},
    {.chunk_size = SVM_CPY_HOST_POOL_4K_SIZE,  .chunk_num = SVM_CPY_HOST_POOL_4K_NUM,   .bitmap = g_svm_cpy_host_pool_4k_bitmap},
    {.chunk_size = SVM_CPY_HOST_POOL_512K_SIZE,.chunk_num = SVM_CPY_HOST_POOL_512K_NUM, .bitmap = g_svm_cpy_host_pool_512k_bitmap}};
```
[api/master/svm_cpy_host_pool.c:24-64](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L24-L64)

> 位图大小用 `SVM_CPY_HOST_POOL_BITMAP_NUM(bit_num)` 宏按 `sizeof(bitmap_t)*8` 向上取整，把任意 chunk 数量都装进整字位图（[svm_cpy_host_pool.c:32-34](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L32-L34)）。

**(2) 选桶与「申请一个 slot」**

选桶就是线性找第一个 `chunk_size >= size` 的桶；申请 slot 就是位图找零位置位：

```c
static int _svm_cpy_host_pool_slot_get(struct svm_cpy_host_pool_bucket *bucket, u32 bucket_idx,
                                       struct svm_cpy_host_pool_slot *slot)
{
    u32 slot_idx;
    if ((bucket->base_va == 0) || (bucket->free_num == 0)) {
        return DRV_ERROR_BUSY;                                  // 桶未建好或已满
    }
    slot_idx = (u32)bitmap_find_next_zero_area(bucket->bitmap, bucket->chunk_num, 0, 1, 0);
    if (slot_idx >= bucket->chunk_num) {
        return DRV_ERROR_BUSY;                                  // 没有空闲位
    }
    bitmap_set(bucket->bitmap, (int)slot_idx, 1);
    bucket->free_num--;
    slot->bucket_idx = bucket_idx;
    slot->slot_idx = slot_idx;
    slot->va = bucket->base_va + (u64)slot_idx * bucket->chunk_size;   // 算出 chunk 起始 VA
    return DRV_ERROR_NONE;
}
```
[api/master/svm_cpy_host_pool.c:161-182](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L161-L182)

`svm_cpy_host_pool_slot_get` 是它的加锁外壳，先选桶再加全局互斥锁（[svm_cpy_host_pool.c:196-216](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L196-L216)）。

**(3) 池子如何被用起来（H2D 中转）**

回到 svm_cpy.c，看 H2D 的池子路径：先把用户数据 `memcpy_s` 进池子 chunk，再以「池子 VA（SVM 管理）→ 设备」发起一次正常同步拷贝：

```c
static int svm_mem_sync_copy_h2d_by_pool(struct svm_copy_va_info *src_info, struct svm_copy_va_info *dst_info)
{
    struct svm_cpy_host_pool_slot slot = {0};
    struct svm_copy_va_info host_svm_info;
    int ret;

    ret = svm_cpy_host_pool_slot_get(src_info->size, &slot);   // 借一个 chunk
    if (ret != DRV_ERROR_NONE) { return ret; }

    ret = svm_memcpy_s((void *)(uintptr_t)slot.va, src_info->size,
                       (void *)(uintptr_t)src_info->va, src_info->size);   // CPU 搬进 chunk
    if (ret != 0) { ... ret = DRV_ERROR_INVALID_VALUE; goto out; }

    svm_copy_va_info_pack(slot.va, src_info->size, svm_get_host_devid(), &host_svm_info);
    ret = svm_sync_copy(&host_svm_info, dst_info);             // SVM主机内存 → 设备

out:
    svm_cpy_host_pool_slot_put(&slot);                          // 归还 chunk
    return ret;
}
```
[api/master/svm_cpy.c:45-70](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L45-L70) —— D2H 的对称实现见 [svm_cpy.c:72-99](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L72-L99)（先设备→chunk，再 chunk→用户主机地址）。

**(4) 回退路径：注册到 master**

当池子不可用（返回 `BUSY` 或 `NOT_SUPPORT`），H2D 回退到 `_svm_mem_sync_copy`：对非 PCIe 连接，它会把主机内存注册（pin）到设备 master，搬完再注销：

```c
static int _svm_mem_sync_copy(enum svm_cpy_dir dir, struct svm_copy_va_info *src_info,
                              struct svm_copy_va_info *dst_info)
{
    ...
    if ((svm_va_is_in_range(host_va, size) == false) &&
        (svm_get_device_connect_type(user_devid) != HOST_DEVICE_CONNECT_TYPE_PCIE)) {
        flag |= REGISTER_TO_MASTER_FLAG_PIN;
        ret = svm_register_to_master(user_devid, &register_va, flag);   // 临时注册
        ...
        if (ret == DRV_ERROR_NONE) { is_register = true; }
    }
    ret = svm_sync_copy(src_info, dst_info);
    if (is_register) {
        (void)svm_unregister_to_master(user_devid, &register_va, flag);  // 注销
    }
    return ret;
}
```
[api/master/svm_cpy.c:448-484](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L448-L484)

**(5) 生命周期：懒初始化 + 自注册回调**

池子用状态机 `UNINIT→READY/FAIL` 做懒初始化，且只对 host devid 生效。它通过 SVM 的回调注册接口把自己挂到「设备打开后/关闭前」的钩子上，靠 `constructor(SVM_INIT_PRI_FINAL)` 在库加载时完成注册：

```c
static int svm_cpy_host_pool_dev_init(u32 devid)
{
    if (devid != svm_get_host_devid()) { return DRV_ERROR_NONE; }   // 只在 host 上建
    (void)pthread_mutex_lock(&g_svm_cpy_host_pool_mutex);
    ret = svm_cpy_host_pool_init();
    (void)pthread_mutex_unlock(&g_svm_cpy_host_pool_mutex);
    ...
}

static void __attribute__((constructor(SVM_INIT_PRI_FINAL))) svm_cpy_host_pool_module_init(void)
{
    (void)svm_register_ioctl_dev_init_post_handle(svm_cpy_host_pool_dev_init);
    (void)svm_register_ioctl_dev_uninit_pre_handle(svm_cpy_host_pool_dev_uninit);
}
```
[api/master/svm_cpy_host_pool.c:225-268](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L225-L268)

> 桶内存来自 `svm_mem_malloc(..., MEM_HOST | ...)`，即「SVM 管理的主机内存」——这正是设备 DMA 能认识的地址来源（[svm_cpy_host_pool.c:86-104](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L86-L104)）。

#### 4.2.4 代码实践

**实践目标**：通过修改池子容量，理解「池子满 → 回退路径」的行为切换。

**操作步骤**（源码阅读 + 配置修改型实践）：

1. 阅读 [svm_cpy_host_pool.c:24-30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L24-L30)，记录三个桶的容量。
2. 把 `SVM_CPY_HOST_POOL_512K_NUM` 从 `8U` 临时改为 `1U`（即 512KB 桶只有 1 个 chunk）。
3. 用 `bash build.sh --pkg --soc=ascend950` 重新编译部署（参见 u1-l2）。
4. 用一个测试程序连续发起多个 512KB 的 H2D 拷贝（待本地验证）。

**需要观察的现象 / 预期结果**：

- 第 1 个 512KB 拷贝命中池子（走 `svm_mem_sync_copy_h2d_by_pool`，`slot_get` 成功）。
- 第 2 个并发请求到来时，512KB 桶已满，`svm_cpy_host_pool_slot_get` 返回 `DRV_ERROR_BUSY`，于是 `svm_mem_sync_copy_h2d` 落到 `_svm_mem_sync_copy` 回退路径。
- 在日志中应能看到从「池子拷贝」切换到「注册到 master」的行为差异。

> 若无运行环境，可改为纯阅读实践：在 [svm_cpy.c:486-498](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L486-L498) 处画一张「`ret` 取值 → 走哪条路径」的判定表，说明 `DRV_ERROR_BUSY` 与 `DRV_ERROR_NOT_SUPPORT` 是仅有的两个「允许回退」的返回值，其它错误会直接上报。

#### 4.2.5 小练习与答案

**练习 1**：为什么池子要分成 128B / 4KB / 512KB 三个桶，而不是一个大池子按需切分？

**参考答案**：定长桶 + 位图使得分配/回收是 O(1) 的「找零位置位」操作，无需维护按字节对齐的空闲链表，锁持有时间极短；分多个尺寸档位则是为了减少「小请求占用大 chunk」的内部碎片浪费（如 64 字节请求只会占用一个 128B chunk，而不是 512KB）。

**练习 2**：池子为什么只在 host devid 上初始化？

**参考答案**：池子是「主机侧中转缓冲」，只有 Host 进程发起的 H2D/D2H 才需要它；设备侧不发起这种「普通主机内存」拷贝。`svm_cpy_host_pool_dev_init` 开头的 `if (devid != svm_get_host_devid()) return;` 直接跳过非 host 设备，避免在每张卡上都重复申请主机内存（[svm_cpy_host_pool.c:225-241](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy_host_pool.c#L225-L241)）。

---

### 4.3 svm_ipc：跨进程设备内存共享

#### 4.3.1 概念说明

拷贝是「把数据搬一份过去」，共享则是「让另一方能直接看到我这块物理内存，不用搬」。SVM 的共享由 `svm_ipc.c` 对外承接，对应接口族（声明在 [pkg_inc/ascend_hal_base.h:2348](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2348) 附近）：

- `halShmemCreateHandle(vptr, byte_count, name, name_len)`：拥有者为一**已存在**的设备内存创建共享句柄，返回一个字符串 `name`。
- `halShmemOpenHandleByDevId(dev_id, name, vptr)`：消费者在自己的设备上打开这个 `name`，得到一个**映射到同一块物理内存**的本地 VA。
- `halShmemCloseHandle(vptr)` / `halShmemDestroyHandle(name)`：分别解除映射、销毁句柄。

底层的真正共享原语是 **CASM（Cross Application Shared Memory，跨应用共享内存）**。CASM 的核心是一个由内核管理的 `key`（u64）：拥有者用设备 VA 换取一个 key，内核记录这块内存的「属主信息」；消费者拿 key 去内核查出属主的真实 VA，再在自己的地址空间里建一张页表，把本地 VA 指向同一块物理页。其上还叠加了一层「白名单」——只有被授权的进程（tgid）才能映射成功。

跨进程传递的是 `name`（一个字符串），而不是裸 key。这是因为：进程间最通用的交换方式就是字符串（可通过管道、共享文件、RPC 等任意通道传递），而 key + 属主信息是二进制结构、还含 0 字节，所以需要一个「0 字节安全的字符串编码」。

#### 4.3.2 核心流程

**创建（拥有者侧）**：

```
halShmemCreateHandle(va, size, name, name_len)
   │
   ├── svm_ipc_create_handle(va, size, &key)
   │     ├── svm_get_prop: 取内存属性，校验 SVM_FLAG_CAP_IPC_CREATE
   │     ├── svm_share_get_src_aligned_size: 算对齐后大小
   │     └── svm_casm_create_key  ──ioctl──>  内核: 记录属主信息，返回 key
   │
   └── svm_ipc_format_name(name, name_len, key)
         └── 把 key + 属主信息(src_va) 编码成 0 字节安全的字符串 name
```

**打开（消费者侧）**：

```
halShmemOpenHandleByDevId(dev_id, name, &vptr)  →  halShmemOpenHandleV2
   │
   ├── svm_ipc_parse_name(name) → 还原 key + 属主 src_va
   │     （跨 server 时先把属主信息写进本端内核 casm_cs）
   │
   └── svm_ipc_open_handle(dev_id, key, &opened_va)
         ├── svm_casm_get_src_va_ex ──ioctl──> 内核: 按 key 查属主 VA
         ├── svm_ipc_malloc_opened_va: 申请一段本地 VA（VA_ONLY，不分配物理页）
         ├── svm_casm_mem_map: pin 属主内存 + 建页表（本地 VA → 属主物理页）
         └── svm_ipc_set_opened_src_info: 把属主信息挂在本地 handle 上（供拷贝看穿用）
```

**字符串编码的关键约束**：`struct svm_ipc_info`（key、server_id、udevid、tgid、va、size 等，共 `IPC_INFO_LEN` 字节）要塞进一个 C 字符串。但它可能含 `0x00`，而 `0x00` 是 C 字符串结束符。解法是「替换位图」：

- 每 7 个信息字节用 1 个位图字节记录（最高位固定为 1），即压缩比 7:1。
- 原值为 0 的字节替换成 `0x01`，并在位图对应位置 1；解析时据位图把 `0x01` 还原回 `0x00`。
- 位图字节最高位恒为 1，保证位图自身也不含 0。

数学上，对 `n` 个信息字节所需位图字节数为：

\[
b = \left\lceil \frac{n}{7} \right\rceil
\]

总长度 `n + b` 必须 `< SVM_MAX_IPC_NAME_SIZE(65)`，代码用 `static_assert` 在编译期保证（[svm_ipc.c:63](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L63)）。

#### 4.3.3 源码精读

**(1) 信息结构与 0 字节安全编码**

要跨进程传递的核心信息被打包成 `struct svm_ipc_info`：

```c
struct svm_ipc_info {
    u8 ver;            // 编码版本
    u8 cs_valid;       // 是否跨 server（cross-server）
    u16 rsv;
    u16 server_id;     // 属主所在 server
    u16 udevid;        // 属主的全局设备号
    int owner_pid;     // 属主进程
    int tgid;
    u64 va;            // 属主的设备 VA
    u64 size;
    u64 key;           // 内核共享 key
};
```
[api/master/svm_ipc.c:45-56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L45-L56)

编码函数 `svm_ipc_format_name` 把上述结构写进 `name` 前 `IPC_INFO_LEN` 字节，后接 `IPC_REPLACE_BITMAP_LEN` 字节位图；扫描每个信息字节，遇到 0 就在位图标记并把该字节改成 1：

```c
for (i = 0; i < IPC_INFO_LEN; i++) {
    if (ipc_info_byte[i] == 0) {
        u32 bitmap_byte, bitmap_bit;
        svm_ipc_info_byte_to_replace_bitmap(i, &bitmap_byte, &bitmap_bit);
        replace_bitmap[bitmap_byte] |= (u8)(0x1 << bitmap_bit);   // 记录：这里原本是 0
        ipc_info_byte[i] = 1;                                     // 替换为非 0
    }
}
```
[api/master/svm_ipc.c:271-284](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L271-L284)（解析 `svm_ipc_parse_name` 是其逆过程，见 [svm_ipc.c:183-221](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L183-L221)）。

> 位图字节初始化时先 `|= 0x1 << 7`（[svm_ipc.c:271-273](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L271-L273)），确保最高位恒 1，于是整个 `name` 不含 0 字节、可安全作 C 字符串传递。

**(2) 创建句柄：换 key**

`svm_ipc_create_handle` 校验地址能力位 `SVM_FLAG_CAP_IPC_CREATE`、算对齐大小后，调 CASM 换取 key：

```c
static int svm_ipc_create_handle(u64 va, u64 size, u64 *key)
{
    ...
    if (!svm_flag_cap_is_support_ipc_create(prop.flag)) {     // 能力位校验
        return DRV_ERROR_PARA_ERROR;
    }
    ret = svm_share_get_src_aligned_size(prop.devid, prop.flag, va, size, &aligned_size);
    ...
    svm_dst_va_pack(prop.devid, DEVDRV_PROCESS_CP1, va, aligned_size, &dst_va);
    ret = svm_casm_create_key(&dst_va, key);                  // 换 key
    ...
}
```
[api/master/svm_ipc.c:289-328](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L289-L328)

CASM 层 `svm_casm_create_key` 把请求打包成 ioctl 下发给内核，内核返回 key：

```c
int svm_casm_create_key(struct svm_dst_va *dst_va, u64 *key)
{
    ...
    para.task_type = dst_va->task_type; para.va = dst_va->va; para.size = dst_va->size;
    ret = svm_cmd_ioctl(dst_va->devid, SVM_CASM_CREATE_KEY, (void *)&para);   // 阻塞 ioctl
    ...
    *key = para.key;
    return 0;
}
```
[share/casm/casm.c:24-48](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/share/casm/casm.c#L24-L48)

> casm.h 头部的注释把 CASM 的能力面总结得很清楚：create/destroy 建白名单内存、add task 加受信进程、map 时「按 key 查属主 + 校验白名单 + pin 物理内存 + 存映射上下文」（[inc/share/casm.h:17-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/inc/share/casm.h#L17-L23)）。

**(3) 打开句柄：建映射**

消费者侧 `svm_ipc_open_handle` 是共享的核心：查属主 VA → 申请本地空壳 VA → 建页表映射 → 记录属主信息。注意它申请本地 VA 时用的是 `SVM_FLAG_ATTR_VA_ONLY`（只预留虚拟地址，不分配物理页，因为物理页用的是属主的）：

```c
static int svm_ipc_open_handle(u32 devid, u64 key, u64 *opened_va, uint64_t flag)
{
    struct svm_global_va src_va;
    ...
    ret = svm_casm_get_src_va_ex(devid, key, &src_va, &update_va, &access_va);  // 查属主 VA
    ...
    ret = svm_ipc_malloc_opened_va(devid, svm_get_non_dev_align_size(src_va.size), align, opened_va); // 申请本地空壳 VA
    ...
    ret = svm_casm_mem_map(devid, *opened_va, src_va.size, key, svm_ipc_open_flag_to_casm_flag(flag)); // 建映射
    ...
    svm_mod_va_devid(*opened_va, devid);
    ret = svm_ipc_set_opened_src_info(*opened_va, &src_va);   // 记录属主信息
    ...
}
```
[api/master/svm_ipc.c:384-433](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L384-L433)

其中本地空壳 VA 的申请带了一组能力位（可被拷贝、可 memset、可再被 IPC 共享关闭等），并标记 `VA_ONLY`：

```c
static int svm_ipc_malloc_opened_va(u32 devid, u64 size, u64 align, u64 *opened_va)
{
    u64 svm_flag = 0;
    svm_flag |= SVM_FLAG_CAP_IPC_CLOSE;
    svm_flag |= SVM_FLAG_CAP_MEMSET;
    svm_flag |= SVM_FLAG_CAP_SYNC_COPY;
    ...
    svm_flag |= SVM_FLAG_ATTR_VA_ONLY;          // 只预留 VA
    ...
    return svm_malloc(&start, size, align, svm_flag, &location);
}
```
[api/master/svm_ipc.c:335-363](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L335-L363)

`svm_casm_mem_map` 完成「pin 属主内存 + 建页表」两步：

```c
int svm_casm_mem_map(u32 devid, u64 va, u64 size, u64 key, u32 flag)
{
    ...
    ret = svm_casm_mem_pin(devid, va, size, key);             // pin 属主物理内存
    ...
    svm_dst_va_pack(devid, PROCESS_CP1, va, size, &dst_info);
    ret = svm_smm_client_map(&dst_info, &src_info, svm_casm_flag_to_smm_flag(flag)); // 建页表映射
    if (ret != 0) { (void)svm_casm_mem_unpin(devid, va, size); ... }
    return 0;
}
```
[share/casm_map/casm_map.c:28-50](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/share/casm_map/casm_map.c#L28-L50)

**(4) 对外入口的三段式**

以 `halShmemCreateHandle` 为例，对外接口是干净的「校验 + pipeline 守护 + 调内部实现」三段式：

```c
DVresult halShmemCreateHandle(DVdeviceptr vptr, size_t byte_count, char *name, uint32_t name_len)
{
    u64 key; int ret;
    if ((name == NULL) || (byte_count == 0) || (vptr == 0) || (name_len == 0) || (name_len < SVM_MAX_IPC_NAME_SIZE)) {
        return DRV_ERROR_INVALID_VALUE;
    }
    svm_use_pipeline();
    ret = svm_ipc_create_handle((u64)vptr, (u64)byte_count, &key);
    if (ret == 0) {
        ret = svm_ipc_format_name(name, name_len, key);
        if (ret != 0) { (void)svm_ipc_destroy_handle(key); ... }
    }
    svm_unuse_pipeline();
    return (DVresult)ret;
}
```
[api/master/svm_ipc.c:476-501](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L476-L501)

> `svm_use_pipeline`/`svm_unuse_pipeline` 是把共享操作纳入流水线串行化的守护对（避免创建/销毁与流式任务并发冲突）。

**(5) 拷贝如何「看穿」共享地址（拷贝 ↔ 共享的汇合点）**

这是把 4.1 与 4.3 串起来的关键。当 `halMemcpy` 发现一端是经 IPC 打开的共享 VA（带 `SVM_FLAG_CAP_IPC_CLOSE`），它会查当年 open 时挂上去的属主信息（`svm_ipc_query_src_info`），把虚拟的本地地址「翻译」回真正持有物理内存的属主设备，从而**直接对属主设备发起拷贝**，而不必先搬到本地再转发：

```c
static int svm_copy_try_query_shared_info(struct svm_copy_va_info *info, struct svm_copy_shared_info *shared)
{
    struct svm_global_va src_info;
    ...
    ret = svm_ipc_query_src_info(info->va, info->size, &src_info);  // 查属主信息
    if (ret == DRV_ERROR_NONE) {
        type = SVM_COPY_SHARED_IPC;                                  // 命中 IPC 共享
    } else {
        ret = vmm_query_ipc_src_info(info->va, info->size, &src_info); // 否则试 VMM 共享
        if (ret != DRV_ERROR_NONE) { return DRV_ERROR_NONE; }        // 都不是：普通地址
        type = SVM_COPY_SHARED_VMM;
    }
    ret = svm_copy_real_info_pack(&src_info, &shared->real_info);   // 翻译为属主设备地址
    ...
}
```
[api/master/svm_cpy.c:228-256](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L228-L256)

`svm_copy_real_info_pack` 用 UDA 把属主的 `udevid` 翻译成本地 `devid`，并查出属主进程的 `host_tgid`（[svm_cpy.c:197-226](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L197-L226)）。随后 `svm_memcpy_sync` 据此重算方向并拷贝（[svm_cpy.c:594-625](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L594-L625)）。

> 这也解释了 `struct svm_copy_va_info` 里 `host_tgid` / `is_share` 两个字段为何存在：它们只在共享场景下被填充，让底层拷贝能感知「这块地址真正的属主是谁」。

#### 4.3.4 代码实践

**实践目标**：用源码跟踪讲清楚「不同设备/进程间如何传递共享句柄并最终映射到同一块物理内存」，回答规格中提出的第二个问题。

**操作步骤**（源码阅读型实践）：

1. **拥有者侧**：跟踪 [halShmemCreateHandle](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L476-L501) → `svm_ipc_create_handle` → `svm_casm_create_key`，记录「设备 VA → 内核 key」的获取过程。
2. **句柄传递**：跟踪 `svm_ipc_format_name`，说明 key 与属主信息如何被编码成可跨进程传递的字符串 `name`（注意 0 字节替换）。
3. **消费者侧**：跟踪 [halShmemOpenHandleV2](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_ipc.c#L629-L665) → `svm_ipc_parse_name` 还原 key → `svm_ipc_open_handle`：① `svm_casm_get_src_va_ex` 查属主 VA；② `svm_ipc_malloc_opened_va` 申请本地空壳 VA；③ `svm_casm_mem_map` 建页表。
4. **汇合点**：跟踪 [svm_copy_try_resolve_shared_info](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_cpy.c#L258-L294)，确认拷贝能看穿共享 VA。

**需要观察的现象 / 预期结果**：

- 整条共享链路上，**跨进程传递的只有字符串 `name`**；真正建立「同一物理内存」关系的是内核 CASM 模块（key → 属主信息 → pin → 建页表）。
- 消费者得到的 `opened_va` 是一个**本地空壳 VA**（`VA_ONLY`），它本身没有物理页，物理页来自拥有者——这就是「共享同一块物理内存」的本质。
- 后续对该 `opened_va` 的拷贝会被「看穿」到属主设备，体现共享与拷贝两套机制的协作。

> 运行层面验证（待本地验证）：参考 SVM README 中「内存共享功能的业务使用流程」——启动两个进程分别绑定 dev0/dev1，进程 0 `halMemAlloc` + `halShmemCreateHandle`，把 name 传给进程 1，进程 1 `halShmemOpenHandleByDevId` 后读取数据，应与进程 0 写入一致。

#### 4.3.5 小练习与答案

**练习 1**：共享句柄为什么用字符串 `name` 而不是直接传递 `key`（u64）？

**参考答案**：`key` 只是内核侧的一个标识，消费者还需要知道属主的 `udevid/server_id/tgid/va/size` 才能完成映射。把这些二进制字段连同 key 一起编码进字符串 `name`，既可通过任意进程间通道（管道、RPC、文件）传递，又用「替换位图」解决了二进制数据含 0 字节无法作 C 字符串的问题。

**练习 2**：`svm_ipc_open_handle` 里为什么要先 `svm_ipc_malloc_opened_va`（带 `VA_ONLY`）再 `svm_casm_mem_map`，而不是直接用属主的 VA？

**参考答案**：每个进程有自己独立的虚拟地址空间，属主的 VA 在消费者进程里未必有效/未占用；所以消费者要申请一段**属于自己的本地 VA**作为访问窗口。由于物理页用的是属主的（共享的就是这块物理内存），本地 VA 只需预留虚拟地址（`VA_ONLY`），再通过 `svm_casm_mem_map` 建页表把本地 VA 指向属主物理页即可。

**练习 3**：`halShmemSetPidHandle` / `halShmemSetPodPid` 这类接口在共享流程中起什么作用？

**参考答案**：它们是 CASM「白名单」管理接口，把指定 pid/tgid 加到某 key 允许访问的受信进程集合（最终调 `svm_casm_add_task` → ioctl `SVM_CASM_OP_TASK`，见 [casm.c:66-112](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/share/casm/casm.c#L66-L112)）。只有白名单内的进程 `svm_casm_mem_map` 才会成功，这是共享内存的访问控制手段。

---

## 5. 综合实践

**综合任务**：用一张「时序 + 数据流」图把本讲三个最小模块串起来，讲清下面这个端到端场景。

> 场景：进程 A 在 dev0 上 `halMemAlloc` 了一块设备内存，写入数据后用 `halShmemCreateHandle` 共享给进程 B；进程 B 在 dev1 上 `halShmemOpenHandleByDevId` 拿到访问窗口后，想把这块数据搬到自己（dev1）的本地内存里。

请按下列步骤完成：

1. **共享建立阶段**：画出 A 侧「VA → casm_create_key(key) → format_name(name)」与 B 侧「parse_name(key) → casm_get_src_va → malloc_opened_va(VA_ONLY) → casm_mem_map」的两条时序，标注每一步跨进程/进内核的边界。
2. **拷贝阶段**：B 调用 `halMemcpy` 把共享 VA 搬到本地。画出 `halMemcpy → svm_memcpy_sync → svm_copy_try_resolve_shared_info`（看穿到 dev0）→ 重算方向为 D2D → `svm_mem_sync_copy_diff_d2d` 的路径。
3. **优化点标注**：在图上标出「`svm_cpy_host_pool` 在哪类拷贝（普通主机内存的 H2D/D2H）才会被触发」，并说明为什么这个综合场景里它**不会**被触发（因为两端都是 SVM 管理的设备内存，不在主机缓冲池的适用范围内）。
4. **一句话结论**：用一句话说明「拷贝」与「共享」在本场景中如何协作——共享负责让 B 看到同一块物理内存，拷贝负责把数据从这块共享内存搬到 B 的本地内存，且拷贝能自动看穿共享地址直达属主设备。

> 若有 ascend950 环境（待本地验证），可进一步用 `msnpureport`（见 u5-l2）或驱动日志观察一次共享 + 拷贝过程中内核侧 `SVM_CASM_*` 与 sync_copy ioctl 的触发顺序，与你的图对照。

---

## 6. 本讲小结

- **方向分发**：svm_cpy 用 `copy_dir_get_by_devid` 判定 H2H/H2D/D2H/D2D，再用 `g_sync_copy[dir]` 函数指针表分发，是典型的表驱动设计。
- **同步阻塞的根因**：整条 `halMemcpy → svm_sync_copy → g_copy_ops[devid]->sync_copy` 链路是同步的，唯一返回点在底层阻塞 ioctl 之后；没有「提交—等待」两段式（异步拷贝才需要 `halMemCpyAsyncWaitFinish`）。
- **主机缓冲池**：`svm_cpy_host_pool` 用预注册的 SVM 主机内存（128B/4KB/512KB 三桶 + 位图）作为「中转站」，让普通主机内存的 H2D/D2H 拷贝免去逐次注册开销；池子满或未初始化时回退到 `svm_register_to_master`。
- **共享原语 CASM**：跨进程/跨设备共享的内核原语是 `key`；拥有者用 VA 换 key，消费者用 key 查属主 VA 并建页表，把本地空壳 VA（`VA_ONLY`）指向属主物理内存；叠加白名单做访问控制。
- **字符串化句柄**：key + 属主信息经「7:1 替换位图」编码成 0 字节安全的字符串 `name`，是跨进程传递的统一载体。
- **拷贝与共享的汇合**：拷贝路径能「看穿」IPC/VMM 共享地址，经 UDA 翻译出属主设备，直接对属主发起搬运——这是 `svm_copy_va_info` 中 `host_tgid`/`is_share` 字段存在的意义。

---

## 7. 下一步学习建议

- **u4-l5（SVM v3 地址空间与分配器架构）**：本讲的 `svm_ipc_malloc_opened_va`、`svm_malloc_opened_va` 都依赖地址空间分配器；下一讲深入 MGA/cache_malloc/gen_allocator 多级分配器，理解 VA 是如何被高效分配回收的。
- **回看 u3-l3（UDA）与 u3-l2（HDC）**：本讲「看穿共享地址」用了 `uda_get_devid_by_udevid_ex`，底层 `svm_sync_copy`/`svm_umc_h2d_send` 走的也是 HDC/ioctl 通道；想彻底搞清「设备间消息怎么送达」，可重读 HDC 与 UDA 两讲。
- **拓展阅读**：若对跨主机（cross-server）共享感兴趣，可阅读 `svm_ipc.c` 中 `cs_valid`/`svm_casm_cs_*` 相关分支，以及 `inc/share/casm_cs.h`，理解跨 server 场景下属主信息的「按需下发—清理」机制。
