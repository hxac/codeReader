# 内存申请/释放/赋值主链路

## 1. 本讲目标

本讲是 SVM（Shared Virtual Memory）模块的第二次课，承接 [u4-l1 SVM 模块总览与初始化](./u4-l1-svm-overview-and-init.md) 已经讲清的「初始化怎么打通 Host↔Device 通道」，继续回答下一个最自然的问题：**初始化完成之后，上层 Runtime 调一次 `halMemAlloc` 到底发生了什么？返回给我的那个指针是怎么来的？用完之后 `halMemFree` 又是怎么把它回收干净的？**

读完本讲，你应该能够：

- 说清 `halMemAlloc` 从「解析 flag → 分配虚拟地址 → mmap 映射 → 进入内核申请物理页并建页表 → 登记到红黑树」的完整调用链；
- 理解 SVM 内存「**先预留虚拟地址、再 mmap、最后 populate 物理页**」的三段式本质，以及它为什么这样设计；
- 解释 `halMemFree` 为什么必须**严格按申请的逆序**解除映射（先解页表、释放物理页，再归还虚拟地址）；
- 掌握 `drvMemsetD8` 内存赋值的能力校验与「快路径清零」优化；
- 认识 `halMemCtl` 这张「内存控制分发表」是如何用函数指针表把特性查询、内存修复等控制类操作统一收口的。

## 2. 前置知识

在进入源码前，先用三段话把必要的背景补齐。

**第一，虚拟地址与物理地址是两回事。** 应用拿到的指针（例如 `0x7f...`）是**虚拟地址（VA）**，它本身不对应任何真实存储。真正存数据的是**物理页（PA）**。要让「读写 VA」能落到「真实物理页」上，必须在两者之间建立**页表映射**——这是 CPU/MMU 的工作。SVM 做的事，本质就是在 Host 进程的虚拟地址空间里圈一段地址，再把这些地址映射到 NPU 设备侧的物理页上，让 Host 和 Device 共享同一套虚拟地址视图（这正是 "Shared Virtual Memory" 的含义）。

**第二，用户态不能直接碰物理页。** 物理页的分配、页表的建立/销毁都是内核特权操作。SVM 运行在用户态（`libascend_hal.so`），所以它必须通过两条「陷入内核」的通道与内核态驱动（SDK-driver 那一层，见 [u6-l1](./u6-l1-sdk-driver-and-kernel-adapt.md)）协作：

- `mmap` 系统调用：把一段 VA 映射到某个字符设备（`/dev/davinci_manager`）的文件偏移上，内核据此建立 VMA；
- `ioctl` 系统调用：下发 `SVM_MPL_POPULATE` 等命令，让内核去申请物理页、写页表项。

**第三，flag 是一个「位域编码」。** 上层调用 `halMemAlloc(pp, size, flag)` 时，目标设备号、内存类型（device/host/dvpp）、页大小（normal/huge/giant）、是否连续、是否 P2P、归属模块 id……全都压缩进了一个 64 位 `flag` 里。理解本讲的关键之一，就是看 SVM 如何把这个 `flag` 拆开、翻译成内部语义。

## 3. 本讲源码地图

本讲围绕三个最小模块（对外门面 `svm_alloc`、内存赋值 `svm_set`、内存控制 `svm_mem_ctl`）展开，但「申请/释放」的真正实现在更下层的分配器里，所以我们会一路追到 `assign/` 下的分配器。涉及的文件如下：

| 文件 | 所属最小模块 | 作用 |
|------|------------|------|
| `src/ascend_hal/svm/v3/api/master/svm_alloc.c` | svm_alloc | **对外门面**：`halMemAlloc`/`halMemFree` 入口、flag 解析、能力位装配 |
| `src/ascend_hal/svm/v3/api/master/svm_set.c` | svm_set | 内存赋值 `drvMemsetD8`：参数校验、能力校验、快路径清零 |
| `src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c` | svm_mem_ctl | 内存控制 `halMemCtl`：特性查询、内存修复等控制类操作的分发表 |
| `src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c` | （实现下沉） | `svm_malloc`/`svm_free`/`svm_get_prop`：handle 红黑树管理 + cache/normal 路径选择 |
| `src/ascend_hal/svm/v3/assign/normal_malloc/normal_malloc.c` | （实现下沉） | `svm_normal_malloc`：**两阶段设计**的核心——先分配 VA，再 populate 物理页 |
| `src/ascend_hal/svm/v3/assign/va_allocator/va_allocator.c` | （实现下沉） | `svm_alloc_va`：按分配器类型分发，从预留范围切出一段 VA |
| `src/ascend_hal/svm/v3/assign/va_allocator/va_reserve.c` | （实现下沉） | `va_reserve_master`：真正执行 `mmap` 把 VA 映射到字符设备 |
| `src/ascend_hal/svm/v3/assign/mpl/mpl_user.c` | （实现下沉） | `svm_mpl_populate`：ioctl 陷入内核申请物理页、建页表 |
| `src/ascend_hal/svm/v3/sys_cmd/svm_mmap.c` | （实现下沉） | `svm_cmd_mmap`：对字符设备 fd 执行 mmap 的封装 |
| `pkg_inc/ascend_hal_define.h` | （背景） | `flag` 位域编码定义 |
| `src/ascend_hal/svm/README.md` | （背景） | SVM 模块官方说明，本讲主链路的文字描述出处 |

> 提示：本讲引用的源码路径大多在 `svm/v3/` 下。v3 对应 `build.sh --soc=ascend950`（A5）的编译产物；ascend910B 走 v2（见 u4-l1）。两套实现的对外接口与主链路思想一致，本讲以 v3 为准。

## 4. 核心概念与源码讲解

本讲按调用顺序自上而下拆成四个最小模块：

- **4.1 svm_alloc**——对外门面：解析 flag、装配能力位、转调下层；
- **4.2 申请/释放的分配器实现**（malloc_mng + normal_malloc）——「分配 VA → populate 物理页」的两阶段设计，本讲的核心；
- **4.3 svm_set**——内存赋值 `drvMemsetD8`；
- **4.4 svm_mem_ctl**——内存控制 `halMemCtl` 的分发表。

---

### 4.1 svm_alloc：申请/释放的对外门面与 flag 解析

#### 4.1.1 概念说明

`svm_alloc.c` 是 SVM 内存申请/释放的**最外层门面**。它做三件事：

1. **暴露稳定的 HAL 符号** `halMemAlloc`/`halMemFree`（这两者声明在 `ascend_hal_base.h`，见 [u3-l1](./u3-l1-hal-overview-and-api.md)），符号名在 v2/v3 间保持不变；
2. **解析 flag**：把上层用 `MEM_DEV`/`MEM_HOST`/`MEM_PAGE_HUGE` 等公共宏拼出的 `flag`，拆成「目标 devid」「归属 module_id」和「内部 `SVM_FLAG_*` 语义」三部分；
3. **装配能力位**：根据内存类型，给这段内存标上它将来支持哪些操作（能否被注册、能否拷贝、能否 memset、能否 IPC 共享……），这些能力位会被后续 `svm_set`、拷贝、共享等模块用来做权限校验。

它**不直接碰物理页**，真正干活的是 4.2 的分配器。

#### 4.1.2 核心流程

`halMemAlloc` 的门面流程（自上而下）：

```
halMemAlloc(pp, size, flag)                 ← 公共入口
└─ halMemAllocInner                         ← 加 master_init + pipeline 守护
   ├─ svm_master_init()                     ← 进程级一次性初始化（u4-l1 已讲）
   └─ svm_mem_malloc(pp, size, flag)        ← 解析 flag + 装配能力位
      ├─ svm_parse_alloc_devid   → devid    ← bit0~9 取设备号，按内存类型校正
      ├─ svm_parse_alloc_module_id → module_id
      ├─ svm_parse_alloc_svm_flag → svm_flag ← MEM_* 翻译为 SVM_FLAG_*，叠加能力位
      └─ svm_module_mem_malloc(...)          ← 进入 4.2 的分配器
```

flag 的位域布局（来自头文件注释）：

```
bit0~9   : devid          ← 设备号
bit10~13 : virt mem type  ← svm/dev/host/dvpp/host_uva...
bit14~16 : phy mem type   ← DDR/HBM
bit17~18 : phy page size  ← normal/huge
bit19    : phy continuity ← 是否连续物理页
bit25~40 : mem advise     ← P2P/4G/TS...
bit56~63 : model id       ← 归属模块（Runtime 模型 id）
```

#### 4.1.3 源码精读

**公共入口极薄，只做转发**——`halMemAlloc` 原样调用 `halMemAllocInner`，`halMemFree` 同理。这样设计是为了让「带 telemetry 的 Inner 版本」与「对外稳定符号」解耦：

[svm_alloc.c:327-330](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L327-L330) —— `halMemAlloc` 仅转发到 `halMemAllocInner`。

`halMemAllocInner` 做参数校验、`svm_master_init()` 保底初始化、并用 `svm_use_pipeline()`/`svm_unuse_pipeline()` 把这次申请包进 pipeline 上下文里，最后处理 OOM 上报：

[svm_alloc.c:292-325](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L292-L325) —— 申请主流程：校验 → master_init → pipeline 守护 → `svm_mem_malloc` → OOM/NOT_SUPPORT 上报。

**flag 解析的三连击**是本模块的精髓。先看「取设备号」：从 `bit10~13` 读出 `virt_mem_type`，再据此校正 `devid`——device/dvpp 内存直接用 flag 里带的设备号（并校验上限），host/host_uva 内存则统一改成「Host 设备号」：

[svm_alloc.c:101-121](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L101-L121) —— `svm_parse_alloc_devid`：按 virt_mem_type 决定 `devid` 的来源。

flag 位域定义见头文件，注释里把每一位的用途写得很清楚：

[ascend_hal_define.h:703-721](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h#L703-L721) —— flag 位域布局注释与 `MEM_DEVID_MASK`/`MEM_VIRT_BIT` 定义。

再看「翻译 + 能力装配」。`svm_parse_alloc_svm_flag` 一方面把不支持的组合挡掉（如「host 内存 + 大页」「连续物理页 + 大页」直接返回 `DRV_ERROR_NOT_SUPPORT`），另一方面把合法的 `MEM_*` 位翻译成内部 `SVM_FLAG_ATTR_*`：

[svm_alloc.c:132-209](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L132-L209) —— `svm_parse_alloc_svm_flag`：合法性拦截 + `MEM_*`→`SVM_FLAG_ATTR_*` 翻译。

紧接着 `svm_mem_malloc` 给这段内存**叠加一堆能力位**（`SVM_FLAG_CAP_*`）——是否允许 register、是否允许 sync/async copy、是否允许 memset、是否允许 IPC 共享等。注意一个关键分支：**Host 内存不支持 prefetch / IPC**，只有 device 内存才追加 `SVM_FLAG_CAP_IPC_*` 和 `SVM_FLAG_CAP_PREFETCH`；而 `MEM_DEV_CP_ONLY`（仅拷贝专用设备内存）走旁路、几乎不给能力位：

[svm_alloc.c:232-258](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L232-L258) —— 能力位装配：device 内存 vs host 内存 vs dev_cp_only 三种分支。

> 这些能力位不是摆设。后面 4.3 的 `drvMemsetD8` 会读 `SVM_FLAG_CAP_MEMSET` 来判断「这块内存到底能不能被 memset」。能力位在申请时一次性写好，后续所有操作都靠它做权限校验，避免每次都重新推断。

最后 `svm_mem_malloc` 调 `svm_module_mem_malloc` 进入分配器（4.2），成功后把返回的 `start`（一个 u64 虚拟地址）转成 `void *` 写回出参 `*va`：

[svm_alloc.c:260-266](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L260-L266) —— 拿到 `start` 后转为指针写回 `*va`。

**释放侧** `svm_mem_free` 则先用 `svm_get_prop` 反查这段地址的属性（devid/flag/size），再做一次「是否允许普通 free」的能力校验，最后转调 `svm_module_mem_free`：

[svm_alloc.c:269-289](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L269-L289) —— `svm_mem_free`：反查属性 → 能力校验 → 转调分配器释放。

#### 4.1.4 代码实践

**实践目标**：亲手把 `flag` 拆开，验证 `svm_parse_alloc_devid` 的逻辑。

**操作步骤**（源码阅读型实践）：

1. 打开 [svm_alloc.c:101-121](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L101-L121)，对照 [ascend_hal_define.h:723-737](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h#L723-L737) 的取值表。
2. 假设上层这样申请一张 0 号设备上的 device 内存：`flag = MEM_DEV | 0`（即 `0x1<<10 | 0`）。
3. 手算 `(flag >> MEM_VIRT_BIT) & ((1<<4)-1)` = `(0x1<<10 >> 10) & 0xF` = `1` = `MEM_DEV_VAL`。
4. 走 `MEM_DEV_VAL` 分支：`*devid = flag & MEM_DEVID_MASK = 0`，校验 `0 < SVM_MAX_DEV_AGENT_NUM` 通过，返回 0。

**需要观察的现象 / 预期结果**：

- 若把申请改成 Host 内存 `flag = MEM_HOST`（`0x2<<10`），手算 virt_mem_type = 2 = `MEM_HOST_VAL`，应走 `*devid = svm_get_host_devid()` 分支——即无论 flag 低 10 位填什么，Host 内存的 devid 都被强制改成 Host 设备号。
- 若填一个不支持的类型（如 `MEM_SVM_VAL=0`），函数直接返回 `DRV_ERROR_NOT_SUPPORT`。

> 待本地验证：`SVM_MAX_DEV_AGENT_NUM` 的具体取值（在 svm 头文件中），它决定了 device 内存可寻址的最大设备号。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `halMemAlloc` 要单独搞一个 `halMemAllocInner`，而不是把逻辑直接写进 `halMemAlloc`？

**参考答案**：把「对外稳定符号」与「内部实现」解耦。`halMemAlloc` 是写进 `ascend_hal_base.h`、对上层 Runtime 承诺的 ABI，不能随意改动；而 `Inner` 版本可以自由加入调试日志、telemetry、pipeline 守护等逻辑，甚至未来可以替换实现而不破坏 ABI。

**练习 2**：上层申请时填了一个非法的「连续物理页 + 大页」组合，会在哪一步被挡掉、返回什么错误码？

**参考答案**：在 `svm_parse_alloc_svm_flag` 里被挡掉（见源码 155-158 行的判断），返回 `DRV_ERROR_NOT_SUPPORT`，根本不会进入分配器，更不会进内核。

---

### 4.2 申请/释放的分配器实现：两阶段设计（VA 先行，物理页后填）

这是本讲的核心模块。它回答主题里那句「分配虚拟地址 → mmap → 内核申请物理页 → 建页表」。

#### 4.2.1 概念说明

从 `svm_alloc.c` 下沉到 `malloc_mng.c` / `normal_malloc.c` 后，你会看到 SVM 把一次「申请」拆成了**两个物理上独立的阶段**：

- **阶段一：分配虚拟地址（VA alloc）**——从预留的虚拟地址范围里切出一段 `[start, start+size)`，并通过 `mmap` 把它映射到字符设备 `/dev/davinci_manager`。此时**还没有任何物理页**，VA 只是一个「空壳」。
- **阶段二：填充物理页（populate）**——通过 `ioctl(SVM_MPL_POPULATE)` 陷入内核，由内核态驱动去**申请真实物理页、并把 VA→PA 的页表项写好**。只有这一步完成，读写 VA 才会真正落到物理存储上。

为什么要拆成两阶段，而不是像 `malloc` 一样一把搞定？

1. **延迟物化**：UVM/SOMA 等特性（见 SVM README）需要「只预留 VA、物理页按需分配」。两阶段设计让「申请 VA」和「申请物理页」可以独立调用——只调阶段一就是「VA_ONLY」内存（VMM 接口的基础，见 [u4-l3](./u4-l3-svm-vmm.md)）。
2. **失败可回滚**：如果 populate 失败，只需把阶段一申请的 VA 还回去即可，状态干净。
3. **跨设备**：阶段二对 Host 内存走本地 ioctl，对 Device 内存则走 UMC 消息发往设备端处理（见 4.2.3），两条路径共用同一套阶段一。

`malloc_mng.c` 在两阶段之外，还用一棵**红黑树**管理所有已分配内存段（每个段是一个 `handle_t`，按 `[start, start+size)` 区间索引），并提供 cache 快路径。

#### 4.2.2 核心流程

完整的 `halMemAlloc` 调用链（融合 4.1 与 4.2）：

```
svm_module_mem_malloc                       [svm_alloc.c:56]
├─ svm_query_page_size_by_svm_flag → align  ← 查页大小，定对齐基准
└─ svm_malloc(start, size, align, flag, loc)[malloc_mng.c:607]
   ├─ malloc_para_check                     ← size/对齐合法性
   ├─ _svm_malloc                           [malloc_mng.c:492]  ← 选路径
   │   ├─ get_aligned_size                  ← size 按页向上对齐放大
   │   └─ go_malloc_cache ?
   │       ├─ 是: malloc_cache              ← 快路径，复用预分配段（u4-l5 详讲）
   │       └─ 否: malloc_normal → svm_normal_malloc  [normal_malloc.c:127]
   │            ├─ normal_va_alloc → svm_alloc_va    ① 阶段一：分配 VA
   │            │   └─ svm_reserve_va → va_reserve_master
   │            │       └─ svm_cmd_mmap(va, size, MAP_SHARED, fd)  ← mmap 字符设备
   │            └─ normal_mem_populate → svm_mpl_client_populate   ② 阶段二：填物理页
   │                ├─ host: svm_mpl_populate → ioctl(SVM_MPL_POPULATE)  ← 陷内核建页表
   │                └─ device: svm_mpl_populate_remote → UMC 消息发往设备端
   ├─ handle_alloc + svm_prop_pack + handle_init   ← 造 handle 记录属性
   ├─ svm_mng_ops_post_malloc                     ← 通知注册的 ops（IPC/prefetch 等）
   └─ handle_insert                                ← 插入红黑树，按区间索引
```

**释放则是严格逆序**：

```
svm_free(start)                            [malloc_mng.c:663]
├─ handle_erase(start, &handle)            ← 从红黑树摘下（带 ref 计数检查）
├─ handle_uninit(handle, false)            ← 释放私有数据；busy 则重新插回
├─ svm_mng_ops_pre_free                    ← 通知 ops 做释放前处理
└─ _svm_free → svm_normal_free             [normal_malloc.c:152]
    ├─ normal_mem_depopulate → svm_mpl_client_depopulate  ①' 先：解页表 + 释放物理页
    │   host: svm_mpl_depopulate → ioctl(SVM_MPL_DEPOPULATE)
    └─ normal_va_free → svm_free_va                        ②' 后：munmap + 归还 VA
        └─ va_release_master → svm_cmd_munmap
```

申请是「VA → 物理页」，释放是「物理页 → VA」——**精确的反序**。

#### 4.2.3 源码精读

**入口 `svm_module_mem_malloc`** 先查页大小（决定对齐基准 `align`），打包 devid/numa，再调真正的 `svm_malloc`。注意成功后会调 `svm_mms_add` 记账（内存统计），失败则打 `svm_mem_stats_show` 帮助定位 OOM：

[svm_alloc.c:56-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L56-L80) —— `svm_module_mem_malloc`：查页大小 → 打包 location → `svm_malloc` → 统计记账。

**`svm_malloc` 的「分配 + 建档 + 入树」三段式**。先用 `_svm_malloc` 拿到 VA（含物理页），再分配一个 `handle_t`、把属性打包进 `svm_prop`、初始化 handle，接着 `svm_mng_ops_post_malloc` 通知关注方，最后 `handle_insert` 插入红黑树。任何一步失败都有对应的 `goto` 回滚标签（`uninit_handle`/`free_handle`/`free_mem`），保证不留半成品：

[malloc_mng.c:607-661](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L607-L661) —— `svm_malloc`：分配 → 建 handle → 入树，失败逆序回滚。

**`_svm_malloc` 选 cache 还是 normal 路径**。先按页对齐放大 size，再判断 `go_malloc_cache`（是否支持且适合走 cache 快路径——连续页、大页、只读、旁路 cache 等场景会被排除），是则 `malloc_cache`，否则 `malloc_normal`：

[malloc_mng.c:492-518](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L492-L518) —— `_svm_malloc`：对齐放大 → cache/normal 二选一。

**两阶段设计的核心在 `svm_normal_malloc`**。它清晰地分成「va 分配」和「populate 物理页」两步，中间任何一步失败都会回滚已完成的步骤（populate 失败则 `normal_va_free` 把 VA 还回去）。两个开关 `POPULATE_ONLY`（只填物理页）和 `VA_ONLY`（只分 VA）让它能灵活组合：

[normal_malloc.c:127-150](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/normal_malloc/normal_malloc.c#L127-L150) —— `svm_normal_malloc`：① `normal_va_alloc` 分 VA → ② `normal_mem_populate` 填物理页。

**阶段一之 `svm_alloc_va`** 按分配器类型（device 默认、host 默认、dev_cp_only、指定地址、pcie_th……）分发到不同的 VA 分配实现。最常见的 `VA_ALLOCATOR_TYPE_DEFAULT` 会调用 `svm_reserve_va` 切出一段预留 VA（还会多预留一个 gap 用于防越界）：

[va_allocator.c:153-195](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_allocator.c#L153-L195) —— `svm_alloc_va`：按 allocator_type 分发。

**`va_reserve_master` 是真正执行 `mmap` 的地方**。它把刚切出的 VA 用 `svm_cmd_mmap` 映射到字符设备 fd 上（`MAP_SHARED`），并校验返回地址与期望一致，再用 `madvise(MADV_DONTDUMP)` 防止这段内存在进程崩溃时被 core dump：

[va_reserve.c:408-431](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_reserve.c#L408-L431) —— `va_reserve_master`：`svm_cmd_mmap` 把 VA 映射到字符设备。

**`svm_cmd_mmap` 的本质是对字符设备 fd 做 mmap**。fd 在 `svm_mmap_init` 里懒打开（双检锁 + 打开 `/dev/davinci_manager` 并 `DAVINCI_INTF_IOCTL_OPEN`），mmap 带 EINTR 重试：

[svm_mmap.c:120-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/sys_cmd/svm_mmap.c#L120-L138) —— `svm_cmd_mmap`：对 `svm_mmap_fd` 执行 `svm_user_mmap`。

**阶段二之 populate**。`svm_mpl_client_populate` 按 devid 分流：Host 内存走 `svm_mpl_populate`（本地 ioctl），Device 内存走 `svm_mpl_populate_remote`（UMC 消息发往设备端，由设备侧内核处理物理页分配与建表）：

[mpl_client.c:94-106](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/mpl_client/mpl_client.c#L94-L106) —— `_svm_mpl_client_populate`：host 走本地、device 走远端消息。

**Host 内存的 populate 就是发一个 ioctl**。`svm_mpl_populate` 把 `(va, size, flag)` 打包成 `svm_mpl_populate_para`，用 `svm_cmd_ioctl(devid, SVM_MPL_POPULATE, &para)` 陷入内核——**物理页的申请和页表项的写入就发生在这一次 ioctl 的内核处理里**：

[mpl_user.c:21-37](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/mpl/mpl_user.c#L21-L37) —— `svm_mpl_populate`：ioctl 陷内核申请物理页、建页表。

**释放侧 `svm_free` 的逆序回收**。先把 handle 从红黑树摘下（`handle_erase`，带 ref 计数检查——若仍被引用返回 `DRV_ERROR_CLIENT_BUSY`），再 `handle_uninit` 释放私有数据，最后 `_svm_free` 做物理回收。关键细节：若 `_svm_free` 返回 `DRV_ERROR_BUSY`（物理页仍被设备占用），handle 会被**重新插回红黑树**（`handle_insert`），把释放推迟到后续的 `SMP_DEL_MEM` 事件再处理：

[malloc_mng.c:663-696](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L663-L696) —— `svm_free`：摘 handle → uninit → `_svm_free`，busy 则重插。

`_svm_free` 同样按 cache/normal 分流：

[malloc_mng.c:520-527](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L520-L527) —— `_svm_free`：cache/normal 二选一。

**`svm_normal_free` 的逆序**就一目了然了——先 `depopulate`（解页表 + 释放物理页），再 `va_free`（munmap + 归还 VA）：

[normal_malloc.c:152-167](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/normal_malloc/normal_malloc.c#L152-L167) —— `svm_normal_free`：先 depopulate 后 va_free。

#### 4.2.4 代码实践

**实践目标**（即本讲指定的实践任务）：跟踪 `halMemAlloc` 的实现，画出「分配虚拟地址 → mmap → 内核申请物理页 → 建页表」的完整调用链，并解释 `halMemFree` 为何要按逆序解除映射。

**操作步骤**：

1. 从 [svm_alloc.c:327](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L327) 的 `halMemAlloc` 出发，逐层跳转：`halMemAllocInner` → `svm_mem_malloc` → `svm_module_mem_malloc` → `svm_malloc` → `_svm_malloc` → `svm_normal_malloc`。
2. 在 `svm_normal_malloc` 处明确分出两条支线：
   - 支线 A（VA）：`normal_va_alloc` → `svm_alloc_va` → `svm_reserve_va` → `va_reserve_master` → `svm_cmd_mmap` → `svm_user_mmap`（落到字符设备 fd）。
   - 支线 B（物理页）：`normal_mem_populate` → `svm_mpl_client_populate` → `svm_mpl_populate` → `svm_cmd_ioctl(SVM_MPL_POPULATE)`（陷入内核）。
3. 画出调用链草图（建议手绘或用文本树），标注每一步发生在「用户态」还是「内核态」（mmap/ioctl 是分界线）。
4. 再从 `halMemFree` 出发走一遍释放链，对比申请链，标出哪些步骤是反序的。

**需要观察的现象 / 预期结果**：

- 申请链中，`mmap` 发生在 `svm_cmd_mmap`（用户态系统调用），`SVM_MPL_POPULATE` ioctl 是「物理页 + 页表」真正发生的地方。
- 释放链中，顺序是 `depopulate`（解页表 + 释放物理页）在前，`va_free`（munmap）在后。

**关于「为何逆序」的论证**（请在你的笔记里写清楚）：

- VA 是物理页的「索引/锚点」，页表项是 **VA → PA** 的映射，它依赖 VA 有效存在。
- 若先 `va_free`（munmap）归还 VA，则页表项会变成**孤儿**——没有 VA 可以定位它们，物理页将无法被正确解除映射，最终**泄漏物理页**。
- 因此必须**先 depopulate**（在 VA 仍有效时，把页表项逐条拆除、物理页归还），**再 va_free**（此时页表已清空，VA 可以安全归还）。
- 这正好是申请顺序（先 va_alloc 后 populate）的严格反序——资源释放遵循「后申请者先释放」的栈式原则，才能保证每一步被释放的资源其依赖项仍然有效。

> 待本地验证：在内核态驱动里跟踪 `SVM_MPL_POPULATE` 的处理函数（位于 sdk_driver 层），确认它确实是「申请物理页 + 写页表项」。这属于 [u6](./u6-l1-sdk-driver-and-kernel-adapt.md) 单元的范畴。

#### 4.2.5 小练习与答案

**练习 1**：`_svm_malloc` 里 `go_malloc_cache` 在哪些情况下会返回 false（强制走 normal 路径）？

**参考答案**：当 `svm_flag_is_by_pass_cache` 为真、或内存是连续物理页（`svm_flag_attr_is_contiguous`）、或大页（gpage）、或只读页（`pg_rdonly`）时，`svm_flag_is_support_cache` 返回 false，从而不走 cache。此外 numa 绑定也会让它走 normal。这些场景下 cache 复用不安全或不划算。

**练习 2**：`svm_free` 返回 `DRV_ERROR_BUSY` 时，这段内存的 handle 去哪了？后续谁来真正释放它？

**参考答案**：handle 被**重新插回红黑树**（`handle_insert`），VA 与物理页都还在。真正的释放由 `svm_alloc_init`（constructor）注册的 `SVM_SMP_DEL_MEM_EVENT` 事件处理函数 `svm_smp_del_mem_event_proc_func` 在后续触发（见 [svm_alloc.c:358-413](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L358-L413)），它带 10 次重试去完成延迟释放。

---

### 4.3 svm_set：内存赋值 drvMemsetD8 与快路径清零

#### 4.3.1 概念说明

`svm_set.c` 实现内存赋值接口 `drvMemsetD8`：把一段设备/主机内存的每个字节设成给定值（典型用途是把一块 device 内存清零）。它的设计体现了 SVM 一以贯之的「**能力位校验**」思想——不是任意地址都能被 memset，必须先确认这段地址在申请时被赋予了 `SVM_FLAG_CAP_MEMSET` 能力。

它还内置了一个**快路径优化**：当满足「值是 0、目标是 device 内存、长度小于 512KB」三个条件时，不走真正的 memset 命令，而是用一块预申请的「全零 Host 内存」做一次 `drvMemcpy`——因为「拷贝一块已有的零」往往比「逐字节写零」在设备上更快。

#### 4.3.2 核心流程

```
drvMemsetD8(dst, destMax, value, num)      ← 公共入口
└─ drvMemsetD8Inner                        [svm_set.c:140]
   ├─ svm_memset_para_check                ← 地址非空、num 合法、能力位 SVM_FLAG_CAP_MEMSET
   │   └─ svm_memset_check_addr_prop_cap   ← 用 svm_get_prop 读 flag，校验能力
   ├─ svm_get_prop(dst) → devid            ← 反查目标设备号
   ├─ svm_can_go_fast_mem_clear ?          ← value==0 && dev!=host && num<512KB
   │   是: svm_mem_clear_fast              ← 用预申请的全零 Host 内存做 drvMemcpy
   │        └─ svm_get_host_zero_mem       ← 懒申请一块 512KB 全零 host 内存（双检锁）
   └─ svm_memset_client(devid, ...)        ← 普通路径：发命令到设备端做 memset
```

#### 4.3.3 源码精读

**公共入口同样极薄**：`drvMemsetD8` 仅转发到 `drvMemsetD8Inner`：

[svm_set.c:176-179](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L176-L179) —— `drvMemsetD8` 转发到 Inner 版。

**参数校验里嵌着能力校验**。`svm_memset_para_check` 先查地址非空、num 不超限，然后调 `svm_memset_check_addr_prop_cap`，后者用 `svm_get_prop` 读出这段地址的 `flag`，**检查 `SVM_FLAG_CAP_MEMSET` 位是否置位**——这正是 4.1 里申请时装配的能力位在此处被消费：

[svm_set.c:75-101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L75-L101) —— `svm_memset_para_check`：基础校验 + 能力位 `SVM_FLAG_CAP_MEMSET` 校验。

补充一点：如果 memset 范围跨越了多段不同属性的内存（比如跨了两段 device 内存但分属不同 devid），`svm_memset_check_addr_prop_consistency` 会逐段校验 flag/devid 一致性，拒绝「跨异构段」的 memset：

[svm_set.c:24-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L24-L44) —— `svm_memset_check_addr_prop_consistency`：跨段一致性校验。

**快路径判断与执行**。`svm_can_go_fast_mem_clear` 三个条件一目了然；`svm_mem_clear_fast` 拿到预申请的全零 Host 内存 `va`，直接 `drvMemcpy(dst, ..., va, num)`——把「写零」转成「拷零」：

[svm_set.c:129-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L129-L138) —— 快路径：条件判断 + 拷贝全零内存。

那块「全零 Host 内存」是懒申请的：`svm_get_host_zero_mem` 用双检锁 + 读写锁，首次调用时 `halMemAlloc` 一块 512KB Host 内存，并立刻把它整块清零，之后所有快路径清零都复用它：

[svm_set.c:104-127](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L104-L127) —— `svm_get_host_zero_mem`：懒申请 + 复用一块全零 Host 内存。

> 这是一个很有意思的「自引用」：`drvMemsetD8` 内部为了清零，调用了 `halMemAlloc`（申请那块零内存）和 `drvMemcpy`（拷贝它）。SVM 的各个子能力是互相组合的。

**普通路径**则发命令到设备端：`svm_memset_client(devid, dst, destMax, value, num)`（实现在 `svm_memset_client.c`，本讲不展开）：

[svm_set.c:173](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L173) —— 普通路径：`svm_memset_client` 下发到设备。

#### 4.3.4 代码实践

**实践目标**：理解能力位如何串起「申请」与「使用」两端。

**操作步骤**（源码阅读 + 推理）：

1. 回到 4.1 的 [svm_alloc.c:248](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L248)，确认普通 device/host 内存申请时会被加上 `SVM_FLAG_CAP_MEMSET`。
2. 再看 [svm_set.c:95](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_set.c#L95) 的 `svm_memset_check_addr_prop_cap`，它正是检查这一位。
3. 推理：如果用 VMM 的「VA_ONLY」方式申请了一段**只有虚拟地址、没有物理页**的内存（见 u4-l3），对它调 `drvMemsetD8` 会发生什么？

**需要观察的现象 / 预期结果**：

- VA_ONLY 内存不会带 `SVM_FLAG_CAP_MEMSET`（VMM 走的是另一套 flag 装配），因此 `drvMemsetD8` 会在能力校验处返回 `DRV_ERROR_PARA_ERROR`，提前挡掉，避免对一个没有物理页的空壳地址做 memset。

> 待本地验证：确认 VA_ONLY / VMM 路径装配的 flag 集合确实不含 `SVM_FLAG_CAP_MEMSET`（可在 `svm_vmm.c` 中核对）。

#### 4.3.5 小练习与答案

**练习 1**：为什么快路径要求 `num < SVM_MEMSET_FAST_LIMIT_SIZE`（512KB）？

**参考答案**：快路径依赖一块**固定 512KB** 的全零 Host 内存（`svm_get_host_zero_mem` 申请的大小就是 `SVM_MEMSET_FAST_LIMIT_SIZE`）。若要清零的范围超过这块缓冲区，就没法一次拷完，只能走普通 memset 命令路径。512KB 是「快路径收益」与「预申请缓冲成本」之间的折中。

**练习 2**：快路径为什么额外要求 `devid != host_devid`（即目标必须是 device 内存）？

**参考答案**：清「Host 内存」为零时，CPU 可以直接高效写零，没必要绕一圈走 DMA 拷贝；只有清「device 内存」时，用预申请的 Host 零内存做一次 H2D 拷贝，借助 DMA 引擎才比「设备逐字节写零」更快。所以快路径只对 device 内存生效。

---

### 4.4 svm_mem_ctl：内存控制 halMemCtl 的分发表

#### 4.4.1 概念说明

`svm_mem_ctl.c` 实现的是「内存控制」接口 `halMemCtl(type, param, ...)`。它和前面三个「动作型」接口（申请/释放/赋值）不同，是一个**控制类总入口**，用来做与某段内存相关的**辅助操作**：查询当前芯片支持哪些内存特性、对一段地址做内存修复、反查某地址归属的 module_id 等。

它的核心设计手法是**函数指针表分发**：用一个以 `type` 为下标的静态数组 `svm_mem_ctrl_handlers[]`，把每种控制类型映射到一个处理函数。`halMemCtl` 本身只做下标越界检查和分发，**真正的逻辑全在各 handler 里**。这种「表驱动 + 函数指针」的手法，和 u2-l2 里见过的 DSMI 命令表、u3-l5 里见过的 queryfeature 是同一类设计——新增一种控制类型只需加一个枚举值、写一个函数、在表里登记一行，主入口零改动。

#### 4.4.2 核心流程

```
halMemCtl(type, param_value, ..., out_value, ...)
└─ 越界/空指针检查: type ∈ [0, CTRL_TYPE_MAX) 且 handler 非空
└─ svm_mem_ctrl_handlers[type](param_value, ..., out_value, ...)

当前注册的 handler:
  CTRL_TYPE_SUPPORT_FEATURE   → mem_ctrl_support_feature  ← 查芯片支持的内存特性
  CTRL_TYPE_MEM_REPAIR        → mem_ctrl_mem_repair        ← 内存修复（隔离坏页）
  CTRL_TYPE_GET_ADDR_MODULE_ID→ mem_ctrl_get_addr_module_id← 反查地址归属 module_id
```

#### 4.4.3 源码精读

**主入口 `halMemCtl`** 极简：校验 `type` 合法且对应 handler 非空（否则返回 `DRV_ERROR_NOT_SUPPORT`），然后调用 handler：

[svm_mem_ctl.c:151-165](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L151-L165) —— `halMemCtl`：越界检查 + 表分发。

**分发表本身**用「指定初始化器」（`[CTRL_TYPE_...] = func`）写成，未赋值的槽位是 NULL，主入口会把它当作「不支持」处理：

[svm_mem_ctl.c:144-149](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L144-L149) —— `svm_mem_ctrl_handlers[]` 分发表。

**典型 handler 之一：特性查询**。`mem_ctrl_support_feature` 内部又嵌了一张二级函数指针表 `mem_ctl_feature_is_support[]`（同样是「枚举下标 + 函数指针」），逐位查询 PCIe BAR 内存、巨页、大页等是否支持。注意它用 `#if` 区分了 ESL（Cloud V4/V5）平台——这些精简平台直接返回 0（全不支持）：

[svm_mem_ctl.c:73-104](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L73-L104) —— `mem_ctrl_support_feature`：二级函数指针表逐位查特性。

**典型 handler 之二：内存修复**。`mem_ctrl_mem_repair` 校验参数后转调 `svm_mem_repair`（实现在 `svm_mem_repair.c`）——当设备出现坏页时，用它把坏页隔离、把数据迁移到好页，是设备可靠性的重要一环：

[svm_mem_ctl.c:23-36](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L23-L36) —— `mem_ctrl_mem_repair`：参数校验 → 转调 `svm_mem_repair`。

**典型 handler 之三：反查 module_id**。`mem_ctrl_get_addr_module_id` 用 `svm_get_prop` 读出地址的 flag，再从中提取 module_id——又一次消费 4.1 里装配的属性：

[svm_mem_ctl.c:115-142](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L115-L142) —— `mem_ctrl_get_addr_module_id`：用 `svm_get_prop` 反查归属模块。

> 注意 `svm_get_prop` 的实现：它在红黑树里按 `[start, start+size)` 区间查找包含该 VA 的 handle，并递增引用计数 `ref`，用完再 `handle_put` 递减。这就是 `svm_mem_free` 里 ref 计数检查的同一套机制：

[malloc_mng.c:842-855](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L842-L855) —— `svm_get_prop`：红黑树区间查找 + 引用计数。

#### 4.4.4 代码实践

**实践目标**：体会「表驱动分发」如何让扩展变得低成本。

**操作步骤**：

1. 阅读 [svm_mem_ctl.c:144-165](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_mem_ctl.c#L144-L165)，确认主入口 `halMemCtl` 没有任何业务逻辑，只有「检查 + 分发」。
2. 假设要新增一种控制操作 `CTRL_TYPE_QUERY_MEM_USAGE`（查询内存用量），列出你需要改动的全部位置。

**需要观察的现象 / 预期结果**（应得到一份「新增清单」）：

1. 在 `CTRL_TYPE_*` 枚举里加一个新值（在 `CTRL_TYPE_MAX` 之前）；
2. 写一个新函数 `mem_ctrl_query_mem_usage(param, ..., out, ...)`；
3. 在 `svm_mem_ctrl_handlers[]` 表里加一行 `[CTRL_TYPE_QUERY_MEM_USAGE] = mem_ctrl_query_mem_usage`。

`halMemCtl` 主入口**一行都不用改**——这正是表驱动分发的价值。注意 `SUPPORT_FEATURE_MAX_NUM` 这类数组上界要同步调整。

> 待本地验证：`CTRL_TYPE_MAX` 的实际取值与各枚举定义位置（在 svm_mem_ctl 相关头文件中）。

#### 4.4.5 小练习与答案

**练习 1**：`halMemCtl` 对一个未注册 handler 的 `type` 会返回什么？为什么这种设计是安全的？

**参考答案**：返回 `DRV_ERROR_NOT_SUPPORT`。因为分发表用指定初始化器构造，未赋值的槽位是 NULL 指针，主入口在分发前显式检查了 `svm_mem_ctrl_handlers[type] == NULL`，所以绝不会解引用空指针，天然安全。

**练习 2**：`mem_ctrl_support_feature` 为什么在 ESL 平台上直接返回 0？

**参考答案**：ESL（Cloud V4/V5 精简形态）是资源/特性受限的部署场景，不支持 PCIe BAR 内存、巨页等高级特性。用编译期 `#if` 直接把整段查询短路成「全不支持」，既省代码体积，又避免在精简平台上误报支持能力。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**一次申请的全生命周期跟踪**」任务。

**背景**：假设上层 Runtime 执行如下伪代码（本任务为源码阅读型，无需真机运行）：

```c
// 示例代码（非项目原有，仅用于说明调用语义）
void *dev_ptr = NULL;
halMemAlloc(&dev_ptr, 1*1024*1024, MEM_DEV | 0);   // 申请 1MB 0 号设备内存
drvMemsetD8((DVdeviceptr)dev_ptr, 1*1024*1024, 0, 1*1024*1024);  // 清零
halMemFree(dev_ptr);                                 // 释放
```

**任务**：

1. **画三张图**：
   - 图 A：`halMemAlloc` 的完整调用链（要标出「用户态/内核态」分界，即 `svm_cmd_mmap` 和 `svm_cmd_ioctl` 两处系统调用）；
   - 图 B：`drvMemsetD8` 的调用链（标出本次走的是快路径还是普通路径，并说明判断依据——`value==0`、`devid!=host`、`num<512KB` 三个条件全满足，故走快路径）；
   - 图 C：`halMemFree` 的逆序回收链（标出 depopulate 在前、va_free 在后）。
2. **写一段话**：解释「申请时装配的 `SVM_FLAG_CAP_MEMSET` 能力位」是如何在 `drvMemsetD8` 里被消费的（即 4.1 的装配与 4.3 的校验如何闭环）。
3. **回答一个开放问题**：如果在 `halMemFree(dev_ptr)` 之后，立刻又有别处错误地调用了 `halMemFree(dev_ptr)`（double free），SVM 会如何检测并拒绝？提示：看 `svm_mem_free` 开头对 `svm_get_prop` 返回值的处理（[svm_alloc.c:275-281](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_alloc.c#L275-L281)）——首次 free 已把 handle 从红黑树摘下，二次 free 时 `svm_get_prop` 找不到对应区间，返回错误并打印 "Addr is not alloced or free repeatedly"。

**预期结果**：三张图能清晰呈现「VA 分配 → mmap → populate 物理页 → 建页表」的正向链，以及其严格逆序的回收链；能力位闭环和 double-free 检测都能用源码行号佐证。

## 6. 本讲小结

- SVM 的申请/释放是**两阶段设计**：先从预留范围分配虚拟地址并 `mmap` 到字符设备（阶段一），再 `ioctl(SVM_MPL_POPULATE)` 陷入内核申请物理页、建立页表（阶段二）。`svm_normal_malloc` 是这一设计的集大成者。
- `svm_alloc.c` 是对外门面，负责把上层压缩进 `flag` 位域的语义（devid / 内存类型 / 页大小 / 模块 id）解析出来，并**装配一整套 `SVM_FLAG_CAP_*` 能力位**，供后续 memset、拷贝、共享等模块做权限校验。
- `halMemFree` 严格按申请的**逆序**回收：先 depopulate（解页表 + 释放物理页），再 va_free（munmap + 归还 VA）。逆序是为了保证「释放物理页/页表时 VA 仍有效」，否则会泄漏物理页。
- handle 用**红黑树按区间索引**，配 ref 引用计数；`svm_free` 遇到 busy 会把 handle 重新插回，靠 `SVM_SMP_DEL_MEM_EVENT` 事件做延迟释放，从而安全处理「物理页仍被设备占用」的情形。
- `drvMemsetD8` 体现「能力位校验」闭环（查 `SVM_FLAG_CAP_MEMSET`），并用一块预申请的全零 Host 内存实现快路径清零（拷零代替写零）。
- `halMemCtl` 用「函数指针表 + 指定初始化器」的表驱动分发，把特性查询、内存修复、module_id 反查等控制类操作统一收口，新增类型零改主入口。

## 7. 下一步学习建议

本讲讲清了「整块申请/释放」的主链路，但故意留了几个口子，正好是后续讲义的主题：

- **VMM 分离申请**：4.2 里反复提到的「VA_ONLY」和「两阶段可独立调用」正是 VMM 的基石。`halMemAddressReserve`/`halMemCreate`/`halMemMap` 如何把「分 VA」「分物理页」「建映射」彻底拆开重复利用物理内存，见 [u4-l3 SVM VMM](./u4-l3-svm-vmm.md)。
- **多级分配器内幕**：本讲的 `svm_alloc_va`/cache 快路径只点到为止。MGA 地址空间管理、cache_malloc/gen_allocator 如何协同实现虚拟地址的快速分配回收，见 [u4-l5 SVM v3 地址空间与分配器架构](./u4-l5-svm-allocator-architecture.md)。
- **拷贝与共享**：申请到的内存如何在 Host↔Device 间搬运、如何在设备间共享，见 [u4-l4 内存拷贝与共享机制](./u4-l4-svm-copy-and-share.md)。
- **内核侧 populate 的真身**：`SVM_MPL_POPULATE` ioctl 在内核态到底如何申请物理页、写页表项，需要下沉到 SDK-driver 内核层，见 [u6 单元](./u6-l1-sdk-driver-and-kernel-adapt.md)。

建议下一讲直接进入 [u4-l3 SVM VMM](./u4-l3-svm-vmm.md)，把「虚拟地址与物理地址分离」这最后一块拼图补齐，就能完整掌握 SVM 内存管理的全貌。
