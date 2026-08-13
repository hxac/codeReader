# PBL 基础库：UDA 统一设备接入

## 1. 本讲目标

本讲聚焦 HAL 层（`libascend_hal.so`）内部的「基础公共库」PBL，并深入其中最底层的一个子模块——**UDA（Unified Device Access，统一设备接入）**。学完本讲，你应当能够：

1. 说清楚 **PBL** 在 HAL 层中的定位，以及它为上层（DSMI、HDC、SVM、TRS、DMS 等）提供了哪些公共基础设施。
2. 解释 UDA 解决的核心问题：**为什么昇腾驱动里同一个 NPU 会有好几种「设备号」**，以及 UDA 如何在它们之间做翻译。
3. 读懂 UDA 的两层结构：`uda_user.c`（公共 API 门面）与 `uda_user_kernel_api.c`（ioctl 通信 + 设备表管理），并理解懒初始化、双检锁、fork 安全、errno 映射等工程细节。
4. 写出 UDA 在主机-设备通信链路中扮演的角色——它不直接收发消息，但它是所有上层模块「找到正确设备」的前提。

## 2. 前置知识

在进入 UDA 之前，先回顾两个关键概念（详见 [u3-l1](u3-l1-hal-overview-and-api.md) 与 [u3-l2](u3-l2-hdc-communication.md)）：

- **用户态 / 内核态与 ioctl**：HAL 是用户态动态库，内核驱动是 `.ko`。用户态要操作硬件，必须通过 `ioctl` 「陷入」内核。本讲会看到大量 `ioctl` 调用。
- **drvError_t 错误码**：HAL 层统一返回 `drvError_t`，`DRV_ERROR_NONE == 0` 表示成功，其余为各类 `DRV_ERROR_*`。

再补充两个本讲要用到的新术语：

- **字符设备（character device）**：Linux 里一类以字节流访问的设备节点，形如 `/dev/xxx`，应用通过 `open/ioctl/close` 操作它。昇腾驱动在内核态注册了多个字符设备作为「用户态入口」。
- **设备号（devid）的多命名空间**：这是本讲的灵魂，下面 4.1 会展开。简单说，应用看到的「第 0 号卡」、内核看到的「物理卡号」、跨主机唯一的「全局设备号」往往不是同一个数字。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/ascend_hal/pbl/uda/uda_user.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user.c) | **公共 API 门面层**。把对外头文件 `ascend_hal_base.h` 里声明的 `drvGetDevNum`/`drvGetDevIDs`/`halGetDevNumEx` 等符号，转调到内部实现。极薄。 |
| [src/ascend_hal/pbl/uda/uda_user_kernel_api.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c) | **UDA 的实现核心**。负责字符设备打开/关闭、ioctl 收发、懒初始化、设备表填充，以及所有查询/翻译函数。 |
| [src/sdk_driver/pbl/uda/command/ioctl/uda_cmd.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/uda/command/ioctl/uda_cmd.h) | **用户态与内核态共享的命令/结构定义**。定义 UDA 的 ioctl 命令号与 `uda_user_info`/`uda_logic_dev` 等数据结构。注意它在 `sdk_driver`（内核侧）目录下，被两侧共用。 |
| [src/ascend_hal/inc/davinci_interface.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/davinci_interface.h) | **「设备管家」字符设备 `/dev/davinci_manager` 的接口定义**。UDA 通过它打开自己的会话。 |
| [src/ascend_hal/pbl/uda/dc/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/dc/CMakeLists.txt) | UDA 的编译配置，揭示关键特性宏（如 `CFG_FEATURE_UDA_CONSTRUCT_INIT`）。 |

一个全局规律（承接 u1-l3）：UDA 的命令定义放在内核侧 `sdk_driver/pbl/uda/` 下，而实现放在用户侧 `ascend_hal/pbl/uda/` 下——这正是「HAL 与 SDK 目录大量镜像、靠 ioctl 跨态通信」的又一个实例。

## 4. 核心概念与源码讲解

### 4.1 PBL 公共基础库与 UDA 的定位

#### 4.1.1 概念说明

**PBL（Public Base Lib，公共基础库）** 是 `src/ascend_hal/pbl/` 下的一组「地基」模块，HAL 层几乎所有上层子模块都要依赖它。它不面向最终用户，而是给驱动内部用的公共能力：

```
src/ascend_hal/pbl/
├── uda/          # 统一设备接入（本讲）
├── urd/          # 用户请求转发（User Request Distribute，下一讲 u3-l4）
├── commlib/      # 公共函数库（drv_comm_intf、drv_error_map、atomic_lock 等）
├── queryfeature/ # 软件特性查询，做多芯片兼容适配
└── ubmm/         # UB（灵衢超节点）内存管理
```

其中 **UDA（Unified Device Access，统一设备接入）** 解决一个非常具体的问题：**「一个 NPU 到底是几号？」**

在昇腾系统里，同一个物理 NPU 在不同视角下有不同编号：

| ID 名称 | 含义 | 典型值/范围 |
| --- | --- | --- |
| **逻辑 devid（logic devid）** | 应用看到的 0 基连续下标，如 `aclrtSetDevice(0)` 的 `0` | host 侧 `0..max_dev_num`（host 上限 100，device 上限 32） |
| **物理 devid（phy_devid）** | 主机分配给该卡的物理编号 | 由内核分配 |
| **唯一 devid（udevid，unique devid）** | 跨主机/设备全局唯一的设备号 | host 上限 1124，device 上限 64 |
| **虚拟 devid（vdev）** | 算力切分（vdavinci）产生的虚拟设备 | 仅 admin 可见 |

> 为什么要有这么多 ID？因为应用只关心「我用第几张卡」（逻辑号），但内核和跨节点通信必须用**全局唯一**的身份（udevid）来定位真正的硬件，而管理面又需要知道**物理槽位**（phy_devid）。UDA 就是这三套（乃至四套）命名空间之间的「翻译官」和「电话簿」。

#### 4.1.2 核心流程

UDA 对外提供两大类能力，整体流程如下：

```
应用/Runtime 调用 hal 接口（传入逻辑 devid）
            │
            ▼
   ┌────────────────────────────────┐
   │  uda_user.c  公共 API 门面       │   drvGetDevNum / drvGetDevIDs / halGetDevNumEx ...
   └────────────────────────────────┘
            │  转调
            ▼
   ┌────────────────────────────────┐
   │  uda_user_kernel_api.c 实现     │
   │  ┌──────────┐   ┌────────────┐ │
   │  │ 设备枚举  │   │ ID 翻译     │ │   读本地 logic_dev 表 / 必要时 ioctl 内核
   │  └──────────┘   └────────────┘ │
   └────────────────────────────────┘
            │  ioctl（懒打开 /dev/davinci_manager）
            ▼
   ┌────────────────────────────────┐
   │  内核态 uda 模块（sdk_driver）   │   持有真正的设备表，回填数据
   └────────────────────────────────┘
```

- **设备枚举**：「当前进程能看到几张卡？分别是哪几张？」——`drvGetDevNum` / `drvGetDevIDs`。
- **ID 翻译**：「逻辑 0 号卡对应的物理号/全局唯一号是多少？」——`drvDeviceGetPhyIdByIndex` 等。

关键设计：UDA 在用户态**缓存**一份设备表（`logic_dev[]`），绝大多数查询只读缓存、不进内核；只有少数动态变化的设备（如可热插拔的 mia 设备）才实时 ioctl 内核。

#### 4.1.3 源码精读

UDA 的 ioctl 命令与数据结构定义在用户/内核共享的 `uda_cmd.h` 中。先看两个核心数据结构：

[uda_cmd.h:11-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/uda/command/ioctl/uda_cmd.h#L11-L17) 定义了用户环境信息——UDA 初始化时从内核取回，决定「我是谁、能看到多少设备」：

```c
struct uda_user_info {
    unsigned int admin_flag;        /* 1: admin 权限，可管理 mia 设备；0: 跑在 docker 里，无 admin 权限 */
    unsigned int local_flag;        /* 1: 本地设备；0: 非本地 */
    unsigned int max_dev_num;       /* 最大逻辑设备数，host=100，device=32 */
    unsigned int max_udev_num;      /* admin 有效，最大唯一设备数，host=1124，device=64 */
    unsigned int support_udev_mng;  /* obp 不支持，milan 支持 */
};
```

[uda_cmd.h:23-30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/uda/command/ioctl/uda_cmd.h#L23-L30) 则是每张卡的「身份证」，三套 ID 全在里面：

```c
struct uda_logic_dev {
    unsigned char valid : 1;     /* 该表项是否有效 */
    unsigned char hw_type : 7;   /* 硬件类型：UDA_HW_DAVINCI=0 / UDA_HW_KUNPENG=1 */
    unsigned char sub_devid;     /* 子设备号（mia 多实例用） */
    unsigned short phy_devid;    /* 物理 devid */
    unsigned short devid;        /* 逻辑 devid */
    unsigned short udevid;       /* 唯一 devid */
};
```

设备类型常量见 [uda_cmd.h:8-9](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/uda/command/ioctl/uda_cmd.h#L8-L9)（`UDA_HW_DAVINCI=0` 是昇腾达芬奇 NPU，`UDA_HW_KUNPENG=1` 是鲲鹏）。理解了这两张表，后面所有函数都只是在读写它们。

#### 4.1.4 代码实践

1. **实践目标**：建立对 UDA 数据模型的直观认识。
2. **操作步骤**：打开 `uda_cmd.h`，对照上面的表，在 `struct uda_logic_dev` 旁批注每个字段的中文含义；再数一下 `uda_cmd.h` 里一共定义了几个 `UDA_*` ioctl 命令。
3. **需要观察的现象**：你会看到命令号用 `_IOR/_IOW/_IOWR('U', n, 类型)` 宏生成，其中 `'U'` 是 UDA 的 magic number，`n` 是序号。
4. **预期结果**：共 6 个基础命令（`UDA_GET_USER_INFO` ~ `UDA_RUDEVID_TO_LUDEVID`，序号 0~6）；带读写的翻译类命令用 `_IOWR`，纯读用 `_IOR`。
5. 运行结果：待本地验证（本实践为源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `max_dev_num`（host 上限 100）远小于 `max_udev_num`（host 上限 1124）？

> **答案**：`max_dev_num` 是**单个进程/容器视角下**的逻辑设备下标上限（一个进程一般用不了上百张卡）；`max_udev_num` 是**整个集群**的全局唯一设备号空间，要容纳多主机 × 多卡 × 虚拟设备，所以大得多。两者刻画的是「局部下标」与「全局唯一编号」两个不同尺度。

**练习 2**：`uda_logic_dev` 里 `devid` 和 `udevid` 分别对应应用视角和全局视角，那么 `phy_devid` 主要给谁用？

> **答案**：主要给**管理面/带外通道**用——例如定位物理槽位、做 PCIe 复位（见 u2-l4 的带外复位）、向 BMC 报告具体哪一张物理卡。它描述的是「硬件在机箱里的真实身份」。

---

### 4.2 uda_user：对外公共 API 门面层

#### 4.2.1 概念说明

`uda_user.c` 是 UDA 对外的**门面（facade）**。它只有 77 行，做的事非常单一：把对外头文件 `pkg_inc/ascend_hal_base.h` 中以 `DLLEXPORT` 声明的公共符号，逐一转调到 `uda_user_kernel_api.c` 里的内部实现。

为什么要单独分一层这么薄的文件？因为**声明（头文件）与实现分离**是 HAL 的纪律：对外接口名（`drvGetDevNum` 等）是稳定的契约，写进 `pkg_inc/`；而实现可以自由演化甚至重命名（`uda_user_get_dev_num`）。门面层就是这个「稳定名字 → 可变实现」的转接头。

#### 4.2.2 核心流程

```
外部调用 drvGetDevNum(&n)
        │  （uda_user.c 里只有一行：return uda_user_get_dev_num(devNum);）
        ▼
uda_user_kernel_api.c::uda_user_get_dev_num()  ← 真正干活
```

#### 4.2.3 源码精读

[uda_user.c:14-32](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user.c#L14-L32) 是一组典型的「一对一转发」，每个公共函数体只有一行 return：

```c
drvError_t drvGetDevNum(uint32_t *devNum)         { return uda_user_get_dev_num(devNum); }
drvError_t drvGetDevIDs(uint32_t *devices, uint32_t len) { return uda_user_get_dev_ids(devices, len); }
drvError_t halGetDevNumEx(uint32_t hw_type, uint32_t *devNum) { return uda_user_get_dev_num_ex(hw_type, devNum); }
drvError_t halGetDevIDsEx(uint32_t hw_type, uint32_t *devices, uint32_t len) { ... }
```

注意命名上的对应规律，这对你在源码里「按图索骥」很重要：

| 公共符号（`ascend_hal_base.h` 声明） | 门面转发目标 | 用途 |
| --- | --- | --- |
| `drvGetDevNum` | `uda_user_get_dev_num` | 查逻辑设备数量 |
| `drvGetDevIDs` | `uda_user_get_dev_ids` | 查逻辑设备 ID 列表 |
| `halGetDevNumEx` | `uda_user_get_dev_num_ex` | 按 `hw_type` 查数量 |
| `halGetVdevNum` / `halGetVdevIDs` | `uda_user_get_vdev_num/ids` | 虚拟设备枚举（admin 专用） |
| `drvDeviceGetPhyIdByIndex` | `uda_user_get_phy_id_by_index` | 逻辑号→物理/唯一号 |
| `drvDeviceGetIndexByPhyId` | `uda_user_get_index_by_phy_id` | 反向翻译 |
| `drvGetDeviceLocalIDs` | `uda_user_get_device_local_ids` | 设备侧本地 ID |
| `halGetHostID` | `uda_user_get_host_id` | 取主机 ID |

这些公共符号的声明可在 `ascend_hal_base.h` 中找到，例如 [ascend_hal_base.h:946](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L946) 的 `drvGetDevNum`、[ascend_hal_base.h:974](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L974) 的 `drvGetDevIDs`、[ascend_hal_base.h:5648](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L5648) 的 `halGetDevNumEx`、[ascend_hal_base.h:5638](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L5638) 的 `halGetVdevNum`。

> 小提示：`uda_user.c` 顶部 `#include "ascend_hal.h"`（[uda_user.c:11](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user.c#L11)），正是 u3-l1 讲过的那个聚合头，由此拿到所有公共声明与 `drvError_t`。

#### 4.2.4 代码实践

1. **实践目标**：练习「声明 → 门面 → 实现」的三点定位。
2. **操作步骤**：任选一个公共符号，如 `halGetVdevNum`。先在 `pkg_inc/ascend_hal_base.h:5638` 看声明；再到 `uda_user.c` 看转发；最后跳进 `uda_user_kernel_api.c` 看实现。
3. **需要观察的现象**：声明处的 Doxygen 注释（`@brief`/`@param`/`@return`）就是这份函数的「合同」，门面和实现都不改它的语义。
4. **预期结果**：三层一一对应，名字只差前缀（`hal`/`drv` ↔ `uda_user_`）。
5. 运行结果：待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：如果有一天内部实现函数 `uda_user_get_dev_num` 被重命名为 `uda_get_logic_dev_count`，需要改动哪些地方？对外调用方（比如 Runtime）需要改吗？

> **答案**：只需改 `uda_user.c` 里那一行转发和 `uda_user.h` 的声明；对外调用方**完全不用改**，因为它调的是稳定的 `drvGetDevNum`。这正是门面层隔离变化的价值。

**练习 2**：门面层的函数都直接 `return uda_user_get_xxx(...)`，没有任何参数校验。校验逻辑放在哪一层？为什么？

> **答案**：放在 `uda_user_kernel_api.c` 的实现层（如 `uda_user_get_dev_num` 里有 `if (devNum == NULL)` 校验）。这样校验逻辑集中在一处，避免重复；门面层保持极薄，职责单一。

---

### 4.3 uda_user_kernel_api：ioctl 通信底座与懒初始化

#### 4.3.1 概念说明

`uda_user_kernel_api.c` 是 UDA 真正干活的文件。它要回答两个问题：

1. **怎么进内核？** —— 通过打开一个字符设备 `/dev/davinci_manager`，再用 `ioctl` 下发 UDA 命令。
2. **什么时候进内核、进几次？** —— 采用**懒初始化（lazy init）**：第一次被调用时才探测设备、建立设备表，之后只读缓存。

这里有几个值得学习的工程手法：**双检锁（double-checked locking）**保证线程安全且只初始化一次；**fork 安全**（用 pid 判断是否在子进程）保证 fork 后能重新打开 fd；**errno → drvError_t 映射**把 Linux 系统错误统一成驱动的错误码。

#### 4.3.2 核心流程

UDA 初始化与通信的整体时序：

```
① 库加载时（constructor，若开启 CFG_FEATURE_UDA_CONSTRUCT_INIT）
   或 ② 首次调用任意 uda_user_get_* 时
        │  uda_init() → 双检锁 → _uda_init()
        ▼
   _uda_init():
     a. uda_get_user_info()        ── ioctl UDA_GET_USER_INFO ──▶ 填 user_info（我是谁）
     b. 非 admin：uda_dev_access() ── 探测 /dev/davinci0..N 可见几张
     c. malloc logic_dev[]         ── 用户态设备表
     d. uda_init_dev_table()       ── ioctl UDA_SETUP_DEV_TABLE ──▶ 告诉内核数量
     e. uda_get_dev_list()         ── ioctl UDA_GET_DEV_LIST ──▶ 内核回填 logic_dev[]
     f. 按 hw_type 统计 davinci / kunpeng 数量
        │
        ▼  之后所有查询只读 logic_dev[] 缓存
   后续 uda_cmd_ioctl(cmd, para):
     - 锁内懒打开（fd<0 或 pid 变了就 uda_char_dev_open）
     - ioctl + EINTR 重试
     - errno → drvError_t 映射
```

#### 4.3.3 源码精读

**(1) 字符设备打开与关闭。** UDA 不直接操作 `/dev/davinci0` 这类「业务设备节点」，而是先在「设备管家」`/dev/davinci_manager` 上打开一个名为 `"uda"` 的会话：

[uda_user_kernel_api.c:122-151](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L122-L151) —— 打开 manager 并 ioctl 注册 uda 模块，拿到一个 fd：

```c
uda_dev_fd = uda_file_open(davinci_intf_get_dev_path(), O_RDWR | O_CLOEXEC);  // 打开 /dev/davinci_manager
...
ret = uda_ioctl(uda_dev_fd, DAVINCI_INTF_IOCTL_OPEN, &arg);  // 注册 "uda" 模块会话
...
uda_cur_pid = getpid();  // 记录打开者 pid，供 fork 检测
```

`davinci_intf_get_dev_path()` 在 host 侧固定返回 `/dev/davinci_manager`（见 [davinci_interface.h:64-75](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/davinci_interface.h#L64-L75)），`DAVINCI_INTF_IOCTL_OPEN/CLOSE` 定义在 [davinci_interface.h:27-28](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/davinci_interface.h#L27-L28)。关闭逻辑对称，见 [uda_user_kernel_api.c:153-174](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L153-L174)。

**(2) 统一的 ioctl 收发器。** 这是整个 UDA 用户态的「咽喉」：

[uda_user_kernel_api.c:176-209](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L176-L209) —— 集中体现三大工程手法：

```c
static int uda_cmd_ioctl(unsigned long cmd, void *para) {
    (void)pthread_mutex_lock(&uda_fd_mutex);
    if (uda_dev_fd < 0 || uda_cur_pid != getpid()) {   // ① fork 安全 + 懒打开
        ret = uda_char_dev_open();
        ...
    }
    (void)pthread_mutex_unlock(&uda_fd_mutex);

    do {
        ret = uda_ioctl(uda_dev_fd, cmd, para);        // ② EINTR 自动重试
    } while ((ret == -1) && (errno == EINTR));

    if (ret < 0) {                                      // ③ errno → drvError_t
        if (errno == EBUSY)        ret = DRV_ERROR_RESOURCE_OCCUPIED;
        else if (errno == ENODEV)  ret = DRV_ERROR_NO_DEVICE;
        else if (errno == ENOMEM)  ret = DRV_ERROR_OUT_OF_MEMORY;
        else                       ret = DRV_ERROR_IOCRL_FAIL;
        ...
    }
    return DRV_ERROR_NONE;
}
```

- **fork 安全**：子进程继承了父进程的 fd，但内核会话不一定还有效，所以检测 `uda_cur_pid != getpid()` 就重新打开。
- **EINTR 重试**：`ioctl` 可能被信号打断返回 `EINTR`，需重试。
- **错误码映射**：把 POSIX 的 `errno` 翻译成 u3-l1 讲过的统一 `drvError_t`。

**(3) 懒初始化 + 双检锁。** 每个公共函数开头都先调 `uda_init()`：

[uda_user_kernel_api.c:324-343](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L324-L343) —— 经典双检锁，保证多线程下只初始化一次：

```c
static int uda_init(void) {
    static pthread_mutex_t init_mutex = PTHREAD_MUTEX_INITIALIZER;
    if (uda_is_init) { return 0; }            // 第一次检查（无锁，快路径）
    (void)pthread_mutex_lock(&init_mutex);
    if (!uda_is_init) {                        // 第二次检查（持锁）
        ret = _uda_init();
        if (ret == 0) { uda_is_init = true; }
    }
    (void)pthread_mutex_unlock(&init_mutex);
    return ret;
}
```

真正干活的 `_uda_init()` 见 [uda_user_kernel_api.c:264-322](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L264-L322)：取 `user_info` → 探测可见设备 → 建表 → 让内核回填 → 统计数量。

**(4) 库加载即初始化。** UDA 还注册了一个 constructor，在 `libascend_hal.so` 被加载时就提前建表：

[uda_user_kernel_api.c:359-364](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L359-L364)：

```c
static void __attribute__((constructor)) uda_user_init(void) {
#ifdef CFG_FEATURE_UDA_CONSTRUCT_INIT
    (void)uda_init();
#endif
}
```

这个宏默认开启，见编译配置 [CMakeLists.txt:34](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/dc/CMakeLists.txt#L34)（`CFG_FEATURE_UDA_CONSTRUCT_INIT`）。因此即便 constructor 没跑（比如被关闭），每个公共函数开头的 `uda_init()` 也能兜底——双重保险。

**(5) 非 admin 的可见性探测。** 注意 `_uda_init` 里对 admin 与非 admin 的区别对待：

[uda_user_kernel_api.c:275-282](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L275-L282) —— 非 admin（如 docker 容器）要实地探测 `/dev/davinci*` 设备节点能看到几张（受 cgroup 限制）；admin 则直接信任内核给的 `max_dev_num`。探测函数 `uda_dev_access` 见 [uda_user_kernel_api.c:85-120](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L85-L120)，它用 `access + open` 逐个尝试 `/dev/davinci0..`，体现了 UDA「屏蔽底层访问差异」的一环：上层只问「有几张」，UDA 自己去算。

#### 4.3.4 代码实践

1. **实践目标**：理解 ioctl 收发与错误码映射。
2. **操作步骤**：在 `uda_cmd_ioctl` 里，假设某次 `ioctl` 因设备被拔出而返回 `-1` 且 `errno == ENODEV`。手动追踪代码路径，确认最终返回值。
3. **需要观察的现象**：函数会走 `errno == ENODEV` 分支，返回 `DRV_ERROR_NO_DEVICE`，并调用 `share_log_read_err(HAL_MODULE_TYPE_DEV_MANAGER)` 打一条错误日志。
4. **预期结果**：上层调用方拿到 `DRV_ERROR_NO_DEVICE`（非 0），据此判断「设备不存在」。
5. 运行结果：待本地验证（可通过在测试环境拔卡/卸载驱动模拟，属源码阅读+推理型实践）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `uda_cmd_ioctl` 要在持锁区间内判断 `uda_cur_pid != getpid()`？如果不判断，fork 出的子进程会出什么问题？

> **答案**：子进程通过 fork 继承了父进程的 `uda_dev_fd`，但内核侧的 "uda" 会话是绑定在父进程 pid 上的，子进程直接用这个 fd 下 ioctl 可能失效或命中错误会话。重新 `uda_char_dev_open()` 让子进程拿到属于自己的合法会话 fd。这是一种典型的「fork 安全」处理。

**练习 2**：双检锁里，第一次 `if (uda_is_init) return 0;` 在锁外读取一个会被多线程修改的布尔值，为什么这里可以接受？

> **答案**：`uda_is_init` 一旦从 false 变 true 就再不会回退（除非显式 `uda_uninit`），且这里只是个「快路径优化」——即便某个线程读到稍旧的 false，最多多走一次锁内第二次检查，不会产生错误结果。这是双检锁的标准用法，正确性由锁内第二次检查与 `uda_is_init` 的单调性共同保证。

---

### 4.4 设备枚举与多 ID 空间翻译：UDA 在 HDC 链路中的角色

#### 4.4.1 概念说明

本节把 4.1 的「翻译官」落到实处。UDA 的查询/翻译函数可分为三组：

1. **枚举组**：`uda_user_get_dev_num` / `get_dev_ids` / `get_dev_num_ex` / `get_vdev_num` —— 回答「有几张、是哪几张」。
2. **本地翻译组**：`uda_get_phy_devid_by_devid`、`uda_get_udevid_by_devid` 等 —— 直接读 `logic_dev[]` 缓存，O(1) 或 O(n) 查表，不进内核。
3. **远程/动态翻译组**：`uda_trans_devid` —— 走 ioctl 实时问内核，用于可能动态变化的设备（mia 设备可热插拔）。

**UDA 与 HDC 的关系（重点）**：UDA **不直接**做 HDC 消息收发（那是 u3-l2 的 hdc_* 干的），但它是 HDC 通信能「找对人」的前提。上层模块（SVM、DSMI、DMS、queue、esched 等）在向某张卡发起 HDC 会话之前，必须先用 UDA 把「应用给的逻辑 devid」翻译成「内核/设备认识的全局唯一 udevid」，再拿 udevid 去打开/路由对应的设备通道。换句话说：**HDC 负责「怎么把消息送过去」，UDA 负责「送给哪台设备的谁」**。

> 与 u2-l2/u2-l3 提到的 `drvGetDevInfo` 区分一下：`drvGetDevInfo` 走 DSMI 的 `DEVDRV_MANAGER_GET_DEVINFO` ioctl，返回的是某张卡的**详细运行信息**（温度、健康态等）；而 UDA 的 `drvGetDevNum`/`drvGetDevIDs`/ID 翻译管的是**枚举与命名**。两者互补，别混淆。

#### 4.4.2 核心流程

以「应用要操作逻辑 0 号卡」为例，UDA 在链路中的位置：

```
应用: aclrtSetDevice(0)            ← 逻辑 devid = 0
        │
        ▼ （上层模块调用 UDA 翻译）
UDA: uda_get_udevid_by_devid(0, &udevid)   ← 查 logic_dev[0].udevid，得 udevid
        │
        ▼ （上层模块用 udevid 路由）
HDC/SVM: 用 udevid 定位设备 → 建立/复用 HDC 会话 → ioctl 陷入内核 → 到达目标 NPU
```

#### 4.4.3 源码精读

**(1) 枚举函数：只读缓存。** 以查设备数量为例：

[uda_user_kernel_api.c:384-399](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L384-L399) —— 校验 + 懒初始化后，直接返回初始化时统计好的 `uda_dev_num_davinci`，**完全不再进内核**：

```c
int uda_user_get_dev_num(uint32_t *devNum) {
    int ret = uda_init();           // 懒初始化（已初始化则立即返回）
    if (ret != 0) { return ret; }
    if (devNum == NULL) { ...; return DRV_ERROR_INVALID_VALUE; }
    *devNum = uda_dev_num_davinci;  // 读缓存
    return DRV_ERROR_NONE;
}
```

查 ID 列表的 `uda_get_dev_IDs`（[uda_user_kernel_api.c:371-382](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L371-L382)）则是遍历 `logic_dev[]`，把 `valid==1 && hw_type==DAVINCI` 的 `devid` 收集到输出数组。按 `hw_type` 区分达芬奇/鲲鹏的版本见 [uda_user_kernel_api.c:420-443](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L420-L443)。

**(2) 本地翻译：O(1) 直查表。** 逻辑号→物理号最直接的实现：

[uda_user_kernel_api.c:528-537](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L528-L537)：

```c
STATIC int uda_get_phy_devid_by_devid(uint32_t devid, uint32_t *phy_devid) {
    if (logic_dev[devid].valid == 0) { ...; return DRV_ERROR_INVALID_VALUE; }
    *phy_devid = logic_dev[devid].phy_devid;   // 直接按下标取
    return DRV_ERROR_NONE;
}
```

反向（物理号→逻辑号）则需遍历，见 [uda_user_kernel_api.c:539-552](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L539-L552)。

**(3) 带形态判断的翻译：milan vs obp。** `uda_user_get_phy_id_by_index` 展示了 UDA 屏蔽「平台差异」的能力：

[uda_user_kernel_api.c:624-649](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L624-L649)：

```c
/* milan return udevid, obp return phy devid */
if (uda_is_support_udev_mng()) {        // milan 平台
    if (devid < user_info.max_dev_num) {
        return uda_get_udevid_by_devid(devid, phyId);   // 普通卡：读缓存
    } else {
        return uda_trans_devid(UDA_DEVID_TO_UDEVID, devid, phyId); // mia 卡：实时 ioctl
    }
} else {                                // obp 平台
    return uda_get_phy_devid_by_devid(devid, phyId);
}
```

- `uda_is_support_udev_mng()` 依据 `user_info.support_udev_mng` 判断当前是 milan 还是 obp 平台（见 [uda_user_kernel_api.c:75-78](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L75-L78)）。
- 同一个公共接口，**上层完全不用关心平台差异**，UDA 内部分支处理——这就是「统一设备接入」中「统一」二字的落点。
- 对 mia（多实例/可热插拔）设备，注释明说「可能被增删，管理进程需实时向内核查询」，所以走 `uda_trans_devid` 的 ioctl 路径（[uda_user_kernel_api.c:234-249](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L234-L249)）。

**(4) UDA 在 HDC 链路中的真实落点。** 上层模块调用 UDA 翻译的实例，SVM 是典型：

[svm_master_init.c:103-117](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L103-L117) —— SVM 在关闭某设备前，先把逻辑 devid 翻译成 udevid：

```c
int svm_device_close(u32 devid) {
    u32 udevid;
    ...
    ret = uda_get_udevid_by_devid(devid, &udevid);  // 逻辑号 → 全局唯一号
    if (ret != DRV_ERROR_NONE) { ...; return DRV_ERROR_PARA_ERROR; }
    ... // 用 devid/udevid 继续后续设备关闭（涉及 HDC 通道）
}
```

这正是「UDA 把逻辑号翻译成设备能识别的唯一身份， HDC 再据此通信」的协作模式。类似的调用还散布在 `dsmi_common_interface.c`、`trs_dev_drv.c`、`event_sched.c`、`devdrv_manager_*.c` 等众多模块中——它们都依赖 UDA 来完成「定位设备」这一步。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：摸清 UDA 提供的「打开/关闭/信息获取」类函数，并用自己的话写出 UDA 在 HDC 通信链路中的角色。
2. **操作步骤**：
   - 在 `uda_user_kernel_api.c` 中搜索 `uda_char_dev_open` / `uda_char_dev_close` / `uda_get_user_info` / `uda_dev_access`，把它们归入「设备打开」「设备关闭」「信息获取」三类，各写一句话说明。
   - 再搜索本文件中所有 `uda_user_get_*` 与 `uda_get_*_by_*` 函数，按「枚举」「本地翻译」「远程翻译」三组分类。
   - 阅读 [svm_master_init.c:113](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/api/master/svm_master_init.c#L113) 的 `uda_get_udevid_by_devid` 调用点，画出「应用逻辑 devid → UDA 翻译成 udevid → SVM/HDC 据此路由到目标 NPU」的时序草图。
3. **需要观察的现象**：UDA 自身没有任何 `hdc_*` 调用（可用 `grep -n "hdc" uda_user_kernel_api.c` 验证应为空），它只产出「正确的设备身份」；真正发消息是上层模块拿到身份后用 HDC 完成的。
4. **预期结果**：你应当能得出结论——**UDA 是 HDC 通信的「寻址层 / 电话簿」**：它不搬运消息，但解决了「消息该送给哪台设备」的命名与翻译问题。没有 UDA，上层模块无法把应用给的逻辑号映射到内核/全局认识的设备身份。
5. 运行结果：待本地验证（属源码阅读 + 调用链追踪型实践；如需运行，可参考 u8-l2 用 UT 框架给 `uda_user_get_dev_num` 写一个最小用例，断言返回值与缓存一致）。

#### 4.4.5 小练习与答案

**练习 1**：`uda_user_get_dev_num` 里直接返回 `uda_dev_num_davinci`，那这个值是什么时候、怎么算出来的？

> **答案**：在 `_uda_init()` 末尾由 `uda_get_dev_num_from_dev_list(logic_dev, max_dev_num, UDA_HW_DAVINCI)` 统计得到（见 [uda_user_kernel_api.c:309](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L309)）。即遍历内核回填的 `logic_dev[]`，数出 `valid==1 && hw_type==UDA_HW_DAVINCI` 的条目数。之后查询只读这个缓存值。

**练习 2**：同样是「逻辑号→物理/唯一号」，为什么普通卡走读缓存，而 mia 卡要走 ioctl？

> **答案**：普通卡在系统启动后基本固定，缓存即可；mia（多实例）设备支持运行中增删（热插拔/动态切分），缓存会过期，所以管理进程需要「实时向内核查询」（代码注释原话）。这是「性能（读缓存）」与「正确性（实时查）」的权衡。

**练习 3**：如果 HDC 是「送消息的快递员」，UDA 是什么角色？用一个类比概括二者关系。

> **答案**：UDA 是「地址簿 / 收件人查询台」。快递员（HDC）能力强但只管送，它需要先到查询台（UDA）确认「逻辑 0 号」对应的真实收件地址（udevid/物理设备），才能把包裹准确送达对应 NPU。

---

## 5. 综合实践

把本讲知识串起来，完成一次「UDA 全链路追踪」：

**任务**：以「一个 docker 容器里的应用调用 `drvGetDevNum` / `drvGetDevIDs` 查询可用 NPU」为场景，画出从应用到内核的完整时序，并标注每一步发生在哪个文件。

**建议步骤**：

1. 从公共入口 `drvGetDevNum`（[uda_user.c:14-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user.c#L14-L17)）出发，标注「门面转发」。
2. 进入 `uda_user_get_dev_num`（[uda_user_kernel_api.c:384](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L384)），标注「懒初始化 + 读缓存」。
3. 回溯初始化路径 `_uda_init`（[uda_user_kernel_api.c:264](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/uda/uda_user_kernel_api.c#L264)），因为是 docker（非 admin），标出 `uda_dev_access` 探测 `/dev/davinci*` 的步骤。
4. 标出三个 ioctl 节点：`UDA_GET_USER_INFO` / `UDA_SETUP_DEV_TABLE` / `UDA_GET_DEV_LIST`，它们都经 `uda_cmd_ioctl` → `/dev/davinci_manager` 进入内核。
5. 最后在图上单独画一条「上层模块用 UDA 翻译出的 udevid 去 HDC 建立会话」的虚线，体现 UDA 与 HDC 的分工。

**验收标准**：你的时序图应当能回答——（a）UDA 何时进内核、何时只读缓存？（b）admin 与 docker 的初始化路径有何不同？（c）UDA 产出什么给 HDC 用？

## 6. 本讲小结

- **PBL** 是 HAL 层的公共基础库（`uda/urd/commlib/queryfeature/ubmm`），UDA 是其中的「统一设备接入」模块。
- UDA 的核心使命是解决**设备号多命名空间**问题：在逻辑 devid、物理 phy_devid、全局唯一 udevid、虚拟 vdev 之间做**枚举与翻译**。
- 代码分两层：`uda_user.c` 是极薄的**公共 API 门面**（对接 `ascend_hal_base.h` 的 `drvGetDevNum` 等）；`uda_user_kernel_api.c` 是**实现核心**，含字符设备打开、ioctl 收发、懒初始化。
- UDA 经 `/dev/davinci_manager` 字符设备 + `UDA_*` ioctl 与内核侧交换设备表，命令与结构定义在用户/内核共享的 `uda_cmd.h`。
- 工程亮点：**双检锁懒初始化**、**fork 安全（pid 检测）**、**EINTR 重试**、**errno→drvError_t 映射**、对 admin/docker 与 milan/obp 的**差异屏蔽**。
- **UDA 是 HDC 通信的「寻址层」**：它不收发消息，但把应用给的逻辑号翻译成设备认识的全局唯一身份，上层模块（SVM/DSMI/DMS 等）再据此用 HDC 路由到正确 NPU。

## 7. 下一步学习建议

- 下一讲 **[u3-l4 PBL：URD 请求转发与 commlib 公共函数](u3-l4-pbl-urd-commlib.md)** 会继续在 PBL 内深入，讲 URD 如何转发用户请求，以及 `commlib` 里的 `drv_error_map`、`atomic_lock` 等基础设施——与本讲的错误码映射、互斥锁一脉相承。
- 若想立刻验证「UDA 产出身份、HDC 负责通信」的协作，建议回看 **u3-l2** 的 HDC client/server/core 模型，对照本讲 4.4 的 SVM 调用点，把两层拼起来理解。
- 后续 **u4（SVM）** 与 **u6（TRS/SDK-driver）** 会大量调用 UDA 的翻译接口，到时可回来印证本讲的「寻址层」定位。
