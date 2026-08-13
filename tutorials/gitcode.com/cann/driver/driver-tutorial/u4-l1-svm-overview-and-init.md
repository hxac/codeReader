# SVM 模块总览与初始化

## 1. 本讲目标

本讲进入 HAL 层体量最大的旗舰模块之一——**SVM（Shared Virtual Memory，共享虚拟内存）**。读完本讲，你应当能够：

- 说清 SVM 在昇腾驱动栈中「管什么、给谁用」的核心职责。
- 区分 SVM 仓库里 `v2` 与 `v3` 两套实现，并知道它们分别对应哪种芯片。
- 顺着 `halMemAgentOpen` → `svm_master_init` → `svm_ioctl_dev_init` 这条主链，说清「进程级一次性初始化」与「每设备初始化」两个层次。
- 解释 Host 侧 SVM 模块如何通过字符设备 `/dev/davinci_manager` 与 Device（内核态）侧建立交互。

本讲只讲「初始化」，内存的申请/释放/拷贝/共享留给后续讲义（u4-l2 ~ u4-l5）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：为什么需要「共享虚拟内存」。**
在传统的 GPU/NPU 编程模型里，Host（CPU 侧）内存和 Device（NPU 侧）内存是两套独立地址空间，数据搬运要显式调用 `memcpy`。而「共享虚拟内存」让同一块物理内存，在 Host 进程和 Device 上能用**同一个虚拟地址**访问。好处是：上层 Runtime（如 acl）只要拿到一个指针，既能在 Host 上读、也能在 Device 上算，省去地址翻译与显式拷贝。SVM 就是把这件事「在驱动里」做扎实。

**直觉二：虚拟地址是「预留」出来的。**
进程的虚拟地址空间很大（64 位下理论可达 \(2^{64}\) 字节），但物理内存有限。SVM 的核心套路是：**先在虚拟地址空间里预留一大段连续区间**（比如 `DEVMM_SVM_MEM_START` 开始的一大块），以后每次申请内存，都先从这段预留区间里「分配一段虚拟地址」，再用 `mmap` 把它和真正申请到的物理页绑定（建立页表）。这样地址分配（快、纯软件）和物理分配（慢、需进内核）就被解耦了。

**直觉三：用户态库 vs 内核态驱动。**
回顾 [u3-l1](u3-l1-hal-overview-and-api.md)：HAL 编译为用户态动态库 `libascend_hal.so`，SVM 的代码就在其中。但真正管物理页、页表、中断的是内核态的 `drv_davinci.ko`。用户态和内核态之间的通道是**字符设备 `/dev/davinci_manager`** + **`ioctl`** 系统调用。SVM 的初始化，本质就是「打开这个字符设备，并通过 ioctl 让内核把本进程的 SVM 状态准备好」。

> 名词速查：`ioctl`（input/output control）是 Linux 下「对设备文件发命令」的系统调用；`mmap`（memory map）是把文件/设备内存「映射」进进程虚拟地址空间的系统调用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 所在层 | 作用 |
|------|--------|------|
| [src/ascend_hal/svm/README.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md) | 文档 | SVM 模块官方说明，给出整体职责与接口分类 |
| [src/ascend_hal/svm/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/CMakeLists.txt) | 构建 | 按 `PRODUCT` 选择编译 `v2` 还是 `v3` |
| [src/ascend_hal/svm/v3/api/master/svm_master_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c) | 用户态（v3） | 进程级初始化编排、公共入口 `halMemAgentOpen` |
| [src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c) | 用户态（v3） | 打开字符设备、`ioctl` 陷入内核、子模块 post-init 注册表 |
| [src/ascend_hal/svm/v2/common/devmm_svm_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/common/devmm_svm_init.c) | 用户态（v2） | v2 的设备 fd + mmap 化初始化范式 |
| [pkg_inc/ascend_hal_base.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h) | 对外头文件 | 声明公共接口 `halMemAgentOpen`/`halMemAgentClose` |
| [src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h) | 内核态 | 定义 `SVM_MAX_AGENT_NUM`/`SVM_MAX_DEV_NUM` 等设备容量常量 |

> ⚠️ 路径提醒：本讲规格里提到的 `src/ascend_hal/svm/v3/common/devmm_svm_init.c` **在源码中并不存在**。`devmm_svm_init.c` 实际位于 `v2/common/`；v3 把这部分职责拆进了 `api/master/svm_master_init.c`（高层编排）与 `sys_cmd/svm_ioctl.c`（底层 ioctl）。本讲会如实按真实路径讲解，不编造文件。

## 4. 核心概念与源码讲解

### 4.1 SVM 模块定位与核心职责

#### 4.1.1 概念说明

SVM 是昇腾 AI 处理器平台的**设备侧内存管理模块**。它的职责可以用一句话概括：**替上层 Runtime（acl）把设备内存的「申请、释放、拷贝、查询、共享」全部管好，并对外暴露一组 `hal*` 接口**。

它不是孤立的内存分配器。SVM 内部其实包含了多个子能力，README 把它们分成几大类：

- **常规申请/释放/赋值**：`halMemAlloc` / `halMemFree` / `drvMemsetD8`——从预留虚拟地址段分配并 `mmap` 映射，再进内核申请物理页、建页表。
- **VMM（分离式申请）**：把「虚拟地址」和「物理内存」分开申请，再动态建立映射（`halMemAddressReserve`/`halMemCreate`/`halMemMap`），用以复用物理内存、减少碎片。
- **拷贝**：`halMemcpy`，H2D/D2D 同步阻塞式拷贝。
- **共享**：设备间共享（`halShmem*`）与 Host-Device 间共享（`halHostRegister`）。
- **UVM / SOMA**：在 SVM 之上叠加的「统一虚拟内存」「流式异步内存池」增强能力（见 README 后半部分）。

本讲只关心这棵大树的「根」——初始化。

#### 4.1.2 核心流程

从外部看，SVM 的初始化入口是公共接口 `halMemAgentOpen`。它被上层在两个时机调用：

1. **应用初始化时**：APP 进程调用 `aclrtSetDevice`，Runtime 会沿调用链最终触发 SVM 初始化（见 README 说明）。
2. **其他驱动模块需要使用设备内存时**：例如 TRS（任务调度）模块在申请流队列内存前，会调用 `halMemAgentOpen`。

无论谁触发，SVM 的初始化都做两件事：

```
aclrtSetDevice (上层)
      │
      ▼
halMemAgentOpen(devid, flag)        ← 公共入口（pkg_inc/ascend_hal_base.h）
      │
      ├── ① 进程级一次性初始化：svm_master_init()
      │       打开「Host 设备」、创建共享日志（只做一次，全局标志位守护）
      │
      └── ② 每设备初始化：svm_device_open(devid)
              svm_ioctl_dev_init(devid)
                 ├─ devid → udevid（UDA 翻译，见 u3-l3）
                 ├─ 打开字符设备 /dev/davinci_manager
                 ├─ ioctl(DAVINCI_INTF_IOCTL_OPEN) 陷入内核
                 └─ 依次调用所有子模块的 post_init 回调
```

#### 4.1.3 源码精读

README 对初始化的描述非常精炼，先看这段官方定义：

[src/ascend_hal/svm/README.md:11-15](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L11-L15) —— 说明 SVM 初始化接口是 `halMemAgentOpen`，由 `aclrtSetDevice` 触发，完成「模块管理结构体初始化」与「Host/Device 侧 SVM 模块交互」。

公共接口在对外头文件中的声明：

```c
DLLEXPORT DV_ONLINE drvError_t halMemAgentOpen(uint32_t devid, uint32_t flag);
```

[src/ascend_hal_base.h:2530](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2530) —— 对外声明。参数 `flag` 取 `SVM_AGENT_DEVICE`（设备）或 `SVM_AGENT_HOST`（主机），返回 `drvError_t`，成功为 `DRV_ERROR_NONE`。`DLLEXPORT` 表明它会被导出给上层（回顾 [u1-l5](u1-l5-public-headers-and-api.md) 的导出宏）。

#### 4.1.4 代码实践

**实践目标**：从对外头文件建立「SVM 接口全景」的第一印象。

**操作步骤**：
1. 打开 [pkg_inc/ascend_hal_base.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h)，定位到 `halMemAgentOpen`（约 2530 行）。
2. 在同一头文件中，用搜索功能查找 `halMemAlloc`、`halMemFree`、`halMemcpy`、`halMemAddressReserve`、`halShmemCreateHandle`，阅读它们的 Doxygen 注释（`@brief`/`@param`/`@return`）。
3. 对照 README 的「内存申请/释放/赋值」「VMM」「内存拷贝」「内存共享」几节，把每个接口归到对应大类。

**需要观察的现象**：所有这些接口都以 `devid`（逻辑设备号）为第一个参数，都返回 `drvError_t`，构成一套风格一致的设备内存 API。

**预期结果**：得到一张「接口名—所属能力大类—一句话用途」对照表，作为后续 u4-l2~u4-l5 的阅读索引。

#### 4.1.5 小练习与答案

**练习 1**：SVM 的「申请内存」为什么要先分配虚拟地址、再进内核申请物理页，而不是一步到位？
**答案**：因为虚拟地址分配是纯软件操作（从预留段里切一段），快且不需要陷入内核；而物理页分配、建页表必须由内核完成。把两者解耦后，可以灵活支持「先占虚拟地址、延迟分配物理页」（如 VMM、UVM 的按需分配）等多种策略。

**练习 2**：`halMemAgentOpen` 的 `flag` 参数有哪两种取值，分别代表什么？
**答案**：`SVM_AGENT_DEVICE`（设备）与 `SVM_AGENT_HOST`（主机），用于区分本次打开是为操作设备侧内存，还是主机侧内存。

---

### 4.2 v2 与 v3 的版本划分与目录组织

#### 4.2.1 概念说明

走进 `src/ascend_hal/svm/`，你会立刻看到两个并列的大目录：`v2/` 和 `v3/`。它们不是「新旧两版随便挑」，而是**按芯片代际严格二选一**。这是理解整个 SVM 模块的最重要的一点。

为什么会有两套？因为 `ascend910B`（A2/A3 架构）和 `ascend950`（A5 架构）的硬件内存子系统差异较大（例如 950 引入了 UB 超节点互联、SOMA 流式内存池、新的地址分配器），所以 SVM 对两类芯片各维护一套实现，互不干扰。

#### 4.2.2 核心流程

二选一由构建脚本决定。`src/ascend_hal/svm/CMakeLists.txt` 是分叉点：

```cmake
if (${PRODUCT} STREQUAL ascend910B)
    add_subdirectory(v2)
elseif(${PRODUCT} STREQUAL ascend950)
    add_subdirectory(v3)
endif()
```

[src/ascend_hal/svm/CMakeLists.txt:11-15](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/CMakeLists.txt#L11-L15) —— `PRODUCT` 变量来自 `build.sh --soc`（回顾 [u1-l2](u1-l2-build-and-deploy.md) 与 [u8-l3](u8-l3-multi-chip-and-build-config.md)）：`--soc=ascend910b` 编译 v2，`--soc=ascend950` 编译 v3。

两套实现的目录组织对比如下：

| 维度 | v2（ascend910B） | v3（ascend950） |
|------|------------------|-----------------|
| 顶层目录 | `command/` `common/` `devmm/` | `apbi/` `api/` `assign/` `command/` `criu/` `dbi/` `inc/` `mem_pool/` `mem_show_cfg/` `op/` `query/` `share/` `sys_cmd/` `umc/` `urma_adapt/` |
| 主逻辑位置 | 集中在 `devmm/devmm_svm.c`（单文件超大） | 按职责拆分到 `api/master/`、`assign/`、`op/` 等 |
| 初始化文件 | `common/devmm_svm_init.c`（fd + mmap） | `api/master/svm_master_init.c`（编排）+ `sys_cmd/svm_ioctl.c`（ioctl） |
| 地址分配 | `devmm_virt_*_heap.c` 多个堆 | `assign/` 下的多级分配器（va_allocator/cache_malloc/gen_allocator，见 [u4-l5](u4-l5-svm-allocator-architecture.md)） |
| 风格 | 较早期，过程式集中 | 分层 + 函数指针表注册，扩展性更强 |

一句话总结：**v3 把 v2 那个「大而全」的 `devmm_svm.c` 拆成了「按职责分层、靠注册表协作」的架构**。本讲后续 4.3、4.4 聚焦 v3，4.5 用 v2 的 `devmm_svm_init.c` 讲清「打开设备 + ioctl + mmap」这条最经典的初始化范式。

#### 4.2.3 源码精读

确认目录结构（实际 `find` 结果）：

- v2 子目录：`command`、`common`、`devmm`。
- v3 子目录：`apbi`、`api`、`assign`、`command`、`criu`、`dbi`、`inc`、`mem_pool`、`mem_show_cfg`、`op`、`query`、`share`、`sys_cmd`、`umc`、`urma_adapt`。

其中 v3 的 `api/master/` 承载对外的 `hal*` 实现（`svm_master_init.c`、`svm_alloc.c`、`svm_cpy.c`、`svm_vmm.c` 等）；`sys_cmd/` 承载与内核交互的 ioctl 层；`assign/` 承载各类地址分配器。

#### 4.2.4 代码实践

**实践目标**：亲手确认「编出来的到底是 v2 还是 v3」。

**操作步骤**：
1. 阅读 [src/ascend_hal/svm/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/CMakeLists.txt)，确认 `PRODUCT` 取值与 `v2`/`v3` 的映射关系。
2. 回顾 `build.sh` 的 `--soc` 参数（[u1-l2](u1-l2-build-and-deploy.md)），回答：用 `--soc=ascend910b` 编译时，`devmm_svm_init.c` 是否参与编译？用 `--soc=ascend950` 呢？

**需要观察的现象**：`devmm_svm_init.c`（在 v2 目录下）只在 ascend910B 构建中参与编译；ascend950 构建完全不进入 `v2/` 目录。

**预期结果**：理解「同一个公共接口 `halMemAgentOpen`，在两类芯片下背后是两套完全不同的实现」。

#### 4.2.5 小练习与答案

**练习 1**：如果有人让你在 ascend950 上调试 `devmm_svm_init.c` 里的初始化逻辑，合理吗？
**答案**：不合理。ascend950 走 v3 路径，`devmm_svm_init.c` 根本不会被编译进 950 的产物。950 的等价逻辑在 `svm_master_init.c` 与 `svm_ioctl.c` 中。

**练习 2**：v3 相比 v2 在目录组织上最大的变化是什么？
**答案**：v3 把原本集中在 `devmm/` 的逻辑，按职责拆分成了 `api`（对外接口）、`sys_cmd`（内核交互）、`assign`（地址分配）、`op`（操作）、`share`（共享）等多层，并用函数指针表注册的方式让子模块松耦合协作。

---

### 4.3 halMemAgentOpen 与 svm_master_init：进程级初始化编排

> 对应最小模块：`svm/v3/api/master`、`svm_master_init`。

#### 4.3.1 概念说明

这是 v3 初始化的「上层编排者」，住在 `src/ascend_hal/svm/v3/api/master/svm_master_init.c`。它解决两个层次的问题：

- **进程级一次性初始化（master init）**：有些资源整个进程只需初始化一次，比如「Host 设备」的打开、共享日志的创建。用一个全局标志位 `g_master_init_flag` 守护，保证只做一次。
- **每设备初始化（device open）**：每张 NPU 各自打开一次，用 `dev_status[]` 数组记录每张卡是否已打开，支持重复打开（幂等）。

这里有个关键概念叫「Host 设备」。在 SVM 的模型里，**主机（Host）本身也被当作一个特殊的「设备」来管理**，它的设备号由 `svm_get_host_devid()` 返回。内核侧的定义印证了这一点：

```c
#define SVM_MAX_DEV_NUM (SVM_MAX_DEV_AGENT_NUM + 1U) /* 65 device + 1 host */
```

[src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h:40](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/svm/v3/command/ioctl/def/svm_pub.h#L40) —— 65 个真实设备 + 1 个 host 槽位，`SVM_MAX_DEV_NUM = 66`。

#### 4.3.2 核心流程

`svm_master_init.c` 的初始化分两层调用关系（自顶向下）：

```
halMemAgentOpen(devid, flag)              [公共入口，217 行]
  ├─ 校验 flag、devid
  ├─ halDrvEventThreadInit(devid)         初始化事件线程
  └─ svm_dev_open(devid, 0)               [175 行]
        ├─ svm_master_init()              [145 行] 进程级一次性
        │     双检锁：if (g_master_init_flag) return;
        │     svm_master_init_locked()    [126 行]
        │       ├─ share_log_create(...)   创建共享日志
        │       └─ svm_device_open_locked(svm_get_host_devid())  打开 Host 设备
        └─ svm_device_open(devid)         [92 行] 每设备
              svm_device_open_locked(devid)  [34 行]
                └─ svm_ioctl_dev_init(devid)  ← 真正进内核（见 4.4）
```

两个关键设计：

1. **双检锁（double-checked locking）**：`svm_master_init` 先不加锁读 `g_master_init_flag`，为 1 直接返回；为 0 才加锁、再查一次、执行初始化。这样已初始化的快路径无锁开销。
2. **幂等打开**：`svm_device_open_locked` 发现 `dev_status[devid] != 0` 时返回 `DRV_ERROR_REPEATED_USERD`（重复使用），上层 `halMemAgentOpen` 把它统一翻译成 `DRV_ERROR_NONE`（成功），所以重复调用 `halMemAgentOpen` 是安全的。

#### 4.3.3 源码精读

先看全局状态与设备打开（带幂等）：

```c
static int dev_status[SVM_MAX_DEV_NUM];
static int g_master_init_flag = 0;
static pthread_mutex_t master_mutex = PTHREAD_MUTEX_INITIALIZER;

static int svm_device_open_locked(u32 devid)
{
    if (dev_status[devid] != 0) {
        return DRV_ERROR_REPEATED_USERD;   /* 支持重复打开 */
    }
    int ret = svm_ioctl_dev_init(devid);   /* 真正干活，见 4.4 */
    if (ret != 0) { return ret; }
    dev_status[devid] = 1;
    return DRV_ERROR_NONE;
}
```

[src/ascend_hal/svm/v3/api/master/svm_master_init.c:30-53](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L30-L53) —— `dev_status[]` 记录每设备状态；`svm_device_open_locked` 调 `svm_ioctl_dev_init` 完成实际打开。

进程级一次性初始化（双检锁）：

```c
int svm_master_init(void)
{
    if (g_master_init_flag == 1) { return DRV_ERROR_NONE; }   /* 快路径无锁 */
    pthread_mutex_lock(&master_mutex);
    if (g_master_init_flag == 0) {
        int ret = svm_master_init_locked();                   /* 真正初始化 */
        if (ret == 0) { g_master_init_flag = 1; }
    }
    pthread_mutex_unlock(&master_mutex);
    return ret;
}

static int svm_master_init_locked(void)
{
    share_log_create(HAL_MODULE_TYPE_DEVMM, SHARE_LOG_MAX_SIZE);
    int ret = svm_device_open_locked(svm_get_host_devid());   /* 打开 Host 设备 */
    if (ret != 0) { share_log_destroy(HAL_MODULE_TYPE_DEVMM); return ret; }
    return DRV_ERROR_NONE;
}
```

[src/ascend_hal/svm/v3/api/master/svm_master_init.c:126-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L126-L163) —— 双检锁守护一次性初始化；`svm_master_init_locked` 创建共享日志后打开「Host 设备」。

公共入口 `halMemAgentOpen`：

```c
drvError_t halMemAgentOpen(uint32_t devid, uint32_t flag)
{
    if (devmm_agent_open_close_flag_is_valid(flag) == false) { return DRV_ERROR_NOT_SUPPORT; }
    if (devid >= SVM_MAX_AGENT_NUM) { return DRV_ERROR_INVALID_VALUE; }

    int ret = halDrvEventThreadInit(devid);          /* 事件线程 */
    if (ret != DRV_ERROR_NONE) { return DRV_ERROR_PARA_ERROR; }

    ret = svm_dev_open(devid, 0);                     /* master_init + device_open */
    return (drvError_t)((ret == DRV_ERROR_REPEATED_USERD) ? DRV_ERROR_NONE : ret);
}
```

[src/ascend_hal/svm/v3/api/master/svm_master_init.c:217-242](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L217-L242) —— 校验 → 事件线程初始化 → `svm_dev_open`，并把「重复打开」翻译为成功。

而 `svm_dev_open` 把两件事串起来：

```c
int svm_dev_open(uint32_t devid, int devfd)
{
    int ret = svm_master_init();      /* 先保证进程级初始化完成 */
    if (ret != 0) { return ret; }
    return svm_device_open(devid);    /* 再做本设备打开 */
}
```

[src/ascend_hal/svm/v3/api/master/svm_master_init.c:175-184](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L175-L184) —— 「先 master、后 device」的顺序保证。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `halMemAgentOpen` 调用的完整路径，建立「两层初始化」的心智模型。

**操作步骤**（源码阅读型实践）：
1. 打开 [svm_master_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c)。
2. 从 `halMemAgentOpen`（217 行）开始，依次跳读：`svm_dev_open`（175 行）→ `svm_master_init`（145 行）→ `svm_master_init_locked`（126 行）→ `svm_device_open_locked`（34 行）。
3. 注意每处对返回值的处理：哪些错误会被「翻译」为成功？

**需要观察的现象**：
- `g_master_init_flag` 只在 `svm_master_init_locked` 返回 0（成功）时才置 1，失败可重试。
- `svm_device_open_locked` 中，`dev_status[devid] = 1` 只在 `svm_ioctl_dev_init` 成功后才设置——失败不会留下「半打开」状态。

**预期结果**：能画出 4.3.2 中的调用层次图，并标注「双检锁」「幂等打开」「先 master 后 device」三个设计点。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `svm_master_init` 要在加锁前先无锁读一次 `g_master_init_flag`？
**答案**：为了优化快路径。初始化完成后，绝大多数调用都命中「已初始化」分支，无锁直接返回，避免每次都竞争 `master_mutex`；加锁后的第二次检查是为了保证线程安全（防止两个线程同时通过第一次检查）。

**练习 2**：`halMemAgentOpen` 把 `DRV_ERROR_REPEATED_USERD` 翻译成 `DRV_ERROR_NONE`，这样做有什么好处？
**答案**：让上层调用者可以放心地「重复打开」同一设备而不必自己处理重复错误，简化了使用模型（幂等接口），也方便多个模块（如 Runtime、TRS）各自调用 `halMemAgentOpen` 而互不干扰。

---

### 4.4 svm_ioctl_dev_init：Host↔Device 字符设备通道

#### 4.4.1 概念说明

`svm_master_init.c` 只是「编排」，真正「让内核为本进程把 SVM 准备好」的工作在 `svm_ioctl_dev_init`（`src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c`）。它是 Host 侧用户态与 Device 侧内核态之间的**唯一通道**——通过打开字符设备 `/dev/davinci_manager` 并发 `ioctl` 实现。

这个文件还承载了 v3 架构里一个非常优雅的设计：**子模块自注册的 post-init 回调表**。每个 SVM 子模块（地址分配器、共享、URMA 适配等）在自己的构造函数里，把「我自己的每设备初始化函数」注册进一张表；`svm_ioctl_dev_init` 打开设备后，会依次调用表里所有函数。这样**新增一个子模块不需要修改 `svm_ioctl_dev_init` 本身**，只需自注册即可——典型的开闭原则。

#### 4.4.2 核心流程

```
svm_ioctl_dev_init(devid)                       [198 行]
  ├─ uda_get_udevid_by_devid_ex(devid, &udevid)  逻辑号→全局唯一号（u3-l3 的 UDA）
  ├─ 加锁 svm_fd_mutex
  ├─ svm_get_dev_fd(devid)：已有 fd？ → 返回 DRV_ERROR_REPEATED_INIT
  ├─ svm_char_dev_open(udevid, &fd)              [61 行]
  │     ├─ svm_file_open("/dev/davinci_manager", O_RDWR|O_CLOEXEC)
  │     └─ svm_user_ioctl(fd, DAVINCI_INTF_IOCTL_OPEN, &arg)  陷入内核（EBUSY 重试）
  ├─ svm_set_dev_fd(devid, fd)                   缓存 fd（带 pid 校验，fork 安全）
  ├─ svm_call_ioctl_dev_init_post_handle(devid)  [163 行]
  │     for i in 0..MAX: 调用所有已注册的子模块 post_init 回调
  └─ 解锁
```

子模块如何注册？例如地址分配器 `assign/va_allocator/va_allocator.c`、缓存分配器 `assign/cache_malloc/cache_init.c`、URMA 适配 `urma_adapt/urma_adapt_init/svm_urma_adapt_master_init.c` 等，都在构造函数里调用：

```c
svm_register_ioctl_dev_init_post_handle(my_dev_init);
```

于是当某张卡被 `svm_ioctl_dev_init` 打开时，它的 VA 分配器、cache 分配器、URMA 段管理、共享子模块……都会各自完成「针对这张卡」的初始化。这就是 README 所说的「Host 侧与 Device 侧 SVM 模块之间的交互」——交互不止是那一次 `DAVINCI_INTF_IOCTL_OPEN`，还包括各子模块在打开后经各自 ioctl 与内核做的进一步协商。

#### 4.4.3 源码精读

打开字符设备并 `ioctl` 陷入内核：

```c
static int svm_char_dev_open(u32 udevid, int *fd)
{
    struct davinci_intf_open_arg arg = {0};
    arg.device_id = (int)udevid;
    strcpy_s(arg.module_name, ..., SVM_CHAR_DEV_NAME);

    *fd = svm_file_open(davinci_intf_get_dev_path(), O_RDWR | O_CLOEXEC);  /* /dev/davinci_manager */
    if (*fd < 0) { return errno_to_user_errno(errno); }

    do {   /* EBUSY 重试：同名 PID 旧资源未回收 */
        ret = svm_user_ioctl(*fd, DAVINCI_INTF_IOCTL_OPEN, &arg);
        retry = ((ret != 0) && (errno == EBUSY) && (cnt < 1000));
        if (retry) { usleep(100000); }   /* 100ms */
    } while (retry);
    ...
}
```

[src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c:61-100](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c#L61-L100) —— 打开 `/dev/davinci_manager`，发 `DAVINCI_INTF_IOCTL_OPEN` 告诉内核「SVM 模块要用这张卡」，`EBUSY` 时重试（处理旧进程资源未释放）。

post-init 回调表与注册函数：

```c
#define SVM_IOCTL_DEV_HANDLE_MAX_NUM 20
static int (*svm_ioctl_dev_init_post_handle[SVM_IOCTL_DEV_HANDLE_MAX_NUM])(u32 devid) = { NULL, };

int svm_register_ioctl_dev_init_post_handle(int (*fn)(u32 devid))
{
    for (int i = 0; i < SVM_IOCTL_DEV_HANDLE_MAX_NUM; i++) {
        if (svm_ioctl_dev_init_post_handle[i] == NULL) {
            svm_ioctl_dev_init_post_handle[i] = fn;   /* 找到空槽就注册 */
            return DRV_ERROR_NONE;
        }
    }
    ...
}

static int svm_call_ioctl_dev_init_post_handle(u32 devid)
{
    for (int i = 0; ...; i++) {
        if (svm_ioctl_dev_init_post_handle[i] != NULL) {
            ret = svm_ioctl_dev_init_post_handle[i](devid);   /* 逐个调用 */
        }
    }
}
```

[src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c:126-175](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c#L126-L175) —— 一张最多 20 槽的函数指针表，子模块自注册，初始化时顺序回调。

把上面几步串起来的 `svm_ioctl_dev_init`：

```c
int svm_ioctl_dev_init(u32 devid)
{
    uda_get_udevid_by_devid_ex(devid, &udevid);          /* 逻辑号→全局号 */
    pthread_mutex_lock(&svm_fd_mutex);
    fd = svm_get_dev_fd(devid);
    if (fd >= 0) { return DRV_ERROR_REPEATED_INIT; }      /* 已打开 */

    svm_char_dev_open(udevid, &fd);                        /* 打开字符设备 */
    svm_set_dev_fd(devid, fd);                             /* 缓存 fd */

    ret = svm_call_ioctl_dev_init_post_handle(devid);      /* 各子模块 per-device init */
    if (ret != DRV_ERROR_NONE) {                           /* 失败回滚 */
        svm_set_dev_fd(devid, -1);
        svm_char_dev_close(udevid, fd);
    }
    pthread_mutex_unlock(&svm_fd_mutex);
    return 0;
}
```

[src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c:198-245](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c#L198-L245) —— 完整的「翻译设备号 → 打开字符设备 → 缓存 fd → 回调子模块 → 失败回滚」流程。

#### 4.4.4 代码实践

**实践目标**：理解「自注册回调表」如何让子模块与初始化主流程解耦。

**操作步骤**（源码阅读型实践）：
1. 在仓库中搜索 `svm_register_ioctl_dev_init_post_handle` 的所有调用点（用 `Grep`），数一数有多少个子模块注册了 init 回调。
2. 任选两个（建议 `assign/va_allocator/va_allocator.c` 与 `assign/cache_malloc/cache_init.c`），查看它们注册的回调函数体（在构造函数 `__attribute__((constructor))` 中调用注册），理解它们「针对某张卡」做了什么初始化。
3. 思考：如果新增一个 SVM 子模块，需要修改 `svm_ioctl_dev_init` 吗？

**需要观察的现象**：注册点分布在 `assign/`、`op/`、`share/`、`urma_adapt/`、`dbi/`、`apbi/`、`query/`、`api/master/` 等多个子目录，每个子模块独立注册，互不依赖。

**预期结果**：得出结论——新增子模块**不需要**改动 `svm_ioctl_dev_init`，只需在自己的构造函数里调一次 `svm_register_ioctl_dev_init_post_handle` 即可被纳入初始化流程。

> 待本地验证：上述注册点的确切数量需用 `Grep` 在本地确认（仓库中至少有二十余处）。

#### 4.4.5 小练习与答案

**练习 1**：`svm_char_dev_open` 在遇到 `errno == EBUSY` 时为什么要重试？
**答案**：当同名（同 PID）的旧进程资源尚未被内核回收时，`DAVINCI_INTF_IOCTL_OPEN` 会返回 `EBUSY`。重试（最多 1000 次，每次间隔 100ms）是为了等内核清理完旧资源后再加入哈希表，避免进程重启后初始化失败。

**练习 2**：`svm_set_dev_fd` 缓存 fd 时为什么要带 pid 校验（见同文件 `svm_get_dev_fd`）？
**答案**：为了 fork 安全。子进程会继承父进程的 fd，但该 fd 在内核里属于父进程上下文；子进程若直接用会出错。pid 校验确保只有真正打开该 fd 的进程才能复用它，其他进程需要重新打开。

---

### 4.5 devmm_svm_init：v2 的 mmap 化初始化范式

> 对应最小模块：`devmm_svm_init`。

#### 4.5.1 概念说明

`devmm_svm_init.c` 位于 `src/ascend_hal/svm/v2/common/`（**注意：是 v2，不是 v3**）。它是 ascend910B 上 SVM 的初始化核心。虽然 v3 重新设计了架构，但 v2 的这段代码把「SVM 初始化到底在干什么」讲得最直白——因为它把三步经典操作（**打开设备 → ioctl 建内核状态 → mmap 预留地址段**）完整地铺在一个文件里。

理解它的价值在于：v3 的 `svm_ioctl_dev_init` + `svm_mmap.c` 做的其实是同样的事，只是拆得更细。看懂 v2 这段，v3 的本质就清楚了。

#### 4.5.2 核心流程

v2 的进程级（master）初始化入口是 `devmm_svm_master_init`，它三步走：

```
devmm_svm_master_init()                       [396 行]
  └─ devmm_svm_init("svm", SVM_MASTER_SIDE)   [369 行]
       ├─ devmm_svm_open()                     [138 行] 打开字符设备 + DAVINCI_INTF_IOCTL_OPEN
       │     devmm_svm_open_proc()             [117 行]
       │       ├─ open("/dev/davinci_manager", O_RDWR|O_SYNC|O_CLOEXEC)
       │       └─ ioctl(fd, DAVINCI_INTF_IOCTL_OPEN, &arg)
       ├─ devmm_svm_alloc_proc_struct()        [350 行]
       │     ioctl(fd, DEVMM_SVM_ALLOC_PROC_STRUCT)   ← 内核为本进程分配 SVM 管理结构
       └─ devmm_svm_map(side)                  [296 行]
             ├─ devmm_svm_get_mmap_para()      [217 行]
             │     ioctl(fd, DEVMM_SVM_GET_MMAP_INFO)  ← 问内核「要 mmap 哪些段」
             └─ for each seg: devmm_svm_map_by_size()  [198 行]
                   mmap(seg.va, seg.size, ..., g_devmm_mem_dev, 0)   ← 映射进预留虚拟地址段
```

简言之：先用 `ioctl` 让内核把本进程的 SVM 状态建好（分配进程级管理结构、告知需要映射哪些虚拟地址段），再用 `mmap` 把这些段映射进进程的虚拟地址空间（起始于 `DEVMM_SVM_MEM_START`）。映射成功后，进程就能在这段地址里分配设备内存了。

#### 4.5.3 源码精读

打开字符设备（v2 版本，逻辑与 v3 的 `svm_char_dev_open` 同源）：

```c
STATIC int devmm_svm_open_proc(const char *davinci_sub_name)
{
    int fd = open(davinci_intf_get_dev_path(), O_RDWR | O_SYNC | O_CLOEXEC);  /* /dev/davinci_manager */
    if (fd < 0) { return fd; }
    DVresult ret = devmm_davinci_open(fd, davinci_sub_name);   /* 内部发 DAVINCI_INTF_IOCTL_OPEN */
    if (ret != DRV_ERROR_NONE) { close(fd); return -1; }
    return fd;
}
```

[src/ascend_hal/svm/v2/common/devmm_svm_init.c:117-146](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/common/devmm_svm_init.c#L117-L146) —— 打开字符设备并通过 `ioctl` 告知内核 SVM 子模块要使用它。

让内核为本进程分配管理结构、并取得 mmap 段信息：

```c
STATIC DVresult devmm_svm_alloc_proc_struct(void)
{
    struct devmm_ioctl_arg para_arg = {0};
    return devmm_svm_ioctl(g_devmm_mem_dev, DEVMM_SVM_ALLOC_PROC_STRUCT, &para_arg);
}

STATIC int devmm_svm_get_mmap_para(struct devmm_mmap_addr_seg *segs, uint32_t *seg_num)
{
    struct devmm_ioctl_arg mmap_arg = {0};
    mmap_arg.data.mmap_para.seg_num = *seg_num;
    mmap_arg.data.mmap_para.segs = segs;
    devmm_svm_ioctl(g_devmm_mem_dev, DEVMM_SVM_GET_MMAP_INFO, &mmap_arg);  /* 内核回填段信息 */
    *seg_num = mmap_arg.data.mmap_para.seg_num;
    ...
}
```

[src/ascend_hal/svm/v2/common/devmm_svm_init.c:217-361](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/common/devmm_svm_init.c#L217-L361) —— `DEVMM_SVM_ALLOC_PROC_STRUCT` 让内核分配进程级 SVM 结构；`DEVMM_SVM_GET_MMAP_INFO` 向内核索取「需要 mmap 哪些虚拟地址段」。

把预留地址段 `mmap` 进进程空间：

```c
STATIC DVresult devmm_svm_init(const char *davinci_sub_name, int side)
{
    DVresult ret = devmm_svm_open(davinci_sub_name);      /* ① 打开设备 */
    if (ret != DRV_ERROR_NONE) { return ret; }
    ret = devmm_svm_alloc_proc_struct();                  /* ② 内核分配进程结构 */
    if (ret != DRV_ERROR_NONE) { devmm_svm_close(...); return ret; }
    ret = devmm_svm_map(side);                            /* ③ mmap 预留地址段 */
    if (ret != DRV_ERROR_NONE) { devmm_svm_close(...); return ret; }
    return DRV_ERROR_NONE;
}
```

[src/ascend_hal/svm/v2/common/devmm_svm_init.c:369-399](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/common/devmm_svm_init.c#L369-L399) —— 三步顺序：打开设备 → 分配进程结构 → mmap 映射；任一步失败都回滚已打开的资源。`devmm_svm_master_init` 即以 `SVM_MASTER_SIDE` 调用它。

> 对比 v3：v3 把这里的「打开设备 + ioctl」放进 `sys_cmd/svm_ioctl.c`，把「mmap 预留地址段」放进 `svm_mmap.c`，再用 `svm_master_init.c` 做编排、用 post-init 回调表做子模块扩展。**三步本质未变，只是拆得更清晰、扩展性更强。**

#### 4.5.4 代码实践

**实践目标**：用一个表格把 v2 与 v3 的初始化步骤一一对应，验证「本质相同、组织不同」。

**操作步骤**（源码阅读型实践）：
1. 阅读 [devmm_svm_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/common/devmm_svm_init.c) 中 `devmm_svm_init`（369 行）的三步。
2. 回到 v3：在 [svm_ioctl.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_ioctl.c) 找到「打开设备 + ioctl」的对应代码（`svm_char_dev_open`）。
3. 填一张对照表：

| 步骤 | v2（devmm_svm_init.c） | v3（对应文件/函数） |
|------|------------------------|----------------------|
| 打开字符设备 | `devmm_svm_open_proc` | `svm_char_dev_open`（svm_ioctl.c:61） |
| 内核分配进程结构 | `DEVMM_SVM_ALLOC_PROC_STRUCT` ioctl | （由 `DAVINCI_INTF_IOCTL_OPEN` + post_init 回调完成） |
| mmap 预留地址段 | `devmm_svm_map` | `svm_mmap.c`（待确认具体函数） |
| 子模块 per-device init | （内联在 devmm 各 heap 初始化） | `svm_call_ioctl_dev_init_post_handle`（svm_ioctl.c:163） |

**需要观察的现象**：v2 倾向于把多件事写在一个流程里；v3 把它们拆成独立模块，靠注册表协作。

**预期结果**：能用一句话总结——「v2 与 v3 的初始化都在做『打开设备 → ioctl 建内核状态 → mmap 地址段』三件事，v3 的进步在于分层与可扩展」。

#### 4.5.5 小练习与答案

**练习 1**：`devmm_svm_map` 为什么要先 `ioctl(DEVMM_SVM_GET_MMAP_INFO)` 问内核，而不是直接 `mmap` 一个固定的地址？
**答案**：因为不同芯片、不同形态（PCIe/UB）下，需要映射的虚拟地址段的数量、起始地址、大小都可能不同。向内核查询可以适配这些差异，保证用户态映射的地址与内核管理的预留段一致。

**练习 2**：`devmm_svm_init` 中三步任何一步失败，都会调用 `devmm_svm_close` 回滚。这种「失败即回滚」的写法有什么好处？
**答案**：避免留下「半初始化」状态——例如设备已打开但进程结构未分配成功时，若不回滚，fd 会泄漏、内核状态会不一致。逆序回滚保证要么全部成功，要么像什么都没发生过一样干净。

---

## 5. 综合实践

**任务**：绘制一张「`aclrtSetDevice` 触发 SVM 完整初始化」的时序图，并标注 v2/v3 的差异点。

**要求**：

1. **纵向时序**（自上而下）：
   - 上层 `aclrtSetDevice` → HAL 公共入口 `halMemAgentOpen(devid, flag)`。
   - 进程级：`svm_dev_open` → `svm_master_init`（双检锁）→ `svm_master_init_locked`（创建共享日志 + 打开 Host 设备）。
   - 每设备：`svm_device_open` → `svm_device_open_locked` → `svm_ioctl_dev_init`。
   - 设备通道：`uda_get_udevid_by_devid_ex`（翻译设备号）→ `svm_char_dev_open`（打开 `/dev/davinci_manager` + `DAVINCI_INTF_IOCTL_OPEN`）→ `svm_call_ioctl_dev_init_post_handle`（回调各子模块）。

2. **横向角色**（从左到右三列）：
   - **用户态 SVM（libascend_hal.so）**：`halMemAgentOpen` / `svm_master_init` / `svm_ioctl_dev_init`。
   - **内核态（drv_davinci.ko）**：接收 `DAVINCI_INTF_IOCTL_OPEN`、为本进程分配 SVM 结构。
   - **设备（NPU）**：被打开、资源被初始化的目标。

3. **标注 v2/v3 差异**：在图边用注释写明——v2（ascend910B）走 `devmm_svm_init.c` 的「open→alloc_proc_struct→map」三步；v3（ascend950）走 `svm_master_init.c` + `svm_ioctl.c` 的「编排 + post-init 注册表」，并把 mmap 拆到 `svm_mmap.c`。

4. **思考题**（写在图下方）：如果让你为 v3 新增一个 SVM 子模块（例如一种新的地址分配策略），你会改动哪些文件？哪些文件**不应该**改动？

**参考答案要点**：新增子模块应在其构造函数里调用 `svm_register_ioctl_dev_init_post_handle(my_init)` 和 `svm_register_ioctl_dev_uninit_pre_handle(my_uninit)` 即可；**不应该**改动 `svm_ioctl_dev_init`、`svm_master_init` 这些主流程文件——这正是 v3 注册表设计的意义。

## 6. 本讲小结

- **SVM 是设备侧内存管理模块**，向上为 Runtime 提供 `halMemAlloc`/`halMemFree`/`halMemcpy`/`halShmem*` 等接口，核心套路是「先预留虚拟地址段、再 mmap 映射、再进内核申请物理页」。
- **v2 与 v3 是按芯片代际二选一**：`build.sh --soc=ascend910b` 编译 v2，`--soc=ascend950` 编译 v3（由 `svm/CMakeLists.txt` 决定）。同一个公共接口 `halMemAgentOpen`，背后是两套实现。
- **v3 把 v2 的「大文件」拆成了分层架构**：`api/master/`（对外接口与编排）、`sys_cmd/`（内核交互）、`assign/`（地址分配器）、`op/`（操作）、`share/`（共享）等。
- **初始化分两层**：`svm_master_init` 做进程级一次性初始化（双检锁、打开 Host 设备）；`svm_device_open` 做每设备初始化（幂等打开，`dev_status[]` 守护）。
- **Host↔Device 通道是字符设备 `/dev/davinci_manager` + ioctl**：`svm_ioctl_dev_init` 翻译设备号（借 UDA）、打开字符设备、`ioctl` 陷入内核，再回调各子模块的 post-init。
- **v3 的精华是自注册回调表**：子模块在构造函数里注册 init/uninit 回调，新增模块无需改动主流程——开闭原则的工程实践。

## 7. 下一步学习建议

本讲只讲了「初始化」。SVM 真正的戏肉在初始化之后的内存操作，建议按以下顺序继续：

- **[u4-l2 内存申请/释放/赋值主链路](u4-l2-svm-alloc-free.md)**：跟踪 `halMemAlloc` 如何在初始化预留的虚拟地址段里分配、mmap、进内核申请物理页，以及 `halMemFree` 的逆序释放。
- **[u4-l3 VMM 虚拟/物理地址分离管理](u4-l3-svm-vmm.md)**：理解 `halMemAddressReserve`/`halMemCreate`/`halMemMap` 如何把虚拟与物理解耦。
- **[u4-l4 内存拷贝与共享机制](u4-l4-svm-copy-and-share.md)**：`halMemcpy` 的阻塞语义与 `halShmem*` 的跨设备共享。
- **[u4-l5 SVM v3 地址空间与分配器架构](u4-l5-svm-allocator-architecture.md)**：深入本讲提到的 `assign/` 下的多级分配器（va_allocator/cache_malloc/gen_allocator），理解 4.4 里那些 post-init 回调到底在分配什么。

阅读源码时，建议先把本讲的 `svm_master_init.c` 与 `svm_ioctl.c` 放在手边，因为后续每个内存接口都会依赖本讲建立的「fd 已打开、子模块已注册」这个前提。
