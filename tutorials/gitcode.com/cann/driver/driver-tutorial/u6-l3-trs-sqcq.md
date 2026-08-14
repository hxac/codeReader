# TRS 任务资源调度：SQ/CQ 通信与 mailbox

## 1. 本讲目标

本讲进入昇腾驱动的「任务下发」核心机制——**TRS（Task Resource Schedule，任务资源调度）**。学完本讲你应当能够：

- 说清 **SQ/CQ（提交队列/完成队列）** 这种「提交—完成」通信模型在昇腾 NPU 上是如何实现的；
- 区分 TRS 的**数据面（SQ/CQ 快速路径）**与**控制面（mailbox/ioctl 慢速路径）**，理解为什么任务下发不走系统调用；
- 读懂 `trs_interface.c` 的「参数校验 → 按连接形态分发」门面套路，以及 PCIE/HCCS/RC 与 UB 两条路径的差异；
- 跟踪一次 `halSqTaskSend` 的完整下发链路：信用检查 → 填充 SQE → 推进 tail → 敲 doorbell；
- 理解内核侧 `trsdrv` 的模块装配、软 SQ 调度线程，以及 mailbox 报文如何承载控制类消息到达设备固件。

## 2. 前置知识

本讲建立在 [u6-l1](u6-l1-sdk-driver-and-kernel-adapt.md)（SDK-driver 内核层与 kernel_adapt）之上。开始前请先回忆以下几个概念：

- **用户态 / 内核态**：`ascend_hal` 编译为用户态动态库（`.so`），`sdk_driver` 编译为内核模块（`.ko`），二者通过字符设备 + `ioctl` 跨态通信。
- **ioctl**：用户态程序通过 `ioctl(fd, cmd, arg)` 陷入内核执行命令，是本讲中「控制面」进入内核的入口。
- **mmap**：把设备的一段内存映射到用户进程地址空间，映射之后用户态读写这块内存等价于读写设备内存，**无需再陷入内核**。这是「数据面」能跑得快的关键。
- **环形队列（ring buffer）**：一块固定大小的内存 + 一个生产者指针（tail）+ 一个消费者指针（head），头尾相接循环使用。本讲的 SQ/CQ 都是环形队列。
- **Host / Device**：Host 指主机（CPU 侧），Device 指 NPU（设备侧）。

下面先用通俗语言建立两个直觉。

**直觉一：餐厅的点菜单。** 想象一家餐厅，服务员（Host）把客人点的菜写在一张长条点菜单上，厨师（Device）按顺序做菜。

- 这张「点菜单」就是 **SQ（Submission Queue，提交队列）**，菜单上每一格是一道菜（一个 **SQE，Submission Queue Entry，提交队列项**，即一个任务描述符）。
- 服务员写完菜要喊一声「新单来了！」，厨师才会去看菜单——这一嗓子就是 **doorbell（门铃寄存器）**。
- 菜做好后，厨师在另一张「出菜登记表」上打个勾——这张表就是 **CQ（Completion Queue，完成队列）**，每一格是一个 **CQE（完成队列项）**。

**直觉二：菜单是公用的，但申请菜单要找经理。** 服务员日常写菜（下发任务）非常频繁，走的是「数据面」；但「申请新菜单、注册新服务员、分配桌号」这类事情不常发生，要找经理（设备固件）办手续，走的是「控制面」。TRS 用两套通道分别承载这两类流量。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 所在源码树 | 作用 |
| --- | --- | --- |
| `src/ascend_hal/trs/core/trs_interface.c` | ascend_hal（用户态） | 对外 `hal*` 接口门面：参数校验 + 按连接形态分发 |
| `src/ascend_hal/trs/core/trs_sqcq.c` | ascend_hal（用户态） | SQ/CQ 的核心实现：分配、mmap、任务下发快速路径 |
| `src/ascend_hal/trs/core/trs_sqcq.h` | ascend_hal（用户态） | SQ/CQ 关键数据结构（`sqcq_usr_info`、`trs_sq_ctrl`） |
| `src/ascend_hal/trs/core/trs_res.c` | ascend_hal（用户态） | 资源 ID 管理（流/事件/模型/通知等） |
| `src/ascend_hal/trs/core/trs_cb_event.c` | ascend_hal（用户态） | 回调类任务的完成事件上报 |
| `src/sdk_driver/trsdrv/trs/trs_init.c` | sdk_driver（内核态） | 内核模块 `trsdrv.ko` 的加载/卸载与子模块装配 |
| `src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c` | sdk_driver（内核态） | 内核侧硬件 SQ 调度（含软 SQ 调度线程） |
| `src/sdk_driver/trsdrv/trs/inc/trs_mailbox_def.h` | sdk_driver（内核态） | mailbox 报文发送接口与头部初始化 |
| `src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h` | sdk_driver（内核态） | mailbox 报文格式与命令类型枚举 |
| `src/sdk_driver/trsdrv/trs/trs_core/command/ioctl/trs_ioctl.h` | 内核/用户**共享** | ioctl 命令号、连接形态枚举、UIO 地址枚举（**注意：全仓库只有这一份 `trs_ioctl.h`，用户态经 include 路径共用**） |

> 一个容易踩坑的点：用户态的 `trs_sqcq.c` 里 `#include "trs_ioctl.h"`，但全仓库只有 `src/sdk_driver/trsdrv/trs/.../ioctl/trs_ioctl.h` 这一份。它是用户态和内核态**共享**的头文件，定义了 ioctl 命令号、连接形态枚举等双方必须一致的契约。

## 4. 核心概念与源码讲解

### 4.1 TRS 概念与 trs/core 模块总览

#### 4.1.1 概念说明

**TRS（Task Resource Schedule）** 是 HAL 层中负责「把任务送到 NPU 上执行」的子模块。上层计算运行时（acl/Runtime）把一个算子或一段计算图封装成一个任务描述符，交给 TRS 下发；TRS 把它写进 SQ，设备侧的调度单元（TSCPU，即 Device 上的控制 CPU）取出执行，完成后通过 CQ 或回调事件回报。

理解 TRS 的关键是抓住**两平面模型**：

| 维度 | 数据面（Data Plane） | 控制面（Control Plane） |
| --- | --- | --- |
| 承载内容 | 任务下发与完成回收 | 资源申请/释放、SQ/CQ 本身的创建 |
| 通道 | SQ/CQ 环形队列（mmap 内存） + doorbell | ioctl → 内核 → mailbox 报文 → 设备固件 |
| 频率 | 极高（每个任务都走） | 低（初始化/销毁阶段） |
| 是否每步陷内核 | **否**（mmap 后纯用户态内存写） | 是（每次 ioctl） |

一个常见的误解是「下发任务也要 ioctl」。实际上，**SQ 在分配阶段被 mmap 到用户态后，下发任务只是往这块内存写数据 + 写一个 doorbell 寄存器，全程不陷内核**——这正是 TRS 性能的来源。ioctl 只在分配/释放 SQ、申请资源等控制面操作时才用。

#### 4.1.2 核心流程

TRS 用户态的整体流程可以概括为「初始化 → 分配 SQ/CQ → 循环下发任务 → 回收完成 → 释放」：

```text
设备打开时（halDeviceOpen 链路）：
  trs_hw_info_init(dev_id)          # ioctl 查询硬件信息：ts_num/connection_type/sq_send_mode
  trs_dev_sq_cq_init(dev_id)        # 为每个 TS 查询 SQ/CQ 最大数量，分配 sqcq_usr_info 数组

运行期（每个 stream）：
  halSqCqAllocate()                 # 控制面：ioctl 申请一对 SQ/CQ，mmap SQE 环形缓冲到用户态
  halResourceIdAlloc()              # 控制面：ioctl 申请 stream/event/model 等资源 ID
  循环 {
    halSqTaskSend()                 # 数据面：填 SQE → 推进 tail → 敲 doorbell（不陷内核）
    ...设备执行...
    （完成经 CQ 或回调事件回报）
  }
  halSqCqFree() / halResourceIdFree()  # 控制面：释放
```

其中 `ts_num` 是 **TS（Task Scheduler，任务调度器）** 的数量，一张 NPU 内可能有多个 TS，TRS 的数据结构按 `(dev_id, ts_id, type)` 三维索引。

#### 4.1.3 源码精读

先看设备级状态。TRS 用三个全局数组管理所有设备：

- `dev_ctx[dev_id]`：每台设备的硬件信息（TS 数、连接形态、发送模式）与互斥锁；
- `cqcq_ctxs[dev_id][ts_id][type]`：按「设备 × TS × 队列类型」三维组织所有 SQ/CQ 的用户态信息。

[src/ascend_hal/trs/core/trs_sqcq.c:67-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L67-L80) 定义了这两个核心容器。`trs_sqcq_ctx` 内含 `sq_info`/`cq_info`（指向 `sqcq_usr_info` 数组）和它们的数量。

连接形态是一个关键枚举，它决定了后续每条 `hal*` 接口走哪条路径。[src/sdk_driver/trsdrv/trs/trs_core/command/ioctl/trs_ioctl.h:73-79](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/command/ioctl/trs_ioctl.h#L73-L79) 定义了五种连接形态：PCIE、HCCS（片间高速互联）、UB（Unified Bus，灵衢超节点总线）、RC（Root Complex，SOC 形态）、UNKNOWN。本讲会反复看到「PCIE/HCCS/RC 走本地路径，UB 走 urma 路径」的分发。

设备级信息在 `trs_hw_info_init` 中通过一次 `TRS_HW_INFO_QUERY` ioctl 从内核取回并填入 `dev_ctx`：

[src/ascend_hal/trs/core/trs_sqcq.c:276-292](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L276-L292) 用 ioctl 查到 `hw_type`、`tsnum`、`connection_type`、`sq_send_mode` 并初始化两把锁。

随后 `trs_dev_sq_cq_init` 为每个 TS 初始化 SQ/CQ 信息表：

[src/ascend_hal/trs/core/trs_sqcq.c:564-584](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L564-L584) 遍历所有 TS，调用 `trs_ts_sq_cq_init`；它内部用 `trs_id_type_init` → ioctl `TRS_RES_ID_MAX_QUERY` 查询「该类型最多有多少个 SQ/CQ」，再 `malloc` 对应大小的 `sqcq_usr_info` 数组并初始化每个元素的锁。失败时对已初始化的 TS 逆序回滚。

SQ/CQ 类型用一个翻译表把对外枚举映射到内核侧枚举，[src/ascend_hal/trs/core/trs_sqcq.c:138-144](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L138-L144)：例如 `DRV_NORMAL_TYPE → TRS_HW_SQ`（硬件队列）、`DRV_CTRL_TYPE → TRS_SW_SQ`（软件队列）、`DRV_CALLBACK_TYPE → TRS_CB_SQ`（回调队列）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立 TRS 用户态的三维索引心智模型。
2. **操作步骤**：在 `trs_sqcq.c` 中找到 `trs_get_sqcq_ctx`、`_trs_get_sq_info`，确认它们都是用 `cqcq_ctxs[dev_id][ts_id][type]` 这个三维下标定位的；再读 `trs_get_sq_info`（[L350-368](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L350-L368)）观察它如何用 `type` 经翻译表校验、再用 `sq_id` 在数组里取元素、最后用 `valid` 标志判断是否已分配。
3. **需要观察的现象**：定位一个 SQ 需要「设备号 + TS 号 + 队列类型 + SQ 号」四个量。
4. **预期结果**：能口述「一个 SQ 在内存里是 `cqcq_ctxs[dev][ts][type].sq_info[sq_id]`」。
5. 无需硬件，纯静态阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `trs_dev_sq_cq_init` 要先 ioctl 查询「SQ/CQ 最大数量」再 `malloc`，而不是写死一个常量？

> **答**：不同芯片形态（ascend910B / ascend950）、不同 TS 的 SQ/CQ 容量不同，写死会浪费内存或不够用。运行期查询内核得到精确数量，按需分配 `sqcq_usr_info` 数组。

**练习 2**：`dev_ctx` 里为什么要存 `connection_type`？

> **答**：后续每条 `hal*` 接口都要根据连接形态（PCIE 还是 UB）选择不同的实现路径，存一份在设备级上下文里可以避免每次下发任务都 ioctl 查询，保证数据面零 ioctl。

---

### 4.2 trs_interface：公共接口门面与连接形态分发

#### 4.2.1 概念说明

`trs_interface.c` 是 TRS 对外暴露的 `hal*` 接口的**门面层（facade）**。它本身几乎不实现业务逻辑，只做两件事：

1. **参数校验**：拦截非法的 `devId`/`tsId`/空指针/越界值；
2. **按连接形态分发**：用 `trs_get_connection_type(devId)` 取得连接形态，再 `switch` 到本地路径（PCIE/HCCS/RC）或 urma 路径（UB）。

这种「先校验、再分发」的门面套路在昇腾驱动里非常普遍，好处是：业务实现（`trs_sqcq.c` 等）不用重复写校验，上层调用者只看到一个统一入口。

与 `trs_interface.c` 平级的还有 `trs_res.c`，它是**资源管理**的门面，负责 stream/event/model/notify 等资源 ID 的申请与释放。

#### 4.2.2 核心流程

门面的通用流程：

```text
halXxx(devId, in, out):
  1. 空指针 / devId 越界 / tsId 越界校验  → 失败返回 DRV_ERROR_INVALID_VALUE
  2. 业务参数校验（如 sqeSize 上限）       → trs_xxx_para_check()
  3. connection_type = trs_get_connection_type(devId)
  4. switch (connection_type):
       PCIE / HCCS / RC → trs_xxx()         # 本地路径
       UB               → trs_xxx_urma()    # urma 路径（弱符号）
```

注意第 4 步：UB 路径的函数（如 `trs_sqcq_urma_alloc`、`trs_sq_task_send_urma`）在 `trs_interface.c` 顶部被声明为 **`__attribute__((weak))` 弱符号**，默认实现是直接返回 `DRV_ERROR_NOT_SUPPORT`。只有当 urma 子库被链接进来时，强符号才覆盖弱符号。这是一种**编译期解耦**：非 UB 形态编译时不必拖入整个 urma 实现。

#### 4.2.3 源码精读

以 `halSqCqAllocate`（分配 SQ/CQ）为典型，看门面三段式：

[src/ascend_hal/trs/core/trs_interface.c:256-285](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L256-L285)：先 `trs_sqcq_alloc_para_check` 校验，再用 `connection_type` 分发到 `trs_sqcq_alloc`（本地）或 `trs_sqcq_urma_alloc`（UB）。

任务下发入口 `halSqTaskSend` 遵循完全相同的结构：

[src/ascend_hal/trs/core/trs_interface.c:1019-1066](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L1019-L1066)：先用 `trs_get_sq_info` 取到 SQ 用户态信息（取不到说明未分配），再用 `trs_is_sq_support_send` 判断该 SQ 是否支持下发（`TSDRV_FLAG_ONLY_SQCQ_ID` 这类「只申请 ID 不要内存」的 SQ 不支持），最后按连接形态分发到 `trs_sq_task_send`（本地）或 `trs_sq_task_send_urma`（UB）。

弱符号声明在文件顶部，[src/ascend_hal/trs/core/trs_interface.c:32-39](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L32-L39)：`trs_sqcq_urma_alloc` 是弱符号，默认返回 `DRV_ERROR_NOT_SUPPORT`。

资源管理门面在 `trs_res.c`。资源 ID 按「设备 × TS × 资源类型」三维管理，[src/ascend_hal/trs/core/trs_res.c:29](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L29) 定义了 `res_id_ctx[dev][ts][type]` 容器。支持的资源类型见翻译表 [src/ascend_hal/trs/core/trs_res.c:42-46](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L42-L46)：stream、event、model、notify、cmo、cnt_notify、sq、cq。

资源申请的对外入口 `_halResourceIdAlloc` 同样是「校验 + 转调」：

[src/ascend_hal/trs/core/trs_res.c:543-552](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L543-L552)：`trs_res_alloc_para_check` 后调用 `trs_local_res_alloc`（内部经 ioctl 进内核，再由 mailbox 到设备固件，详见 4.4）。资源数量查询 `trs_id_query` 走的是 `TRS_RES_ID_MAX_QUERY` ioctl，[src/ascend_hal/trs/core/trs_res.c:701-714](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L701-L714)。

对于 notify（通知）类资源，TRS 还会把设备侧的通知记录地址映射进用户态，让 Host 能直接轮询，[src/ascend_hal/trs/core/trs_res.c:129-164](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L129-L164) 调 `halResAddrMap` 映射、`trs_register_reg` 注册。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：体会门面「校验 + 分发」套路的统一性。
2. **操作步骤**：在 `trs_interface.c` 中分别打开 `halSqCqAllocate`（L256）、`halSqCqFree`（L287）、`halSqCqQuery`（L324）、`halSqCqConfig`（L433）、`halSqTaskSend`（L1019），对比它们的结构。
3. **需要观察的现象**：几乎每个函数都是「para_check → `trs_get_connection_type` → `switch` 四个 case（PCIE/HCCS/RC 合并、UB 单列、default 报错）」的模板。
4. **预期结果**：能预测任何一个新 `hal*` 接口的开头写法。再找一处 UB 专属能力（如 `halAsyncDmaCreate2D`，L657）——它的 PCIe 分支直接返回 `DRV_ERROR_NOT_SUPPORT`，说明 2D 异步拷贝是 UB 独有能力。
5. 无需硬件，纯静态阅读。

#### 4.2.5 小练习与答案

**练习 1**：弱符号 `trs_sqcq_urma_alloc` 默认返回 `DRV_ERROR_NOT_SUPPORT`。如果不小心在一个 UB 形态的设备上、但 urma 强符号没被链接进来，调用 `halSqCqAllocate` 会怎样？

> **答**：分发到 `trs_sqcq_urma_alloc`，由于只有弱符号，执行默认实现，返回 `DRV_ERROR_NOT_SUPPORT`，SQ/CQ 分配失败。弱符号机制保证了非 UB 编译产物不会因找不到 urma 符号而链接报错。

**练习 2**：资源管理 `trs_res.c` 和 SQ/CQ 管理 `trs_sqcq.c` 都用「设备 × TS × 类型」三维容器，它们的「类型」维度有何不同？

> **答**：`trs_sqcq.c` 的类型是**队列类型**（NORMAL/CALLBACK/CTRL 等，对应 HW_SQ/CB_SQ/SW_SQ）；`trs_res.c` 的类型是**资源类型**（STREAM/EVENT/MODEL/NOTIFY 等）。二者是正交的两类对象，但都按同一三维模式组织。

---

### 4.3 trs_sqcq：SQ/CQ 环形队列与任务下发快速路径

#### 4.3.1 概念说明

本模块是 TRS 的心脏。它要回答两个问题：**SQ/CQ 怎么分配？任务怎么下发？**

分配阶段的关键是 **mmap**：TRS 把设备上的「SQE 环形缓冲 + 一组控制寄存器」映射到用户进程地址空间。映射完成后，用户态直接读写这块内存就能操作队列——写 SQE 是普通内存写，通知设备是写 doorbell 寄存器（也是一段 mmap 的内存）。这就是「数据面零 ioctl」的真相。

下发阶段的核心是一个**环形队列的生产者逻辑**：

- `head`：消费者（设备）位置，设备已处理到这里；
- `tail`：生产者（Host）位置，Host 下次往这里写；
- `depth`：环形队列总槽位数。

Host 写之前先算 **credit（信用/空闲槽位）**，确保不把队列写满溢出；写完推进 `tail`，再敲 doorbell。

ARM（aarch64）是**弱内存序**架构，写操作可能被重排或停留在缓存。因此 SQE 数据写完之后、敲 doorbell 之前，必须插入**内存屏障（memory barrier）**，保证设备看到 doorbell 时一定能读到完整的 SQE。

完成（completion）的回报有两条路：普通任务经 CQ，回调类任务经 **esched 事件**（`trs_cb_event`）。

#### 4.3.2 核心流程

**SQ/CQ 分配流程**（`trs_local_sqcq_alloc`）：

```text
1. (可选) 经 mem_ops 申请 SQ 内存，得到 sq_que_va
2. trs_sq_mmap: 把设备的 SQE 环形缓冲 + 控制寄存器页 mmap 到用户态，得到 sq_map
3. trs_fill_sq_alloc_info: 把 mmap 地址填进 uio_info
4. ioctl(TRS_SQCQ_ALLOC)              # 控制面：让内核登记这对 SQ/CQ
5. trs_sq_info_init → trs_sq_usr_info_init: 把 db/head/tail 等控制指针存进 sqcq_usr_info
6. trs_cq_info_init: 初始化 CQ 用户态信息
```

**任务下发快速路径**（`trs_sq_task_send` → `trs_sq_task_send_uio`）：

```text
1. trs_sq_task_send_check:
     - 对齐/合法性校验
     - trs_sq_credit_check: 算 credit，不够则刷新 head 再算，仍不够返回 NO_RESOURCES
2. (软 SQ) trs_soft_que_prefetch: 预取 SQE 槽位与 tail 寄存器（性能优化）
3. trs_sq_task_fill: memcpy 把 SQE 写进环形缓冲 (tail+i) % depth 处
4. 推进 tail = (tail + sqe_num) % depth
5. trs_sq_task_send_set_db: 写 tail 寄存器 → 敲 doorbell（含内存屏障）
```

环形队列的**信用计算**是经典算法。为区分「满」和「空」（二者都是 head==tail），约定**保留一个槽位不用**，于是：

- 当 tail 未回绕（tail ≥ head）时，空闲槽位为
  \[
  \text{credit} = \text{depth} - (\text{tail} - \text{head}) - 1 = \text{depth} - (\text{tail} - \text{head} + 1)
  \]
- 当 tail 已回绕（tail < head）时，空闲槽位为
  \[
  \text{credit} = \text{head} - \text{tail} - 1
  \]

队列空时 credit = depth − 1（最大），满时 credit = 0。

#### 4.3.3 源码精读

先看承载每个 SQ 状态的数据结构。`trs_sq_ctrl` 把所有「操作这个 SQ 需要的指针」聚在一起：

[src/ascend_hal/trs/core/trs_sqcq.h:45-55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.h#L45-L55)：`que_addr`（SQE 环形缓冲起始）、`db`（doorbell）、`head`/`tail`（队列指针寄存器）、`head_reg`/`tail_reg`（设备侧寄存器镜像）、`shr_info`（共享统计）。而 `sqcq_usr_info`（[trs_sqcq.h:57-77](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.h#L57-L77)）在 `sq_ctrl` 之外还保存了 head/tail 的本地副本、depth、e_size（单个 SQE 大小）、发送互斥锁与读写锁。

mmap 的布局由 `trs_sq_mmap` 决定。它把设备内存分两段映射：

[src/ascend_hal/trs/core/trs_sqcq.c:209-260](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L209-L260)：`non_reg_addr`（可读可写，含 SQE 环形缓冲 + 前几个控制页）与 `reg_addr`（只读，剩余控制寄存器页），再按 `TRS_UIO_*` 枚举把它们切片赋给 `sq_map.ctrl[]`。UIO 地址枚举见 [trs_ioctl.h:18-26](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/command/ioctl/trs_ioctl.h#L18-L26)：SHR_INFO / HEAD / TAIL / DB / HEAD_REG / TAIL_REG。

`trs_sq_usr_info_init` 把内核 ioctl 返回的 `uio_info` 里的地址搬运到 `sqcq_usr_info`：

[src/ascend_hal/trs/core/trs_sqcq.c:716-770](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L716-L770)：把 `que_addr`、`db`、`head`、`tail`、`head_reg`、`tail_reg`、`shr_info`、`soft_que_flag` 一一存好，并设置 `depth`/`e_size`/`max_num`、置 `valid=1`。注意 L734：`uio_flag==0` 表示该 SQ 不支持用户态直接操作，此时立即 `trs_sq_munmap` 解除映射并把 `que_addr` 置空（后续走 ioctl 下发）。

分配主流程 `trs_local_sqcq_alloc` 把上述步骤串起来，[src/ascend_hal/trs/core/trs_sqcq.c:981-1045](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L981-L1045)：可选申请 SQ 内存 → mmap → 填 uio_info → 在 `trs_sqcq_mutex` 保护下 ioctl `TRS_SQCQ_ALLOC` → 初始化 sq/cq 用户态信息；失败时逆序回收（munmap + mem_free）。

接着是任务下发的快速路径。先看内存屏障与 doorbell 写入函数：

[src/ascend_hal/trs/core/trs_sqcq.c:29-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L29-L44) 定义了 `mb()`/`wmb()`/`smp_wmb()`，在 aarch64 上分别是 `dsb(sy)`/`dsb(st)`/`dmb(ishst)`。`trs_set_sq_tail`（[L648-652](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L648-L652)）写 tail 前插 `smp_wmb()`，`trs_set_sq_db`（[L664-668](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L664-L668)）写 doorbell 前插 `wmb()`——保证 SQE 数据先于 doorbell 对设备可见。

信用计算 `trs_sq_get_credit` 与 4.3.2 的公式一一对应：

[src/ascend_hal/trs/core/trs_sqcq.c:1530-1539](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1530-L1539)：`tail>=head` 返回 `depth-(tail-head+1)`，否则返回 `head-tail-1`。`trs_sq_credit_check`（[L1565-1585](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1565-L1585)）在 credit 不足时调 `trs_update_sq_head`（[L1521-1528](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1521-L1528)，从寄存器或 ioctl 刷新 head）再算一次，仍不足则返回 `DRV_ERROR_NO_RESOURCES`。

填充 SQE 是简单的环形拷贝，[src/ascend_hal/trs/core/trs_sqcq.c:1609-1625](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1609-L1625)：把 `info->sqe_addr` 起的 `sqe_num` 个 SQE 拷到 `(tail+i) % depth` 处。

快速路径主体 `trs_sq_task_send_uio`：

[src/ascend_hal/trs/core/trs_sqcq.c:1645-1680](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1645-L1680)：check → 软 SQ 预取 → fill → 推进 tail → `trs_sq_task_send_set_db` 敲 doorbell。注意 L1670 的 `trs_get_sq_ctrl_flag`：如果调用者给的 `sqe_addr` 落在 SQE 环形缓冲范围内，说明调用者**自己已经填好了 SQE**（`TRS_SQ_CTRL_BY_USER_FLAG`），TRS 就跳过 fill；否则 TRS 代填（`TRS_SQ_CTRL_BY_TRS_FLAG`）。判定函数见 [trs_sqcq.h:169-177](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.h#L169-L177)。

doorbell 写入策略在 `trs_sq_task_send_set_db`，[src/ascend_hal/trs/core/trs_sqcq.c:1627-1643](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1627-L1643)：先写 tail 寄存器；若是软 SQ 且本次刚好填到满足「指定数目」（`trs_sq_has_specified_num_task`），则把 **sq_id 写进 doorbell**——这是给内核调度线程的「快发」提示（详见 4.4），让内核无需扫描所有 SQ 就知道哪个 SQ 有活干；否则把 tail 写进 doorbell，直接通知硬件。

分发入口 `trs_sq_task_send` 还处理了非 UIO 的情况，[src/ascend_hal/trs/core/trs_sqcq.c:1682-1701](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1682-L1701)：若 SQ 支持 UIO（`que_addr != NULL`）走快速路径；否则若是 STARS 实例的回调任务，走 `trs_cb_event_submit`；再否则退化为 ioctl `TRS_SQCQ_SEND`。

最后看回调完成路径。回调类任务（`DRV_CALLBACK_TYPE`）的完成不写 CQ，而是经 esched 事件上报。`trs_cb_event_init` 建立事件接收框架：

[src/ascend_hal/trs/core/trs_cb_event.c:35-78](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_cb_event.c#L35-L78)：attach 设备 → 创建扩展事件组（绑定 CP CPU，最多 1024 线程）→ 为每个线程订阅 `EVENT_TS_CALLBACK_MSG` 事件。等待完成用 `trs_cb_event_wait`（[L96-131](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_cb_event.c#L96-L131)）阻塞在 `halEschedWaitEvent` 上，按 subevent 区分硬件超时、软件消息、STARS 事件三种情况。

#### 4.3.4 代码实践（源码追踪型）

1. **实践目标**：完整跟踪一次任务下发的数据面路径，确认它「不陷内核」。
2. **操作步骤**：从 `halSqTaskSend`（[trs_interface.c:1019](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L1019)）出发，假定连接形态是 PCIE，跟踪到 `trs_sq_task_send`（[trs_sqcq.c:1682](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1682)）→ `trs_sq_task_send_uio`（[L1645](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1645)）。
3. **需要观察的现象**：在 `trs_sq_task_send_uio` 的执行路径里，找出所有「会触发系统调用」的点。你会发现：`memcpy_s`（用户态内存写）、`__builtin_prefetch`（预取，不陷内核）、`trs_set_sq_tail`/`trs_set_sq_db`（写 mmap 的寄存器内存，不陷内核）。**整条路径没有 ioctl。**
4. **预期结果**：在纸上画出「Host 用户态 → SQE 环形缓冲（mmap 内存）→ doorbell 寄存器（mmap 内存）→ 设备感知」的链路，并标注「内存屏障插在 fill 之后、doorbell 之前」。
5. 这是纯静态追踪，无需硬件；若要在真机上验证 doorbell 是否真被写，需借助设备侧性能采集（prof），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`trs_sq_get_credit` 为什么在两个分支里都减 1？如果不减会怎样？

> **答**：减 1 是为了保留一个空槽位，使「满」和「空」可区分。若不减，当 tail 追上 head 时 `tail==head`，既可能表示队列空（一个都没写）也可能表示队列满（写满一圈）——产生歧义，设备无法判断。保留一个槽位后，空时 `tail==head`（credit=depth−1），满时 credit=0，状态唯一。

**练习 2**：`trs_sq_task_fill` 之后为什么要分别用 `smp_wmb()`（写 tail）和 `wmb()`（写 doorbell）两道屏障，能不能只用一道？

> **答**：tail 寄存器和 doorbell 寄存器的可见性要求不同。tail 是给「同进程/同设备」看的队列指针，用较轻的 `smp_wmb()`（inner-shareable）即可；doorbell 是真正「按铃」通知设备的强信号，必须用更强的 `wmb()`（`dsb(st)`）确保此前所有写（含 SQE 数据和 tail）都对设备完全可见后再触发。两道屏障对应两个语义层次，不能合并省略。

---

### 4.4 trsdrv 内核侧：模块装配、软 SQ 调度与 mailbox 控制面

#### 4.4.1 概念说明

前面三模块都在用户态（`ascend_hal`）。控制面的 ioctl 进入内核后，由 `sdk_driver/trsdrv`（编译为内核模块 `trsdrv.ko`）接手。本模块讲三件事：

1. **模块装配**：`trsdrv.ko` 加载时如何按特性宏装配十来个子模块，失败如何回滚。
2. **软 SQ（software SQ）调度**：用户态「软 SQ」背后的实际硬件下发，由内核里的调度线程代劳；4.3 里「把 sq_id 写进 doorbell」的快发提示就是给它看的。
3. **mailbox（邮箱）**：内核到设备固件（TSCPU）的控制类消息通道，承载资源申请/释放等操作。

回顾 4.1 的两平面模型：**数据面**（SQ/CQ + doorbell）的「设备侧消费」由设备硬件或内核调度线程完成；**控制面**（资源管理 ioctl）从内核经 mailbox 报文送到设备固件执行。

#### 4.4.2 核心流程

**模块加载/卸载**采用「函数指针表 + 顺序调用 + 失败回滚」模式：

```text
init_trs (模块加载):
  for each 子模块 in g_sub_table:        # 顺序
      if 子模块.init() 失败:
          goto out
  return 0
out:
  for 已 init 的子模块 (逆序): 子模块.uninit()   # 回滚
  return 错误码

exit_trs (模块卸载):
  for each 子模块 (逆序): 子模块.uninit()
```

**软 SQ 调度**：内核起一个 kthread，周期性（有任务时 1000µs，空闲时 4000µs）扫描所有软 SQ，把其中待下发的 SQE 真正提交给硬件 SQ。快发优化：若 doorbell 携带了 trigger sqid，先发该 SQ，再扫描其余。

**mailbox 报文**：每条报文以 `trs_mb_header`（含 `cmd_type` 命令类型 + `valid` 魔数 `0x5A5A`）开头，后接具体 payload；经 `trs_mbox_send` 在指定 channel 上发往设备固件。

#### 4.4.3 源码精读

模块入口在 `trs_init.c`。它用 `ka_module_init`/`ka_module_exit`（kernel_adapt 提供的内核模块宏，回忆 u6-l1）注册加载/卸载函数：

[src/sdk_driver/trsdrv/trs/trs_init.c:34-68](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_init.c#L34-L68) 定义子模块表 `g_sub_table`，每一项是 `{init, uninit}` 函数指针对，由 `CFG_FEATURE_TRS_*` 宏门控（如 `TRS_CORE`、`TRS_STARS`、`TRS_ID_POOL`、`TRS_TSMNG`、`TRS_SHR_ID`、`TRS_SIA_ADAPT/AGENT`、`TRS_MIA_ADAPT/AGENT`、`TRS_SEC_EH_ADAPT/AGENT`）。这正体现了 u6-l1 讲过的「编译期特性宏冻结二进制能力」。

加载逻辑 `init_trs` 是「正向初始化 + 失败逆序回滚」：

[src/sdk_driver/trsdrv/trs/trs_init.c:70-87](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_init.c#L70-L87)：正向循环调 `init()`，任一失败就跳到 `out`，对已初始化的子模块逆序调 `uninit()`。卸载 `exit_trs`（[L89-97](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_init.c#L89-L97)）整体逆序 `uninit`。最后用 `KA_MODULE_LICENSE("GPL")` 等声明模块元信息（[L99-104](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_init.c#L99-L104)）。

软 SQ 的内核调度在 `trs_hw_sqcq.c`。调度线程 `trs_sq_send_thread`：

[src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c:170-189](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c#L170-L189)：kthread 循环里按 1000µs（忙）/4000µs（闲）周期调用 `trs_sq_send_proc`；它先做「快发」，再扫描所有 SQ。调度过程 `trs_sq_send_proc`：

[src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c:134-161](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c#L134-L161)：若 doorbell 带来了 trigger sqid（`get_trigger_sqid`，即 4.3 里「把 sq_id 写进 doorbell」的那一步），先 `trs_hw_sq_send_task` 发该 SQ（快发，免去全扫描）；随后遍历所有引用计数 `ref>0` 的 SQ 逐个发送，并在每轮用 `trs_try_resched` 防止长时间占用 CPU。注意 L142 的 `SQ_SEND_NON_FAIR_MODE`（非公平/优先模式）与公平模式的区分。

mailbox 是控制面的内核→设备通道。报文头部初始化与发送接口：

[src/sdk_driver/trsdrv/trs/inc/trs_mailbox_def.h:21-29](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/inc/trs_mailbox_def.h#L21-L29)：`trs_mbox_init_header` 填 `cmd_type`、清 `result`、置 `valid=TRS_MBOX_MESSAGE_VALID`；`trs_mbox_send`/`trs_mbox_send_ex` 在指定 channel 上发送。

报文格式与命令类型在 `trs_h2d_msg.h`。魔数与头部结构：

[src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h:297-308](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h#L297-L308)：`TRS_MBOX_MESSAGE_VALID` 为 `0x5A5A`（一个易识别的魔数，用于校验报文有效），`struct trs_mb_header` 含 `cmd_type` 等。命令类型枚举：

[src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h:45-75](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h#L45-L75)：`TRS_MSG_GET_RES_ID`/`TRS_MSG_PUT_RES_ID`（资源申请/释放）、`TRS_MSG_GET_PHY_ADDR`（查物理地址）、`TRS_MSG_SQ_REG_MAP`/`TRS_MSG_SQ_REG_UNMAP`（SQ 寄存器映射）、`TRS_MSG_TS_CQ_PROCESS`（CQ 处理）等——这些正是 4.2 里资源管理 ioctl 进入内核后要经 mailbox 送达设备固件的控制操作。每个具体报文（如资源申请）都以 `struct trs_mb_header header` 打头（见文件内多处 `struct xxx { struct trs_mb_header header; ... }`）。

把这些串起来，资源申请的完整跨层路径是：`_halResourceIdAlloc`（用户态 `trs_res.c`）→ ioctl → 内核 `trsdrv` → 组装 `trs_mb_header` 报文 → `trs_mbox_send` → 设备固件（TSCPU）执行 → 结果原路返回。这就解释了为什么 4.1 称资源管理为「控制面慢速路径」。

#### 4.4.4 代码实践（源码追踪型）

1. **实践目标**：打通「控制面 ioctl → 内核 → mailbox → 设备」的完整链路认知。
2. **操作步骤**：
   - 从 `trs_id_query`（[trs_res.c:701](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_res.c#L701)）看到它调用 `trs_dev_io_ctrl(dev_id, cmd, para)` 陷入内核；
   - 在内核侧 `trs_h2d_msg.h` 的命令枚举（[L45-75](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/command/msg/trs_h2d_msg.h#L45-L75)）里找到 `TRS_MSG_GET_RES_ID`，理解资源申请最终变成一条 mailbox 报文；
   - 阅读 `trs_mbox_init_header`（[trs_mailbox_def.h:21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/inc/trs_mailbox_def.h#L21)）确认报文头部如何被打上 `cmd_type` 与 `0x5A5A` 魔数。
3. **需要观察的现象**：同一种「资源申请」语义，在用户态是 ioctl 命令号、在内核变成 `enum trs_msg_cmd_type`、在 mailbox 报文里是 `trs_mb_header.cmd_type`——三层命名不同但一一对应。
4. **预期结果**：能画出「`_halResourceIdAlloc` → ioctl → trsdrv → `trs_mbox_send` → TSCPU」的控制面时序图，并与 4.3 的数据面时序图对比。
5. 纯静态阅读，无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：`init_trs` 为什么在某个子模块 `init` 失败时要逆序调用已初始化模块的 `uninit`？

> **答**：内核模块加载是事务性的——前面子模块可能已经申请了中断、注册了设备号、创建了 proc 节点等资源。若中途失败不回滚，这些半成品资源会泄漏，导致模块加载失败后系统状态残留、再次加载也可能冲突。逆序回滚保证「要么全成功，要么像没来过一样」。

**练习 2**：对比数据面与控制面，为什么资源申请要走 mailbox，而任务下发不走？

> **答**：资源申请是低频但需要「设备固件参与决策」的操作（如分配哪个资源 ID、登记到设备表），必须把请求送到 TSCPU，mailbox 正是这条内核→固件的通道。任务下发是超高频操作，若每次都经 mailbox 往返会引入微秒级延迟且占用固件 CPU；因此用 mmap 把 SQ 暴露给用户态，下发只写内存 + doorbell，由硬件/调度线程在设备侧本地消费，把往返开销摊薄掉。

---

## 5. 综合实践

本实践把四个模块串起来，目标是产出一份「TRS 任务下发端到端链路图」。

**任务**：在一张大图上同时画出**数据面**和**控制面**两条链路，并标注每一步发生在哪一层（用户态 / 内核态 / 设备固件）、是否陷内核、用了哪种通信原语。

建议步骤：

1. **控制面（初始化阶段）**：从 `halSqCqAllocate`（[trs_interface.c:256](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L256)）→ `trs_local_sqcq_alloc`（[trs_sqcq.c:981](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L981)）→ mmap（[L1008](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1008)）→ ioctl `TRS_SQCQ_ALLOC`。标注：这一步陷内核、经 mailbox 到固件。
2. **数据面（稳态下发）**：从 `halSqTaskSend`（[trs_interface.c:1019](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_interface.c#L1019)）→ `trs_sq_task_send_uio`（[trs_sqcq.c:1645](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_sqcq.c#L1645)）→ credit 检查 → fill SQE → 推进 tail → doorbell。标注：**全程不陷内核**，内存屏障插在 fill 与 doorbell 之间。
3. **设备侧消费**：标注软 SQ 由内核 kthread（[trs_hw_sqcq.c:170](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/trs_core/trs_hw_sqcq.c#L170)）代发，快发用 doorbell 携带的 sqid；硬件 SQ 由设备直接消费 doorbell。
4. **完成回报**：普通任务经 CQ，回调任务经 `trs_cb_event_wait`（[trs_cb_event.c:96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/trs/core/trs_cb_event.c#L96)）的 esched 事件。

完成后，用一句话对比两条面：**数据面靠 mmap 内存 + doorbell 实现零 ioctl 高吞吐，控制面靠 ioctl + mailbox 实现低频但需固件参与的资源管理**。

> 说明：本实践为源码阅读型，无需 NPU 硬件即可完成；若要在真机上验证各步骤时序，需借助 driver 的 prof 性能采集工具，**待本地验证**。

## 6. 本讲小结

- TRS 采用**两平面模型**：数据面（SQ/CQ + doorbell，高频、零 ioctl）与控制面（ioctl + mailbox，低频、需固件参与）。
- 用户态门面 `trs_interface.c` 统一遵循「参数校验 → 按连接形态（PCIE/HCCS/RC vs UB）分发」套路；UB 路径用弱符号实现编译期解耦。
- SQ/CQ 在分配阶段经 mmap 把设备内存映射到用户态，下发任务变成「写 SQE 环形缓冲 + 写 doorbell 寄存器」，全程不陷内核。
- 环形队列用「保留一槽」的信用算法区分满/空；下发路径用 `smp_wmb()`/`wmb()` 两道内存屏障保证 SQE 先于 doorbell 对设备可见。
- 内核模块 `trsdrv.ko` 用「子模块表 + 失败逆序回滚」装配；软 SQ 由内核 kthread 周期调度，doorbell 携带 sqid 实现「快发」。
- 控制面资源管理经 ioctl 进入内核，组装成带 `0x5A5A` 魔数的 mailbox 报文送达设备固件（TSCPU）。

## 7. 下一步学习建议

- **任务调度代理**：本讲的「软 SQ 调度线程」是设备级的，下一讲 [u6-l4 TS Agent 任务调度代理与 esched 事件调度](u6-l4-ts-agent-and-esched.md) 会讲 `ts_agent`（虚拟 SQ 工作器）与 `esched` 事件调度，它们与本讲的 `trs_cb_event`（用了 `halEschedWaitEvent`）直接相关，建议紧接着读。
- **故障管理**：任务下发失败、设备超时等异常会进入 FMS，可继续读 [u6-l5 FMS 故障管理系统与 soft_fault 软故障处理](u6-l5-fms-and-soft-fault.md)。
- **源码延伸**：想深入 UB 形态的数据面，可读 `trs_sqcq_urma_alloc`/`trs_sq_task_send_urma` 的强符号实现（在 `src/ascend_hal/trs/core/urma/` 与 `src/ascend_hal/comm/ascend_urma_adapt/`，结合 [u3-l5](u3-l5-comm-urma-queryfeature.md) 的 urma 知识）；想深入 mailbox 的硬件通道，可读 `src/sdk_driver/trsdrv/trs/lba/comm/adapt/trs_mbox.c` 与 `trs_host_soft_mbox.c`。
