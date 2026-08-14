# 平台资源管理 platform（中断/预留内存/SOC 平台）

## 1. 本讲目标

本讲承接 [u6-l1（SDK-driver 层与 kernel_adapt）](u6-l1-sdk-driver-and-kernel-adapt.md)，把视角从「内核如何调用 Linux API」继续下沉到内核态驱动里的一个**资源中转站**——`platform` 模块。

读者学完本讲应能：

- 理解 `platform` 模块作为「芯片资源注册仓库」的职责：它把硬件资源（中断号、寄存器基址、预留内存）从「发现方」搬运到「使用方」。
- 掌握 **生产者 → 仓库 → 消费者** 这条贯穿全模块的资源流转主线，并能指出每一环对应的源码函数。
- 读懂 `soc_platform.c`（NEAR 形态 SOC 平台，用 UDA 通知触发）如何把 IRQ / 寄存器基址 / 预留内存写入仓库。
- 读懂 `tsdrv_parse.c`（PCIe Host / DC 平台）如何编排「中断解析 + 地址解析」两步。
- 理解中央仓库 `soc_resmng` 的存储结构（以 `devid + 子系统 + 子id` 为主键、以字符串名字为二级键）以及消费方（TRS 任务调度、esched 事件调度）如何读回。

## 2. 前置知识

本讲假设你已经了解：

- **三层架构**（DCMI/HAL/SDK-driver）与「用户态 `ascend_hal.so` 经 `ioctl` 陷入内核态 `.ko`」的跨态模型（见 u1-l1、u6-l1）。
- **内核模块（.ko）的加载与符号导出**：`ka_module_init` / `KA_EXPORT_SYMBOL_GPL` 等是 kernel_adapt 提供的「薄封装」（见 u6-l1）。
- **UDA（统一设备接入）** 的设备号翻译角色，以及设备类型四元组 `(hw, object, location, prop)`（见 u3-l3）。

下面补充三个本讲特有、但容易卡住读者的概念。

### 2.1 为什么需要一个「资源仓库」

一张 NPU 在内核里会被很多子系统使用：TRS（任务调度）要往「门铃寄存器」写命令、要申请 SQ/CQ 预留内存、要注册 CQ 完成中断；esched（事件调度）要拿 topic 中断；mailbox 要拿应答中断。问题是：

- 这些资源的**物理位置**（寄存器在 PCIe BAR 的哪个偏移、预留内存在设备侧的哪个物理地址、中断是 MSI-X 的第几个 vector）只有「最早发现硬件的人」知道——PCIe 探测时解析 BAR、设备树（DTS）/平台数据解析时拿到地址。
- 而这些资源的**使用者**（TRS、esched）并不关心资源是怎么被发现的，它们只想要「给我这块设备的 SQ/CQ 内存地址」「给我第 i 个 CQ 中断号」。

如果让每个使用者都自己去解析 BAR / DTS，代码会到处重复、且和具体芯片强耦合。`platform` 模块就是为此而生的**解耦层**：它负责把硬件资源**发现并登记**进一个中央仓库，使用者只管从仓库里按名字/类型取用。

### 2.2 中断（IRQ）的两层编号

Linux 的 MSI-X 中断有两层编号，本讲会反复出现，先讲清楚：

- **vector id（entry）**：MSI-X 表项编号，是硬件/PCIe 层面的「第几个中断向量」。例如 index 0 是 mailbox 应答，index 1 是 SQ 发送触发，index 2~17 是 CQ 更新。
- **Linux IRQ 号（irq_request）**：把 vector 注册进 Linux 中断子系统后，内核分配的软件中断号，`request_irq()` 注册处理函数时用的就是它。

`platform` 仓库里往往同时存了这两者（`soc_resmng_set_irq_by_index` 存 Linux IRQ，`soc_resmng_set_hwirq` 存 vector），供不同消费者按需取用。

### 2.3 SOC / NEAR / DC / Host 几个词

- **SOC（System On Chip）形态**：NPU 芯片与主机 CPU 处在同一个片上系统/超节点内，主机通过 SOC 内部总线直接管理，本讲对应 `soc_platform`（路径里的 `near` 子目录）。
- **NEAR**：UDA 设备类型里的 `location` 取值，表示「近端」芯片（相对 `LOCAL`/`REMOTE`），即本机可直接触达的实体芯片。
- **DC（Device Control）/ ts_platform_host**：NPU 以 PCIe 加速卡形式插在主机上（ascend910），主机侧作为 Host 管理，对应 `drv_platform/ts_platform_host`。

两套实现是同一个「资源登记」职责在不同硬件形态下的两份代码，这是本讲要对比的核心。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `src/sdk_driver/platform/soc_platform/near/soc_platform.c` | **生产者（SOC/NEAR）** | 内核模块 `ascend_soc_platform.ko`；UDA 通知触发，把 IRQ/寄存器/预留内存写入仓库 |
| `src/sdk_driver/platform/soc_platform/near/soc_platform.h` | 日志宏 | 定义 `soc_err/soc_warn/soc_info/soc_debug` 四个带模块名的日志宏 |
| `src/sdk_driver/platform/dc/drv_platform/ts_platform_host/tsdrv_parse/tsdrv_parse.c` | **生产者编排（PCIe Host）** | 薄编排层，串联「中断解析 + 地址解析」两步 |
| `src/sdk_driver/platform/dc/drv_platform/ts_platform_host/tsdrv_parse/tsdrv_parse.h` | 接口声明 | 声明 `tsdrv_parse_init/exit` |
| `src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h` | 资源结构（结构体风格） | 定义 PCIe Host 侧的 `devdrv_ts_pdata` / `devdrv_platform_data` 等结构体与 DTS 地址索引枚举 |
| `src/sdk_driver/inc/pbl/pbl_soc_res.h` | **仓库接口（门面）** | 声明全部 `soc_resmng_set/get_*` API、资源枚举、实例打包函数 |
| `src/sdk_driver/pbl/soc_resmng/soc_resmng.c` | **仓库实现** | 以 `devid + 子系统 + 子id` 定位设备，按名字/类型存取资源的链表与数组实现 |
| `src/sdk_driver/inc/comm/comm_pcie.h` | 资源读取声明 | 声明 `devdrv_get_addr_info` / `devdrv_get_ts_drv_irq_vector_id` 等，以及 `enum devdrv_addr_type` |
| `src/sdk_driver/comm/pcie/host/devdrv_ctrl.c` | 资源源头 | `devdrv_get_addr_info` 从 PCIe 控制器的 `res` 字段取回原始地址 |
| `src/sdk_driver/inc/pbl/pbl_uda.h` | UDA 设备类型 | `uda_dev_type_pack` 四元组与 NEAR 实体打包函数 |
| `src/sdk_driver/trsdrv/.../trs_chan_near_ops_rsv_mem.c` | **消费者示例** | TRS 读回 `TS_SQCQ_MEM` 并 ioremap |
| `src/sdk_driver/trsdrv/.../trs_chan_irq.c` | **消费者示例** | TRS 读回 IRQ 注册处理函数 |

> 说明：`tsdrv_parse.c` 里调用的 `tsdrv_irq_parse_init` / `tsdrv_addr_parse_init` 的实现文件（`tsdrv_irq_parse.c` / `tsdrv_addr_parse.c`）**不在本开源仓库内**（其声明头也未随仓提供）。本讲对它们的描述仅基于 `tsdrv_parse.c` 的调用与 `devdrv_platform_resource.h` 的结构体定义，不臆造其内部实现。

## 4. 核心概念与源码讲解

### 4.1 platform 模块定位与「生产者→仓库→消费者」模型

#### 4.1.1 概念说明

`platform` 不是某个单一功能，而是 SDK-driver 内核态里一条**资源流水线**的总称。它的存在是为了回答一个问题：

> 一块 NPU 的硬件资源（多少个中断、哪些寄存器基址、哪段预留内存）被发现后，如何被互不相关的多个内核子系统安全地共享？

答案是引入一个**中央资源仓库（registry）**，把流水线拆成三段：

1. **发现（Source）**：PCIe 探测时解析 BAR、平台数据/DTS 解析时拿到地址与中断号，存进 PCIe 控制器的 `pci_ctrl->res` 或平台结构体。
2. **登记（Producer / 本讲的 platform）**：把发现到的资源转换、命名，写入中央仓库 `soc_resmng`。
3. **取用（Consumer）**：TRS、esched 等业务子系统按 `(devid, 子系统, 子id) + 名字/类型` 从仓库读回，再做 `ioremap`、`request_irq` 等动作。

`platform` 模块就是第 2 段「登记」的代码集合，按硬件形态分两份：SOC/NEAR 形态的 `soc_platform`，PCIe Host/DC 形态的 `drv_platform/tsdrv_parse`。

#### 4.1.2 核心流程

整条流水线可以用下面的伪代码与数据流表示：

```
            (1) PCIe probe / DTS parse
   pci_ctrl->res.ts_db / ts_sram / ts_sq ...   ← 原始地址来自 BAR
   devdrv_platform_data.devdrv_addr_base[]      ← 原始地址来自 DTS

            (2) Producer（platform 模块，本讲重点）
   soc_platform_init_instance(devid):           ← SOC/NEAR 形态
       devdrv_get_addr_info(devid, TS_SQ_BASE, ...)
       devdrv_get_ts_drv_irq_vector_id(devid, idx, &irq)
       devdrv_get_irq_vector(devid, irq, &irq_request)
       ──写入──► soc_resmng 仓库（按名字/类型）

   tsdrv_parse_init(devid, dev_info):           ← PCIe Host/DC 形态
       tsdrv_irq_parse_init(...)                ← 解析中断（实现未开源）
       tsdrv_addr_parse_init(...)               ← 解析地址（实现未开源）

            (3) Consumer（TRS / esched / mailbox）
   soc_resmng_get_rsv_mem(inst, "TS_SQCQ_MEM", &mem)  → ka_mm_ioremap(...)
   soc_resmng_get_irq_by_index(inst, irq_type, i, &irq) → request_irq(...)
```

关键的设计取舍是：**仓库用「字符串名字」做二级键**（如 `"TS_SQCQ_MEM"`、`"TS_DOORBELL_REG"`），而不是用偏移量或全局编号。这样生产者和消费者之间只耦合一个**约定好的名字**，双方各自演进互不影响——这和 u3-l4 里 URD 用 `main_cmd/sub_cmd` 二维编号、u5-l1 里 device_monitor 用「请求结构体地址当 msgid」是同一类「以名字/约定解耦」的工程手法。

#### 4.1.3 源码精读

先看仓库的「主键」长什么样——一个三元组：

[pbl_soc_res.h:281-285](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_soc_res.h#L281-L285) 定义了资源实例的主键 `struct res_inst_info`：`devid`（设备号）+ `sub_type`（子系统类型，目前只有 `TS_SUBSYS`）+ `subid`（子系统内编号，对 TS 子系统即 `tsid`）。所有 `set/get` 都先靠它定位到「哪块设备的哪个子系统」。

[pbl_soc_res.h:317-322](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_soc_res.h#L317-L322) 是装配主键的内联函数 `soc_resmng_inst_pack`，生产者每次写仓库前都要先调它填好三元组。

仓库对外暴露的「写」接口集中在一组函数声明里：

[pbl_soc_res.h:325-341](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_soc_res.h#L325-L341) 给出三类资源的 `set/get` 成对接口：预留内存（`set/get_rsv_mem`，按名字）、寄存器基址（`set/get_reg_base`，按名字）、中断（`set_irq_num`/`set_irq_by_index`/`set_hwirq`，按类型+下标）。注意它们成对出现——有 `set` 就有 `get`，这正是「仓库」语义：有人存就有人取。

仓库内部把 TS 子系统的中断按类型分类，类型枚举在这里：

[pbl_soc_res.h:188-205](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_soc_res.h#L188-L205) 定义 `enum soc_ts_irq_type`，列举了 TS 子系统的全部中断类别：`TS_MAILBOX_ACK_IRQ`（mailbox 应答）、`TS_CQ_UPDATE_IRQ`（CQ 完成）、`TS_SQ_SEND_TRIGGER_IRQ`（SQ 发送触发）、`TS_FUNC_CQ_IRQ`、`TS_STARS_TOPIC_IRQ`（topic 调度）等。生产者按类型存，消费者按类型取。

#### 4.1.4 代码实践

**实践目标**：用「双向 grep」亲眼确认生产者与消费者通过同一名字 `TS_SQCQ_MEM` 间接耦合。

**操作步骤**：

1. 在仓库根目录执行，找出所有「写入」`TS_SQCQ_MEM` 的地方（生产者）：
   ```bash
   grep -rn '"TS_SQCQ_MEM"' --include=*.c src/sdk_driver/ | grep set_rsv_mem
   ```
2. 再找出所有「读取」`TS_SQCQ_MEM` 的地方（消费者）：
   ```bash
   grep -rn '"TS_SQCQ_MEM"' --include=*.c src/sdk_driver/ | grep get_rsv_mem
   ```
3. 对比两边的文件，你会发现生产者在 `platform` / `trsdrv` 适配层，消费者在 `trsdrv` 业务层，二者**没有头文件级别的直接依赖**，只靠字符串名字握手。

**需要观察的现象**：两次 grep 命中的文件分属不同目录、互不 include 对方头文件——这正是「仓库解耦」的证据。

**预期结果**：生产者侧命中 `soc_platform.c`（`"TS_SQCQ_MEM"`）与 `trs_chan_near_ops_rsv_mem.c` 的 `set` 调用；消费者侧命中 `trs_chan_near_ops_rsv_mem.c` 的 `get` 调用。若环境无 grep 工具则属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么仓库的「寄存器基址」和「预留内存」用字符串名字做键，而「中断」用 `(类型枚举, 下标)` 做键？

**参考答案**：寄存器基址和预留内存是「有名资源」——一块内存叫什么（`TS_SQCQ_MEM`、`TS_SRAM_MEM`）由业务约定，数量与种类随芯片演进会增减，用字符串名字便于扩展、且消费者只认名字。中断则是「同质、有序」的资源——同一个 CQ 更新中断类型下有 N 个（index 0~N-1），用 `(类型, 下标)` 数组下标访问比遍历字符串链表更高效，也更适合 `for (i=0; i<n; i++)` 批量注册。

**练习 2**：如果新增一类硬件资源（例如一段新的共享内存），需要改仓库的哪些地方？

**参考答案**：通常不需要改仓库实现，只需：① 生产者用已有的 `soc_resmng_set_rsv_mem(inst, "新名字", &mem)` 存入；② 消费者用同名 `soc_resmng_get_rsv_mem` 取出。仓库本身是「按名字挂链表」的通用容器，新名字天然支持。这正是仓库设计的收益。

---

### 4.2 soc_platform：NEAR 形态 SOC 平台的资源装配

#### 4.2.1 概念说明

`soc_platform` 是 SOC/NEAR 形态（如 ascend910_93、ascend950）下的资源登记生产者，编译为独立内核模块 `ascend_soc_platform.ko`（见 4.2.3 的 Makefile）。它的核心职责是：**当一块近端实体 NPU 被内核发现并初始化时，自动把这块芯片的 IRQ、寄存器基址、预留内存登记进 `soc_resmng` 仓库**。

它有两个关键设计：

- **事件驱动（UDA 通知）**：它不主动扫描设备，而是在模块加载时向 UDA 注册一个「通知回调」，等 UDA 发现新设备、触发 `UDA_INIT` 动作时才被回调执行。这是 u3-l3 里 UDA「寻址层」的延伸用法——UDA 不只翻译设备号，还充当设备生命周期事件的派发器。
- **仓库写入式（registry 风格）**：与 4.3 的 `tsdrv_parse`（把资源填进一个结构体）不同，`soc_platform` 直接调 `soc_resmng_set_*` 把资源写进中央仓库，是更现代、更解耦的写法。

#### 4.2.2 核心流程

`soc_platform` 的执行流程：

```
模块加载 ka_module_init(soc_platform_init)
   ├─ uda_notifier_register(... UDA_PRI0, soc_platform_host_notifier_func)   ← 订阅 NEAR 实体设备事件
   │     注册两个设备类型：(DAVINCI, ENTITY, NEAR, REAL) 和 (..., NEAR, UDA_REAL_SEC_EH)
   │
设备被发现 → UDA 回调 soc_platform_host_notifier_func(udevid, action)
   └─ if action == UDA_INIT: soc_platform_init_instance(udevid)
         ├─ soc_platform_set_irq(devid, tsid)        ← 登记 5 类中断
         │     ├─ mailbox 应答中断      (TS_MAILBOX_ACK_IRQ)
         │     ├─ SQ 发送触发中断       (TS_SQ_SEND_TRIGGER_IRQ)
         │     ├─ CQ 更新中断（批量）   (TS_CQ_UPDATE_IRQ)
         │     ├─ 功能 CQ 中断          (TS_FUNC_CQ_IRQ)
         │     └─ topic 调度中断        (TS_STARS_TOPIC_IRQ)
         ├─ soc_platform_set_reg_base(devid, tsid)   ← 登记 8 个寄存器基址
         ├─ soc_platform_set_rsv_mem(devid, tsid)    ← 登记 2 段预留内存
         └─ soc_resmng_subsys_set_num(devid, TS_SUBSYS, 1)  ← 声明 TS 子系统数量=1
模块卸载 ka_module_exit(soc_platform_exit) → 反注册 UDA 通知
```

每登记一个中断，都遵循同一个三步小循环（以单个中断为例）：

1. `devdrv_get_ts_drv_irq_vector_id(devid, index, &irq)`：按 MSI-X 下标取出**硬件 vector**。
2. `devdrv_get_irq_vector(devid, irq, &hwirq)`：把 vector 翻译成 **Linux IRQ 号**。
3. `soc_resmng_set_irq_by_index(...)` + `soc_resmng_set_hwirq(...)`：把 Linux IRQ 和 vector 都存进仓库。

#### 4.2.3 源码精读

先看模块入口与事件订阅——这是整段逻辑的起点：

[soc_platform.c:446-468](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L446-L468) 是模块初始化 `soc_platform_init`，经 `ka_module_init`（kernel_adapt 薄封装，见 u6-l1）注册为内核模块入口。它向 UDA 注册通知回调，订阅的设备类型由 `uda_dev_type_pack` 四元组决定。

[soc_platform.c:450-451](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L450-L451) 调 `uda_davinci_near_real_entity_type_pack` 打包出 `(DAVINCI, ENTITY, NEAR, REAL)` 这个类型，对应的内联定义在 [pbl_uda.h:108-111](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_uda.h#L108-L111)。`UDA_NEAR` 表示「近端」实体芯片，即本机可直接管理的 SOC 形态芯片。`UDA_PRI0` 是回调优先级。

接下来看回调如何把动作分派到实例初始化：

[soc_platform.c:433-444](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L433-L444) 是 `soc_platform_host_notifier_func`，只在 `action == UDA_INIT`（设备初始化）时调 `soc_platform_init_instance`，其他动作（如设备移除）直接返回。这是典型的「事件过滤器」。

[soc_platform.c:418-430](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L418-L430) 是实例初始化总入口 `soc_platform_init_instance`，依次登记中断、寄存器基址、预留内存，最后 `soc_resmng_subsys_set_num` 声明该设备的 TS 子系统数量为 1。这四步是本模块的全部产出。

中断登记的聚合入口：

[soc_platform.c:282-290](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L282-L290) 是 `soc_platform_set_irq`，用 `ret |=` 把 5 个子函数的返回值「或」在一起——任一失败都会在最终返回值里体现（注意这是「或」累加而非短路，目的是即便某类中断登记失败也继续尝试其余类，尽量把能登记的都登记上）。

其中 mailbox 应答中断的登记是理解所有中断登记的模板：

[soc_platform.c:47-86](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L47-L86) 是 `soc_platform_set_mb_irq`，完整展示「三步小循环」：先 `soc_resmng_inst_pack` 装配主键 → `soc_resmng_set_irq_num` 声明这类中断有几个 → `devdrv_get_ts_drv_irq_vector_id` 取 vector → `devdrv_get_irq_vector` 翻译成 Linux IRQ → `soc_resmng_set_irq_by_index` 存 Linux IRQ → `soc_resmng_set_hwirq` 存 vector。其他中断子函数结构与此一致。

CQ 更新中断多了一处**自适应数量**逻辑，值得单独看：

[soc_platform.c:131-147](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L131-L147) 是 `soc_platform_get_cq_update_irq_num`，它根据 `devid` 是否 `≥ VDEV_START_ID(100)`（即是否虚拟设备）、连接协议是否 HCCS、是否 mdev 虚拟机启动模式，把 CQ 中断数从基准 `CQ_UPDATE_IRQ_NUM(16)` 动态折半（`/8` 或 `/2`）。这说明**资源数量本身会随虚拟化/拓扑形态而变**，仓库登记的是「实际可用数量」而非固定值。`devdrv_get_connect_protocol` 的声明见 [comm_msg_chan.h:182](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/comm/comm_msg_chan.h#L182)。

寄存器基址的登记展示「按名字存」的典型用法：

[soc_platform.c:292-385](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L292-L385) 是 `soc_platform_set_reg_base`，对每个寄存器区都重复「`devdrv_get_addr_info` 取地址 → `soc_resmng_set_reg_base` 按名字存」两步，共登记 8 个名字：`TS_DOORBELL_REG`、`TS_STARS_RTSQ_SCHED_REG`、`TS_STARS_CQINT_REG`、`TS_STARS_CDQM_REG`、`TS_STARS_INT_REG`、`TS_STARS_NOTIFY_TBL_REG`、`TS_STARS_EVENT_TBL_NS_REG` 等。其中后三个用 `soc_warn` 容忍缺失（老型号可能没有），体现「按型号渐进登记」。

预留内存的登记最短，却最能体现生产者职责：

[soc_platform.c:387-416](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L387-L416) 是 `soc_platform_set_rsv_mem`，登记两段内存：`TS_SRAM_MEM`（取自 `DEVDRV_ADDR_TS_SRAM`）与 `TS_SQCQ_MEM`（取自 `DEVDRV_ADDR_TS_SQ_BASE`）。注意第二段若取地址失败只打 `soc_info` 不报错——某些形态 SQ 内存不在设备侧而在主机侧（见 4.4 消费者里的 `h2d` 转换）。

`devdrv_get_addr_info` 读的是哪里的数据？看它的实现源头：

[devdrv_ctrl.c:2041-2057](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/comm/pcie/host/devdrv_ctrl.c#L2041-L2057) 是 `devdrv_get_addr_info_by_type`，可见原始地址来自 PCIe 控制器的 `pci_ctrl->res.ts_db/ts_sram/ts_sq/...`——这些是 PCIe 探测时解析 BAR 填进去的。所以 `devdrv_get_addr_info` 是「源头读取」，`soc_resmng_set_*` 是「仓库写入」，`soc_platform` 正是中间的搬运工。

最后看构建配置，确认它是个独立 `.ko`：

[src/sdk_driver/platform/soc_platform/near/CMakeLists.txt:12-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/CMakeLists.txt#L12-L17) 用 `add_host_ko` 声明产物名 `ascend_soc_platform`，依赖 `asdrv_pbl`（仓库）、`asdrv_trs`（消费者之一）、`drv_pcie_host`（地址源头）等——依赖列表本身就印证了「生产者依赖仓库与源头、被消费者依赖」的关系。

#### 4.2.4 代码实践

**实践目标**：跟踪一个中断从「硬件 vector」到「仓库条目」的完整登记路径，并理解登记后谁能取到它。

**操作步骤**：

1. 打开 `soc_platform.c`，定位 `soc_platform_set_cq_update_irq`（[L149](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L149)），画出「`get_cq_update_irq_num` → 循环 `get_ts_drv_irq_vector_id` → `get_irq_vector` → `set_irq_by_index` + `set_hwirq`」的循环体。
2. 用 grep 找出谁消费 `TS_CQ_UPDATE_IRQ` 这类中断：
   ```bash
   grep -rn "TS_CQ_UPDATE_IRQ" --include=*.c src/sdk_driver/trsdrv src/sdk_driver/esched
   ```
3. 在消费方（如 `trs_chan_irq.c`）确认它是用 `soc_resmng_get_irq_by_index` 读回的。

**需要观察的现象**：登记侧用的是 `TS_CQ_UPDATE_IRQ` 枚举值，消费侧用的也是同一个枚举值——两端靠枚举对齐，没有硬编码下标。

**预期结果**：能画出「vector → Linux IRQ → 仓库 → 消费者 request_irq」的完整链路。中断实际注册（`request_irq`）发生在消费侧而非 platform 侧——platform 只负责「把号码登记好」。若无法在本地编译运行内核模块，则跟踪结论属「源码阅读型实践」，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`soc_platform_set_irq` 用 `ret |=` 累加五个子函数返回值，而不是 `ret = f1(); if(ret) return ret;` 短路返回。这样设计有什么好处和风险？

**参考答案**：好处是「尽量装满」——即使某一类中断（如老芯片没有 topic 中断）登记失败，也不影响其余中断登记，保证已登记的资源可用。风险是错误码被「或」运算污染（多个非零错误码相或，值失去诊断意义），且无法在第一个失败处立即感知。代码用大量 `soc_err` 日志弥补了诊断问题。

**练习 2**：为什么 `soc_platform` 用 UDA 通知回调触发，而不是在模块加载时自己遍历设备？

**参考答案**：因为设备可能在模块加载**之后**才被热插拔/枚举进来（尤其超节点/UB 场景）。UDA 作为设备生命周期事件中心，能在「设备真正就绪可初始化」的时刻回调，避免 platform 漏登记后到设备、或对尚未就绪的设备过早登记。

---

### 4.3 drv_platform / tsdrv_parse：PCIe Host（DC）平台的资源解析

#### 4.3.1 概念说明

当 NPU 以 PCIe 加速卡形式（ascend910）插在主机上时，主机侧是「Host」，对应的资源登记代码在 `platform/dc/drv_platform/ts_platform_host/` 下（`dc` = Device Control，`ts_platform_host` = TS 平台主机侧）。它的入口是 `tsdrv_parse.c`。

与 `soc_platform`（直接写仓库）不同，这一路更接近**传统 Linux 驱动的平台数据（platform_data）风格**：把解析出的资源填进一个大的结构体 `struct devdrv_ts_pdata` / `struct devdrv_platform_data`，供同模块内的其他逻辑直接按字段读取。可以理解为它是「结构体风格的资源仓库」，而 `soc_platform` 是「注册表风格的资源仓库」。两者职责相同，风格与适用形态不同。

> 需要再次强调：`tsdrv_parse.c` 调用的 `tsdrv_irq_parse_init`、`tsdrv_addr_parse_init` 的实现文件不在本开源仓库内，本节只讲「编排层做了什么」和「目标结构体长什么样」，不臆造解析实现。

#### 4.3.2 核心流程

`tsdrv_parse` 的编排非常简洁：

```
tsdrv_parse_init(devid, dev_info):
   ├─ 参数校验（dev_info / pdata 非空；UT 模式下放宽）
   ├─ tsdrv_irq_parse_init(devid, tsid, dev_info)   ← 解析中断，填进 dev_info->pdata->ts_pdata[]
   ├─ tsdrv_addr_parse_init(devid, tsid, dev_info)  ← 解析地址，填进 ts_pdata[] 的各 *_paddr/_vaddr 字段
   └─ return 0
   （失败路径：UT 模式不返回错误；非 UT 模式回滚已解析的中断）

tsdrv_parse_exit(devid, dev_info):
   ├─ tsdrv_addr_parse_exit(...)
   └─ tsdrv_irq_parse_exit(...)    ← 逆序回收
```

两步顺序（先 IRQ 后地址）与退出时的逆序（先地址后 IRQ）成对，是「申请-释放」标准对称写法。

#### 4.3.3 源码精读

入口编排：

[tsdrv_parse.c:18-47](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/tsdrv_parse/tsdrv_parse.c#L18-L47) 是 `tsdrv_parse_init`。注意其中大量 `#ifndef TSDRV_UT` 条件编译——这是为单元测试留的「桩」：在 UT（`TSDRV_UT` 宏打开）模式下，即便 `dev_info` 为空或解析失败也不返回错误，让单测能独立编译运行（与 u6-l1 提到的 `#ifdef EMU_ST`、本讲的 `#ifndef TSDRV_UT` 是同一类「为测试放宽真实约束」的手法，呼应 u8-l2 的 UT 体系）。真实（非 UT）路径下，地址解析失败会 `goto err_addr_parse_init` 回滚已登记的中断，保证不留半成品。

[tsdrv_parse.c:49-55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/tsdrv_parse/tsdrv_parse.c#L49-L55) 是 `tsdrv_parse_exit`，严格逆序：先退出地址解析、再退出中断解析。

解析结果填进的目标结构体定义在资源头里：

[devdrv_platform_resource.h:108-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h#L108-L163) 是 `struct devdrv_ts_pdata`，这是单块 TS 子系统资源的「结构体仓库」：物理地址（`sram_paddr`、`ts_mbox_send_paddr`、`doorbell_paddr`、`stars_ctrl_paddr`、`stars_sq_rsvmem_paddr`、`numa_base_paddr`）、大小、虚拟地址（`__ka_mm_iomem *` 内核 IO 映射指针）、中断数组（`irq_cq_update[]`、`irq_cq_update_request[]`、`irq_mailbox_ack`、`irq_functional_cq` 等）。对比 4.2 里 `soc_platform` 把同样语义的资源按名字存进 `soc_resmng`，这里是把它们作为结构体字段集中存放——同一份信息，两种容器。

地址资源在平台数据里用「DTS 索引数组」组织：

[devdrv_platform_resource.h:89-104](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h#L89-L104) 定义 `enum devdrv_dts_addr_index`，列举 DTS（Device Tree Source）里各项资源的下标：GIC 基址、TS 子系统控制、TS 门铃、TS SRAM、dispatch、sysctl、stars、ras、aicore、tsensor 共享内存等。`devdrv_platform_info` 里用 `devdrv_addr_base[DEVDRV_DTS_MAX_RESOURCE_NODE]` / `devdrv_addr_size[]` 两个平行数组按这些下标存放地址（见 [devdrv_platform_resource.h:180-181](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h#L180-L181)）。这是「按下标存」的另一种资源容器风格。

注意头里大量 `CFG_SOC_PLATFORM_CLOUD_V2` 宏分支（如 [L63-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h#L63-L80) 把 `DEVDRV_CQ_IRQ_NUM`、`DEVDRV_TS_MEMORY_SIZE` 在 V2 与非 V2 间取不同值）——这正是 u1-l2 / u8-l3 讲的「`--soc` → CMake → `CFG_*` 宏在编译期冻结芯片能力」在资源层的体现：不同芯片的资源容量在编译期就已定型。

#### 4.3.4 代码实践

**实践目标**：对比「结构体风格」与「注册表风格」两种资源容器，理解同一信息为何有两份代码。

**操作步骤**：

1. 在 `devdrv_platform_resource.h` 的 `devdrv_ts_pdata` 里找出「mailbox 应答中断」对应的字段（`irq_mailbox_ack` / `irq_mailbox_ack_request`，[L140-143](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/dc/drv_platform/ts_platform_host/ascend910/devdrv_platform_resource.h#L140-L143)）。
2. 回到 4.2 的 `soc_platform.c`，找出「mailbox 应答中断」在注册表里对应的名字（`TS_MAILBOX_ACK_IRQ` 枚举 / `mbox_ack_irq` 字符串）。
3. 列一张对照表：同一资源（mailbox 应答中断）在两种风格下分别用什么键去存/取。

**需要观察的现象**：结构体风格用「字段名」`irq_mailbox_ack` 存取；注册表风格用「枚举值」`TS_MAILBOX_ACK_IRQ` 存取。两者承载的信息一致。

**预期结果**：对照表能说明「结构体风格耦合于头文件定义、读写快但扩展需改结构；注册表风格耦合于名字、扩展只需加名字、但读取需遍历链表」。这是架构演进的痕迹——新形态（NEAR/950）倾向于注册表风格。

#### 4.3.5 小练习与答案

**练习 1**：`tsdrv_parse_init` 里为什么把「地址解析失败」设计成回滚「中断解析」，而不是直接返回？

**参考答案**：因为中断已经先于地址成功解析并可能已申请了内核资源（如已分配 IRQ 描述符、已部分注册）。若地址失败就直接返回而不回滚，会留下「已登记的中断但无对应地址」的半成品状态，后续使用时崩溃或泄漏。逆序回滚（`tsdrv_irq_parse_exit`）保证「要么全成功，要么完全干净」，是资源管理的标准 RAII 思想。

**练习 2**：`devdrv_ts_pdata` 同时存了 `xxx_paddr`（物理地址）和 `xxx_vaddr`（`__ka_mm_iomem *` 虚拟地址）。为什么两个都要存？

**参考答案**：物理地址用于**跨设备/跨进程传递**（如告诉对端设备某内存在它地址空间的位置、或写入 DMA 描述符）；虚拟地址用于**本机内核直接读写**（经 `ioremap` 映射后 CPU 访问寄存器/内存）。两者用途不同，缺一不可。`soc_platform` 那一路只存物理地址（`soc_rsv_mem_info.rsv_mem` 是 `phys_addr_t`），虚拟映射由消费方按需 `ioremap`（见 4.4）——这也是两种风格的又一差异。

---

### 4.4 资源仓库 soc_resmng 的存储机制与消费者

#### 4.4.1 概念说明

前两节的生产者都把资源写进 `soc_resmng`，本节补全仓库的**存储结构**与**消费方**。理解了本节，整条流水线才闭环。

`soc_resmng` 的存储模型是「**两级定位 + 按类型分容器**」：

- **第一级：设备**。每个 `devid` 对应一个 `struct soc_dev_resmng`（由 `get_resmng(devid)` 取出）。
- **第二级：子系统**。设备内按 `sub_type`（目前仅 `TS_SUBSYS`）再分数组，如 `ts_resmng[subid]`（`subid` 即 `tsid`）。
- **容器分类型**：在该子系统下，寄存器基址、预留内存用**带名字的链表节点**存；中断用**按 `(类型, 下标)` 的数组**存；另有键值对（`key_value`）、属性（`attr`）、MIA 资源（`mia_res`）等容器。

这种「按名字挂链表」的设计让仓库成为通用容器——生产者写任何新名字，消费者按同一名字读，仓库实现无需改动。

#### 4.4.2 核心流程

一次「写」与一次「读」的内部流程：

```
写入 soc_resmng_set_rsv_mem(inst, "TS_SQCQ_MEM", &mem):
   ├─ inst_param_check(inst)              ← 校验主键三元组合法
   ├─ get_resmng(inst->devid)             ← 取设备级容器
   ├─ if inst->sub_type == TS_SUBSYS:
   │     subsys_ts_set_rsv_mem(&resmng->ts_resmng[subid], name, mem)
   │       ├─ rsv_mem_node_find(name, &ts_resmng->rsv_mems_head)  ← 名字命中则更新
   │       └─ 未命中则新建节点 → ka_list_add 进链表
   └─ 返回

读出 soc_resmng_get_rsv_mem(inst, "TS_SQCQ_MEM", &mem):
   └─ 同样定位到 ts_resmng[subid] → rsv_mem_node_find → 拷出 mem

消费者 TRS 取用：
   soc_resmng_get_rsv_mem(inst, "TS_SQCQ_MEM", &rsv_mem)
     → ka_mm_ioremap(paddr, size)         ← 物理地址映射为内核虚拟地址
     → trs_rsv_mem_init(...)              ← 交给 TRS 的预留内存管理
```

中断的存取走数组下标而非名字链表：

```
soc_resmng_set_irq_by_index(inst, irq_type, index, irq)  → ts_resmng->irq[irq_type][index] = irq
soc_resmng_get_irq_by_index(inst, irq_type, index, &irq) → 读回
```

#### 4.4.3 源码精读

仓库写函数的通用骨架（以预留内存为例）：

[soc_resmng.c:531-565](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/soc_resmng/soc_resmng.c#L531-L565) 是 `soc_resmng_set_rsv_mem`，体现全部写函数的统一三段式：① `inst_param_check` 校验主键 → ② `get_resmng(devid)` 取设备容器 → ③ 按 `sub_type` 分派到子系统级 `subsys_ts_set_rsv_mem`。它还做了名字长度校验（`SOC_RESMNG_MAX_NAME_LEN`，见 [pbl_soc_res.h:20](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_soc_res.h#L20)），防止越界。函数末尾 `KA_EXPORT_SYMBOL_GPL(soc_resmng_set_rsv_mem)` 把符号以 GPL 导出，供其他 `.ko`（如 `ascend_soc_platform`）调用——这与 u6-l1 的 kernel_adapt 导出符号同源。

中断按下标存的骨架：

[soc_resmng.c:741-766](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/soc_resmng/soc_resmng.c#L741-L766) 是 `soc_resmng_set_irq_by_index`，结构与上面完全一致（校验→取容器→按 `TS_SUBSYS` 分派 `subsys_ts_set_irq_by_index`），区别仅在中断按下标而非名字定位。

按名字查找节点的实现，揭示「名字链表」本质：

[soc_resmng.c:95-121](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/soc_resmng/soc_resmng.c#L95-L121) 是 `rsv_mem_node_find` 与 `io_bases_node_find`，用 `ka_list_for_each_entry_safe` 遍历链表、`ka_base_strcmp` 比名字命中。可见「按名字存取」本质是**线性查找链表**——名字空间小（每类十几个），线性查找可接受；若名字激增才需换哈希/树。这是「简单优先」的工程取舍。

> 数学上，设某子系统下某类资源节点数为 \(n\)，则按名字读写的复杂度为 \(O(n)\)；而中断按下标读写为 \(O(1)\)。本模块里 \(n\) 是个位数到十几，\(O(n)\) 与 \(O(1)\) 实际差异可忽略，故选了实现最简单的链表。
>
> \[
> T_{\text{byName}}(n)=O(n),\qquad T_{\text{byIndex}}=O(1)
> \]

现在看消费方如何取用——这是流水线的终点。预留内存消费者：

[trs_chan_near_ops_rsv_mem.c:55-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/lba/near/comm/adapt/trs_host_chan/trs_chan_near_ops_rsv_mem.c#L55-L71) 是 `trs_chan_ops_get_rsv_mem`，TRS 用 `soc_resmng_get_rsv_mem(inst, name, &rsv_mem)` 按名字取回（名字正是 4.2 里 `soc_platform` 存入的 `TS_SQCQ_MEM`）。取出后：

[trs_chan_near_ops_rsv_mem.c:73-101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/lba/near/comm/adapt/trs_host_chan/trs_chan_near_ops_rsv_mem.c#L73-L101) 的 `_trs_chan_ops_rsv_mem_init` 把取回的物理地址 `paddr` 经 `ka_mm_ioremap` 映射成内核虚拟地址，再交给 TRS 的预留内存管理器 `trs_rsv_mem_init`。注意：**platform 只登记物理地址，虚拟映射由消费方按需做**——这是职责的清晰切分。

中断消费者：

[trs_chan_irq.c:32-46](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/lba/comm/adapt/trs_chan_irq.c#L32-L46) 是 `trs_chan_get_irq_by_index`，TRS 用 `soc_resmng_get_irq_by_index(inst, irq_type, irq_index, irq)` 按 `(类型, 下标)` 取回 Linux IRQ 号，随后（同文件下游）注册中断处理函数。这与 4.2 里 `soc_platform_set_irq_by_index` 存入的 `(类型, 下标)` 完全对齐。

[trs_chan_irq.c:48-69](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/lba/comm/adapt/trs_chan_irq.c#L48-L69) 的 `trs_chan_get_irq` 展示批量读取：先 `soc_resmng_get_irq_num` 取该类型中断总数，再循环 `get_irq_by_index` 取每一个——正好对应 4.2 里 `soc_platform_set_cq_update_irq` 按自适应数量批量登记的写法。

至此整条流水线闭环：

| 阶段 | 代码位置 | 做什么 |
| --- | --- | --- |
| 源头 | `devdrv_ctrl.c` `devdrv_get_addr_info` | 从 `pci_ctrl->res` 取原始地址 |
| 生产者 | `soc_platform.c` `set_rsv_mem`/`set_irq`/`set_reg_base` | 命名并写入 `soc_resmng` |
| 仓库 | `soc_resmng.c` `set_*`/`get_*` | 按 `(devid,sub,subid)+名字/类型` 存取 |
| 消费者 | `trs_chan_near_ops_rsv_mem.c` / `trs_chan_irq.c` | 读回后 `ioremap`/`request_irq` |

#### 4.4.4 代码实践

**实践目标**：亲手画出「TS_SQCQ_MEM 预留内存」从硬件到 TRS 使用的完整生命周期，并理解 platform 为上层支撑了哪些操作。

**操作步骤**：

1. **源头**：在 `comm_pcie.h` 找 `DEVDRV_ADDR_TS_SQ_BASE`（[L447](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/comm/comm_pcie.h#L447)），在 `devdrv_ctrl.c` 确认它读自 `pci_ctrl->res.ts_sq`（[L2054-2057](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/comm/pcie/host/devdrv_ctrl.c#L2054-L2057)）。
2. **生产**：在 `soc_platform.c` 确认它以名字 `"TS_SQCQ_MEM"` 存入仓库（[L404-410](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L404-L410)）。
3. **存储**：在 `soc_resmng.c` 确认存进 `ts_resmng[subid]` 的预留内存链表（[L531-565](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/soc_resmng/soc_resmng.c#L531-L565)）。
4. **消费**：在 `trs_chan_near_ops_rsv_mem.c` 确认 TRS 按同名读回并 `ioremap`（[L81-86](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/trsdrv/trs/lba/near/comm/adapt/trs_host_chan/trs_chan_near_ops_rsv_mem.c#L81-L86)）。
5. 用四句话总结 `platform` 为上层支撑了哪些操作：SQ/CQ 内存映射、中断注册、寄存器访问、mailbox/topic 通信基址。

**需要观察的现象**：四步之间没有任何一个函数直接调用下一个——它们全靠「仓库名字」和「EXPORT_SYMBOL」间接耦合。

**预期结果**：画出一张包含「PCIe BAR → pci_ctrl->res → devdrv_get_addr_info → soc_resmng_set_rsv_mem → soc_resmng_get_rsv_mem → ioremap → TRS 使用」的箭头图，并标注每一步的文件名与行号。这是本讲的综合实践，建议把图画到笔记里。本实践为源码阅读型，无需运行，结论可即时验证。

#### 4.4.5 小练习与答案

**练习 1**：如果两个生产者不小心用同一个名字 `"TS_SQCQ_MEM"` 往同一 `(devid, tsid)` 写了不同的内存地址，仓库会怎样？

**参考答案**：由 `rsv_mem_node_find`（[soc_resmng.c:95-106](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/soc_resmng/soc_resmng.c#L95-L106)）的逻辑决定——若查找命中已有同名节点，通常是**更新覆盖**该节点的值；若未命中则新建。因此后写者覆盖先写者。这意味着名字是「软约定」：生产方必须遵守命名规范，否则会相互覆盖，且编译期无法发现。这也是为什么 `TS_SQCQ_MEM` 这类名字在仓里被当作跨模块契约对待。

**练习 2**：仓库为什么对中断用数组下标、对内存用名字链表，而不是统一用一种？

**参考答案**：两种资源的使用模式不同。中断是「同质批量」——一个类型下有 N 个、消费方按 `for i` 逐个注册处理函数，数组下标 \(O(1)\) 最自然。内存是「异质有名」——每段有不同语义（SRAM、SQ/CQ、共享内存），消费方按语义名字取用，且数量少、会随芯片增减，名字链表扩展性最好。统一用一种反而别扭：用数组存内存要预先编号、用链表存中断要遍历。仓库为两类资源各选了最贴合的容器。

---

## 5. 综合实践

设计一个贯穿本讲的源码阅读任务：**「绘制 platform 资源登记全景图并补全一张缺失的生产者」**。

**任务背景**：假设团队评审时发现，某新型号芯片新增了一段「TS event table」预留内存，TRS 侧需要用，但暂时没人把它登记进仓库。

**任务步骤**：

1. **理清现状**：通读 4.2~4.4，确认当前 `soc_platform_set_reg_base` 已经登记了 `TS_STARS_EVENT_TBL_NS_REG` **寄存器基址**（[soc_platform.c:372-382](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L372-L382)），但 `soc_platform_set_rsv_mem` 只登记了 `TS_SRAM_MEM` 和 `TS_SQCQ_MEM` 两段**内存**（[soc_platform.c:387-416](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/platform/soc_platform/near/soc_platform.c#L387-L416)）。
2. **画全景图**：画一张图，把 `DEVDRV_ADDR_TS_EVENT_TBL_NS_BASE`（[comm_pcie.h:477](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/comm/comm_pcie.h#L477)）→ `devdrv_get_addr_info` → `soc_resmng_set_*` → `soc_resmng_get_*` → `ioremap` 这条链路完整画出。
3. **补全生产者（纸面设计，不改动源码）**：参照 `TS_SQCQ_MEM` 的写法，写出「若要把 event table 也作为预留内存登记」应在 `soc_platform_set_rsv_mem` 里增加的代码片段（伪代码即可），并指出：① 用什么名字（例如 `TS_EVENT_TBL_MEM`）；② 取地址用哪个 `devdrv_addr_type`；③ 消费方应如何用同名读回。
4. **反思**：说明为什么这个新增只需改生产者一处 + 消费者一处，而**仓库实现完全不用改**——这正是仓库解耦的收益。

**预期产出**：一张全景图 + 一段伪代码补丁 + 一句关于「仓库可扩展性」的结论。本任务纯源码阅读与设计，无需编译运行。

## 6. 本讲小结

- `platform` 模块是 SDK-driver 内核态的**资源登记中转站**，把硬件资源从「发现方」（PCIe BAR / DTS）搬运到「使用方」（TRS、esched、mailbox）。
- 整条流水线是 **生产者 → 仓库 → 消费者**：生产者读取原始资源并命名写入，消费者按名字/类型读回再做 `ioremap`/`request_irq`。
- `soc_platform.c`（NEAR/SOC 形态，ascend910_93/950）是「注册表风格」生产者：UDA 通知回调驱动，直接调 `soc_resmng_set_*` 登记五类中断、八个寄存器基址、两段预留内存，编译为 `ascend_soc_platform.ko`。
- `tsdrv_parse.c`（PCIe Host/DC 形态，ascend910）是「结构体风格」生产者编排层：串联中断解析与地址解析，结果填进 `devdrv_ts_pdata`（其实现在本开源仓外）。
- 中央仓库 `soc_resmng` 以 `(devid, 子系统, 子id)` 为主键，寄存器/内存按**字符串名字链表**存（\(O(n)\)），中断按 `(类型, 下标)` 数组存（\(O(1)\)），通过 `KA_EXPORT_SYMBOL_GPL` 跨模块共享。
- 两种风格的差异本质是架构演进：新形态倾向名字注册表（解耦、可扩展），老形态用结构体字段（紧凑、读写快）；`platform` 为上层支撑了 SQ/CQ 内存映射、中断注册、寄存器访问、mailbox/topic 通信基址等关键操作。

## 7. 下一步学习建议

本讲把「资源如何被登记与共享」讲透了，接下来可以：

- 进入 **[u6-l3（TRS 任务资源调度：SQ/CQ 通信与 mailbox）](u6-l3-trs-sqcq.md)**，看本讲登记的 `TS_SQCQ_MEM` 预留内存和 CQ 更新中断如何被 TRS 真正用于任务下发与完成回收——那是 platform 仓库最主要的消费者。
- 进入 **[u6-l4（TS Agent 与 esched 事件调度）](u6-l4-ts-agent-and-esched.md)**，看 `TS_STARS_TOPIC_IRQ` 中断和 topic 调度寄存器基址如何被 esched 消费。
- 回顾 **u3-l3（UDA）**，对比 UDA 作为「设备号翻译层」与本讲 UDA 作为「生命周期事件派发器」的两种用法，加深对 UDA 多面性的理解。
- 若对仓库实现细节感兴趣，可通读 `src/sdk_driver/pbl/soc_resmng/soc_resmng.c` 全文（约 2700 行），观察 `mia_res`（算力资源 bitmap）、`attr`（属性）、`key_value` 等其他容器的存取，它们为 u7-l5 的算力切分（vascend）提供资源记账基础。
