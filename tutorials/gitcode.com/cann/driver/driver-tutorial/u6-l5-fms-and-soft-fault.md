# FMS 故障管理系统与 soft_fault 软故障处理

## 1. 本讲目标

NPU 在长期运行中难免出现异常：设备 OS 重启、驱动内核态组件不可恢复、上层守护进程（如 `iammgr`、`process-manager`、`tsdaemon`）发生软故障等。如果这些异常没人管，要么被静默吞掉，要么直接硬复位整张卡，代价都很高。本讲讲解昇腾驱动**内核态**的故障管理系统——**FMS（Fault Management System）**，以及其中最贴近软件故障的 **soft_fault** 子模块。

学完本讲，你应当能够：

- 说清 FMS 内核模块（`asdrv_fms.ko`）是如何装配起来的，以及 `smf`（传感器/事件框架）与 `dtm`（设备拓扑控制块）这两根骨架各管什么。
- 画出一次软故障「**检测 → 上报 → 入队 → 周期扫描 → 上报出去/恢复**」的完整生命周期，并讲清 `soft_fault_event_handler` 与 `soft_fault_event_scan` 这对“生产者—消费者”的分工。
- 理解 `os_reset`（设备 OS 初始化事件）与 `drv_kernel_soft`（驱动内核态不可恢复软故障）这两类内置故障源在故障恢复链路中扮演的角色。
- 掌握用户态进程如何经 `dms_sensor_interface` 注册自己的软故障传感器并上报，以及近期提交「Add soft_fault whitelist」引入的**白名单访问控制**机制为什么是必要的安全门。

## 2. 前置知识

本讲是单元 6（SDK-driver 内核层）的一环，默认你已经学过：

- **u6-l1**：`sdk_driver` 编译为内核模块 `.ko`，与用户态 `ascend_hal`（`.so`）经 `ioctl` 跨态通信；`kernel_adapt`（`ka_*`）是唯一直接调 Linux 内核 API 的适配底座，其余子模块只调它导出的符号。本讲里你会大量看到 `ka_task_*`、`ka_list_*`、`ka_mm_*`、`ka_notifier_block_t` 等 `ka_*` 封装，它们都来自 kernel_adapt。
- **u3-l4**：**URD（User Request Distribute）** 用「一个设备 fd + 一个 ioctl 号」靠 `main_cmd/sub_cmd` 二维编号把命令分发到内核各处理者。本讲的软故障命令（`soft_node_register` 等）正是 URD 命令表里的一行，用户态 ioctl 经 URD 派发进来。
- **u3-l3**：**UDA** 把应用逻辑 devid 翻译成物理设备号。本讲里 `devdrv_manager_container_logical_id_to_physical_id` 就是这套翻译。
- 一些 Linux 内核常识：**通知链（notifier chain）**——内核里“事件订阅/广播”的机制，注册一个 `notifier_block`，事件发生时回调 `.notifier_call`；**设备树（device tree）**——`of_property_read_*` 从硬件描述里读节点属性；**task exit profile**——进程退出时的内核钩子。

几个本讲反复出现的术语，先统一解释：

| 术语 | 含义 |
|------|------|
| **故障（fault）/ 软故障（soft fault）** | 不是硬件物理损坏，而是软件层面的事件：OS 重启完成、守护进程异常、调度器报错等。用结构体 `struct soft_fault` 描述。 |
| **传感器（sensor）** | 故障的“产生源”抽象。每个传感器有一类 `sensor_type`，被周期性“扫描”以取出它产生的事件。 |
| **节点（node）** | 一类逻辑设备的抽象（如 OS Linux、DRV_KERNEL、用户进程组），节点下挂若干传感器。 |
| **断言（assertion）** | 事件的方向：`OCCUR`(发生)、`RESUME`(恢复)、`ONE_TIME`(一次性，不配对恢复)。 |

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `src/sdk_driver/fms/` 下：

| 文件 | 作用 |
|------|------|
| [`fms_module.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c) | FMS 内核模块（`asdrv_fms.ko`）的入口，用子模块表装配 `dtm`/`smf`/`fpdc`/特性自动初始化。 |
| [`dtm/dms_dtm_init.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/dtm/dms_dtm_init.c) | **D**evice **T**opology **M**anagement：初始化系统/设备控制块（节点链表的“容器”）。 |
| [`smf/dms_smf_init.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/smf/dms_smf_init.c) | **S**ensor **M**anagement **F**ramework：初始化事件框架与传感器框架（扫描调度）。 |
| [`soft_fault/soft_fault.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c) | soft_fault 核心：事件队列、上报处理函数、扫描函数、状态机、初始化。 |
| [`soft_fault/soft_fault_define.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault_define.h) | soft_fault 的核心数据结构定义（`soft_fault`/`soft_event`/`soft_dev` 等）。 |
| [`soft_fault/os_reset.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c) | 设备 OS（重）初始化事件的注册与上报（故障恢复信号）。 |
| [`soft_fault/drv_kernel_soft.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.c) | 驱动内核态“不可恢复软故障”的上报接口（`EXPORT_SYMBOL_GPL`）。 |
| [`soft_fault/dms_sensor_interface.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c) | 用户态入口：3 个 URD 命令处理函数 + 白名单访问控制。 |
| [`soft_fault/drv_kernel_soft.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.h) | 白名单的访问角色/环境位定义与 `struct soft_fault_acc`。 |
| [`inc/fms/soft_fault_whitelist.inc`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist.inc) | 白名单表（950 及以后芯片）。 |
| [`inc/pbl/pbl_feature_loader.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_feature_loader.h) | `DECLAER_FEATURE_AUTO_INIT` 宏——特性自动初始化机制。 |
| [`inc/pbl/pbl_urd.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_urd.h) | `BEGIN_DMS_MODULE_DECLARATION`/`ADD_FEATURE_COMMAND` 宏——URD 命令表声明。 |

## 4. 核心概念与源码讲解

### 4.1 FMS 故障管理系统总览：模块装配与 smf/dtm 骨架

#### 4.1.1 概念说明

FMS 是一个**独立的内核模块** `asdrv_fms.ko`（见 [`fms/CMakeLists.txt`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/CMakeLists.txt)，目标名 `asdrv_fms`）。它的职责是「**把分散在各处的故障信号汇聚起来、分类、周期性地扫出去，交给上层事件框架做分发与收敛**」。

要理解 FMS，先要分清两根骨架：

- **dtm（Device Topology Management，设备拓扑管理）**：管「**容器**」。它初始化一张全局的系统控制块 `g_dms_system_ccb`，里面按 `dev_id` 开了 `ASCEND_DEV_MAX_NUM` 个槽位，每个槽位挂一条 `dev_node_list`——后续所有故障节点（包括 soft_fault 注册的节点）都挂到这些链表上。dtm 还可选地从设备树解析一张“设备状态表”。
- **smf（Sensor Management Framework，传感器管理框架）**：管「**调度与事件**」。它初始化事件框架（`dms_event_init`）和传感器框架（`dms_sensor_init`）。传感器框架的核心能力是**周期性地扫描所有已注册传感器的 `pf_scan_func`**，把传感器产出的事件取走、上报。soft_fault 正是利用这套扫描机制，把自己的 `soft_fault_event_scan` 注册成传感器的扫描函数。

一句话：**dtm 提供“节点挂在哪”，smf 提供“传感器怎么被周期性扫”，soft_fault 在二者之上实现“软故障的具体语义”。**

#### 4.1.2 核心流程

FMS 模块加载/卸载采用昇腾内核驱动里反复出现的「**子模块表 + 正向初始化 / 失败逆序回滚**」模式（与 u6-l3 的 trsdrv、u6-l4 的 ts_agent 同构）：

```
insmod asdrv_fms.ko
  └─ ka_module_init(init_fms_base)
       for each in g_sub_table: 顺序调用 init()
          1. dms_dtm_init()        ← dtm：建系统/设备控制块
          2. dms_smf_init()        ← smf：建事件+传感器框架
          3. fpdc_receiver_init()  ← (非 edge host) 故障/panic 数据接收
          4. module_feature_auto_init()  ← 按阶段跑所有 DECLAER_FEATURE_AUTO_INIT
                  └─ STAGE_5 → soft_init()   ← soft_fault 在此进入
       任一失败 → goto out：逆序调用已成功者的 uninit()
rmmod
  └─ ka_module_exit(exit_fms_base)
       逆序调用每个 uninit()
```

关键认知：**soft_fault 并不在 `g_sub_table` 里直接列出**，它是通过第 4 项 `module_feature_auto_init()` 触发的「特性自动初始化」机制、在 `FEATURE_LOADER_STAGE_5` 阶段被加载的。这是一种**解耦**设计——特性子模块只需用宏声明自己的 init/uninit，不必改动 FMS 主表。

#### 4.1.3 源码精读

FMS 的子模块表与正向初始化逻辑在 [`fms_module.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c)：

[g_sub_table 子模块表 — fms_module.c:23-30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c#L23-L30) 把 dtm、smf、fpdc、特性自动初始化四项排成数组，每项是一对 `init/uninit` 函数指针。这就是“表驱动装配”。

[init_fms_base 正向 init + 失败逆序回滚 — fms_module.c:32-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c#L32-L49) 顺序 `for` 调用 `init()`，任一返回非 0 即 `goto out`，从当前下标**逆序**调用 `uninit()`。卸载函数 `exit_fms_base` 同样逆序——保证初始化与销毁严格镜像。

dtm 的初始化在 [`dms_dtm_init.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/dtm/dms_dtm_init.c)：

[dms_init_dev_cb 建“容器” — dms_dtm_init.c:247-268](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/dtm/dms_dtm_init.c#L247-L268) 分配 `g_dms_system_ccb`，含一个 `base_cb` 和 `ASCEND_DEV_MAX_NUM` 个 `dev_cb_table[i]`，每个控制块初始化一把 `node_lock` 互斥锁和一条空的 `dev_node_list`。这是后续所有故障节点的“挂载点”。

[dms_dtm_init 入口 — dms_dtm_init.c:295-310](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/dtm/dms_dtm_init.c#L295-L310) 先 `dms_init_dev_cb()` 建容器，再（若启用 `CFG_FEATURE_DEVICE_STATE_TABLE`）从设备树解析状态表。

smf 的初始化极简，在 [`dms_smf_init.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/smf/dms_smf_init.c)：

[dms_smf_init — dms_smf_init.c:20-25](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/smf/dms_smf_init.c#L20-L25) 依次 `dms_event_init()`（事件框架）、`dms_sensor_init()`（传感器框架）。传感器框架启用后，才会周期性地去扫每个传感器的扫描函数。

#### 4.1.4 代码实践

> **实践目标**：验证 FMS 的「表驱动装配 + 逆序回滚」模式，并确认 soft_fault 经特性自动初始化进入。

1. 打开 [`fms_module.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c)，数一数 `g_sub_table` 有几项，注意 `fpdc_receiver_init` 被 `#ifndef CFG_EDGE_HOST` 包裹——说明 edge（边缘设备）形态下它不装配。
2. 在 [`soft_fault.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c) 文件末尾找到 `DECLAER_FEATURE_AUTO_INIT(soft_init, FEATURE_LOADER_STAGE_5)`（见 [soft_fault.c:753](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L753)），对照 [`pbl_feature_loader.h:102-110`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_feature_loader.h#L102-L110) 的宏展开，理解它实际生成了一个被 `EXPORT_SYMBOL` 的符号 `<模块名>_STAGE_5_init_soft_init`，由 `module_feature_auto_init()` 在阶段 5 调用。
3. **观察现象（待本地验证）**：若在真实环境 `lsmod | grep fms` 看到 `asdrv_fms`，且 `dmesg` 中出现 `dms_dtm_init start.` / `Dms driver init success.`（来自 [dms_dtm_init.c:298-308](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/dtm/dms_dtm_init.c#L298-L308)），再出现 `soft event driver init success.`（来自 [soft_fault.c:736](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L736)），即可印证 dtm → smf → soft_fault 的装配顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `g_sub_table` 里 `dms_smf_init` 与 `dms_dtm_init` 的顺序对调，会出什么问题？
**答**：会失败。dtm 建好的控制块（`dev_node_list` 等）是 smf 传感器/事件框架以及 soft_fault 注册节点的依赖前提；若 smf 先跑、dtm 后跑，smf 或 soft_fault 在注册节点时就找不到可挂载的容器，初始化失败并被逆序回滚。

**练习 2**：为什么 soft_fault 不直接写进 `g_sub_table`，而要走 `module_feature_auto_init`？
**答**：解耦。特性自动初始化让 soft_fault 这类「可选特性」用宏自注册，FMS 主表不必为每个特性改动；同时 `FEATURE_LOADER_STAGE_5` 保证了它在 dtm/smf 之后再加载，依赖顺序由 stage 编号隐式约束。

---

### 4.2 soft_fault 核心：事件队列与上报/扫描状态机

#### 4.2.1 概念说明

soft_fault 是 FMS 里实现「**软故障语义**」的核心。它的设计可以套用一个经典的「**生产者—消费者**」模型：

- **生产者**：各种故障源调用 `soft_fault_event_handler(&event)` 把一个故障事件塞进队列。
- **消费者**：传感器框架周期性地调用 `soft_fault_event_scan(private_data, data)`，把队列里的事件取走、上报给上层。

二者之间是一片**按「设备 → 用户 → 节点 → 子传感器」四级定位的事件队列**。同时，事件有生命周期——发生（OCCUR）后可能恢复（RESUME），也可能是一次性的（ONE_TIME）。soft_fault 用一张小巧的**状态表**管理「事件何时该被真正删掉」，以正确处理“恢复早于首次扫描”这种竞态。

#### 4.2.2 核心流程

先看数据结构层级（自顶向下逐层收窄）：

```
drv_soft_ctrl (全局 g_soft_ctrl)
  └─ s_dev_t[dev_id][user_id]  ──→ soft_dev_client*   (每个进程/每类用户一个)
                                   └─ head: soft_dev 链表  (按 node_type 区分的“节点”)
                                        └─ sensor_event_queue[sub_id]  (每个子传感器一条事件队列, struct soft_event)
                                             └─ error_list: soft_error_list 链表  (真正的故障事件条目)
```

其中 `user_id` 取值见 [`device_config.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/device_config.h)：`SF_SENSOR_DAVINCI=0`、`SF_SENSOR_OS=1`、`SF_SENSOR_DRV=2`、`SF_SENSOR_USER=3..64`（`SF_USER_MAX=65`），即把 OS、驱动内核、用户进程区分到不同的 user 槽位，互不干扰。

一次软故障的生命周期：

```
【上报 / 生产】
soft_fault_event_handler(event)
  ├─ 按 (dev_id, user_id, node_type, node_id, sub_id) 定位到 event_queue
  ├─ 若 assertion == RESUME  → is_soft_fault_recover(): 标记/删除匹配的既有故障
  └─ 否则（OCCUR / ONE_TIME）
       ├─ 分配 soft_error_list，memcpy 拷贝 event
       └─ soft_add_fault_event(): 按 (sensor_type, err_type) 去重后入队，report_status=INIT

【扫描 / 消费】  （传感器框架每 ~120ms 调一次）
soft_fault_event_scan(private_data, data)
  ├─ 从 private_data 解包出 (dev_id, user_id, node_type, node_id, idx)
  ├─ 定位 event_queue，遍历 error_list
  ├─ 把每个事件拷进 data->sensor_data[]，event_count++
  └─ 按状态表决定：删除该条目 / 仅置 report_status=SCANNED
```

状态表的关键：`report_status` 有三态 `INIT / SCANNED / RECOVERED`，分别表示“刚入队，还没被扫过”、“已被扫过一次”、“收到过恢复”。它解决的核心问题是——**当“恢复”比“首次扫描”先到时，这条故障仍应被上报一次再删除**，否则上层永远看不到这次短暂故障。

#### 4.2.3 源码精读

核心数据结构在 [`soft_fault_define.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault_define.h)：

[struct soft_fault — 软故障.c:116-127](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault_define.h#L116-L127) 是一个故障事件的完整描述：定位四元组（`dev_id`/`user_id`/`node_type`/`node_id`）+ `sub_id`（子传感器下标）+ `err_type`（具体错误码）+ `assertion`（OCCUR/RESUME/ONE_TIME）+ 附带数据。

[struct drv_soft_ctrl — soft_fault_define.h:171-175](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault_define.h#L171-L175) 是全局控制块的二维表 `s_dev_t[ASCEND_DEV_MAX_NUM][SF_USER_MAX]`，每个元素是一个 `soft_dev_client*`。

[enum EVENT_REPORT_STATUS — soft_fault_define.h:129-134](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault_define.h#L129-L134) 定义 `INIT/SCANNED/RECOVERED` 三态。

**上报入口** `soft_fault_event_handler` 在 [`soft_fault.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c)：

[soft_fault_event_handler — soft_fault.c:328-392](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L328-L392) 先做参数校验与定位（`soft_event_queue_get`），再分流：`assertion == GENERAL_EVENT_TYPE_RESUME` 走恢复分支 `is_soft_fault_recover`，否则分配 `soft_error_list` 并 `soft_add_fault_event` 入队。

[soft_add_fault_event 去重入队 — soft_fault.c:169-196](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L169-L196) 遍历队列，若已存在相同 `(sensor_type, err_type)` 的事件则不重复入队（仅当它处于 RECOVERED 时把状态复位为 INIT，等待重新扫描），否则追加到链表尾。这就是“同一故障不重复堆积”的去重逻辑。

**恢复判定** `is_soft_fault_recover`：

[is_soft_fault_recover — soft_fault.c:221-246](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L221-L246) 找到 `err_type` 相同且 `assertion==OCCUR` 的既有事件，依据 `check_error_report_status` 决定“立即删除”还是“标为 RECOVERED”。

**状态表** 是整个设计的点睛之笔：

[err_report_status_table — soft_fault.c:203-210](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L203-L210) 一张 3×3 的布尔表，行是事件当前 `report_status`，列是当前处理阶段。两个 `true`：
- `[SCANNED][RECOVERED] = true`：事件已被扫过、之后才恢复 → 恢复时直接删（已经上报过了）。
- `[RECOVERED][SCANNED] = true`：恢复先于首次扫描到达 → 扫描时**再上报一次然后删除**，避免漏报。这正是上方注释所解释的语义。

**扫描消费者** `soft_fault_event_scan`：

[soft_fault_event_scan — soft_fault.c:265-326](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L265-L326) 从 `private_data` 解包五元组定位队列，遍历 `error_list` 把事件拷到输出 `data->sensor_data[]`；对 `ONE_TIME` 事件或状态表判定要删除的事件直接摘链释放，其余置 `SCANNED`。注意 [soft_fault.c:318-320](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L318-L320) 用 `DMS_MAX_SENSOR_EVENT_COUNT` 限了单次扫描上报上限，超过则 `break`，剩余留给下一周期——这是背压（backpressure）。

`private_data` 的打包/解包用位运算把五个字段压进一个 `u64`：

[soft_combine_private_data — soft_fault.c:155-167](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L155-L167) 将 `dev_id`(16b) | `user_id`(16b) | `node_type`(16b) | `node_id`(8b) | `sensor_id`(8b) 拼成 64 位。这是因为传感器框架的扫描回调签名只给一个 `private_data` 透传字段——soft_fault 用它编码了完整的队列寻址信息，扫描时再 [解包还原](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L276-L280)。

#### 4.2.4 代码实践

> **实践目标**：用源码阅读型实践，亲手“演算”一次故障在状态表里的流转。

1. **场景设定**：假设某守护进程在 t=0ms 上报了一个 `err_type=0x05, assertion=OCCUR` 的故障；传感器框架的扫描周期是 [`SF_SENSOR_SCAN_TIME = 120ms`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/device_config.h#L21)；该故障在 t=50ms 恢复（上报 `assertion=RESUME`）。
2. **跟踪 t=0ms**：进入 [`soft_fault_event_handler`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L328-L392)，`assertion != RESUME`，于是 [`soft_add_fault_event`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L169-L196) 入队，`report_status = INIT`。
3. **跟踪 t=50ms**：进入 handler 的 RESUME 分支，调用 [`is_soft_fault_recover`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L221-L246)。此时事件 `report_status` 仍是 INIT（还没被扫过），查 [`状态表`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L203-L210) 的 `[INIT][RECOVERED] = false`，于是**不删除**，只把它标为 `RECOVERED`。
4. **跟踪 t=120ms 首次扫描**：进入 [`soft_fault_event_scan`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L265-L326)，事件被拷出上报一次；由于 `report_status == RECOVERED`，查表 `[RECOVERED][SCANNED] = true`，于是 [摘链释放](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L308-L313)。
5. **预期结果**：这次短暂故障被上报了恰好一次，然后从队列消失。若没有状态表，t=50ms 就直接删除，上层会**完全看不到**这次故障——这就是状态表存在的意义。

> 说明：以上是依据源码逻辑推演的控制流，实际 `dmesg` 是否能看到对应 `fault recover.` / `soft node update state success.` 日志取决于芯片形态与是否启用该 sensor，属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`soft_add_fault_event` 为什么要在入队前去重？如果不去重会怎样？
**答**：同一个 `(sensor_type, err_type)` 故障若在恢复前被多次 OCCUR 上报，会堆积多条重复条目，扫描时被重复上报，污染上层事件统计甚至触发误恢复。去重保证“同一种故障在队列里至多一条未决”。

**练习 2**：`soft_fault_event_scan` 里 `data->event_count == DMS_MAX_SENSOR_EVENT_COUNT` 时 `break`，剩余事件怎么处理？
**答**：剩余事件保留在队列里（状态未被改写或仅被置 SCANNED），等下一个 ~120ms 扫描周期继续取。这是一种背压机制，避免单次扫描占用过多时间/缓冲。

---

### 4.3 软故障的检测与恢复信号：os_reset 与 drv_kernel_soft

#### 4.3.1 概念说明

4.2 讲的是“故障进来后怎么存、怎么扫”，本讲回答“**故障从哪儿来、恢复信号怎么发**”。soft_fault 内置了两类典型生产者：

- **os_reset**（设备侧，`CFG_FEATURE_OS_INIT_EVENT`）：当**设备 OS 完成（重新）初始化**时，向主机故障框架上报一个“OS init”一次性事件。它本质是**故障恢复完成的信号**——主机据此知道设备 OS 已重新就绪，可以恢复对其的监控/重连。注意它不是“执行硬复位”的动作，而是“设备 OS 已（重）启动完成”的通知。
- **drv_kernel_soft**（`CFG_FEATURE_DRV_KERNEL_SOFT_EVENT`）：驱动内核态组件遇到**不可恢复的软故障**时，通过 `EXPORT_SYMBOL_GPL` 的接口 `hal_kernel_drv_soft_fault_report` 上报。这是“我这边出大事了、自己恢复不了”的求助信号。

两者都复用 4.2 的同一套队列与 handler，区别只在于：它们用不同的 `user_id`（`SF_SENSOR_OS` vs `SF_SENSOR_DRV`）、不同的 `node_type`（`HAL_DMS_DEV_TYPE_OS_LINUX` vs `HAL_DMS_DEV_TYPE_DRV_KERNEL`）和不同的 `err_type`（`OS_INIT=0x09` vs `SOFT_FAIL_CANNOT_RECOVER=0x0C`）。

#### 4.3.2 核心流程

**os_reset 的触发链（设备 OS 启动完成 → 通知主机）**：

```
设备 OS 启动完成
  └─ DMS 框架发出 DMS_H2D_EVENT
       └─ soft_notifier() (g_soft_notifier 回调)  ── soft_fault.c:539-560
            └─ soft_h2d_event(dev_id)
                 └─ os_device_notifier_func(dev_id)   ── os_reset.c:150-161
                      ├─ os_dev_register(dev_id)   注册 OS-Linux 节点 + 传感器
                      └─ os_reset_report(dev_id)   上报 OS_INIT 一次性事件
                           └─ soft_fault_event_handler()  (进入 4.2 的队列)
```

**drv_kernel_soft 的触发链（其他内核模块求助）**：

```
某内核模块检测到不可恢复软故障
  └─ hal_kernel_drv_soft_fault_report(dev_id)   (EXPORT_SYMBOL_GPL)
       ├─ drv_kernel_soft_fault_register(dev_id)  注册 DRV_KERNEL 节点 + 传感器
       └─ drv_kernel_soft_fault_report(dev_id)    上报 SOFT_FAIL_CANNOT_RECOVER
            └─ soft_fault_event_handler()         (进入 4.2 的队列)
```

#### 4.3.3 源码精读

os_reset 在 [`os_reset.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c)：

[os_device_notifier_func — os_reset.c:150-161](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c#L150-L161) 是 H2D 通知的落点：先注册 OS-Linux 节点，再上报 OS init 事件。

[os_dev_register — os_reset.c:79-133](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c#L79-L133) 分配并初始化一个 `soft_dev`，用 `os_dev_node_config` 配置其节点/传感器，再调 [`soft_register_one_node`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L414-L454) 注册进 DMS（注意它先注册 dev_node，再把 sensor 对象逐个注册，失败时逆序回滚——又一个“正向注册/失败逆序”实例）。

[os_reset_report — os_reset.c:26-56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c#L26-L56) 构造一个 `soft_fault`：`err_type = OS_INIT(0x09)`、`assertion = GENERAL_EVENT_TYPE_ONE_TIME`、附带字符串 `"OS init"`，然后交给 `soft_fault_event_handler`。因为是 `ONE_TIME`，扫描时会 [上报一次后立即删除](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L308-L309)，不占队列。

通知链注册在 soft_fault.c：`g_soft_notifier` 的 `.notifier_call = soft_notifier` 处理 `DMS_H2D_EVENT`（见 [soft_fault.c:539-560](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L539-L560)），在 [`soft_init`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L699-L753) 里经 `dms_register_notifier` 挂上。

drv_kernel_soft 在 [`drv_kernel_soft.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.c)：

[hal_kernel_drv_soft_fault_report — drv_kernel_soft.c:153-173](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.c#L153-L173) 是对外导出接口，支持单设备或“所有设备”（`DRV_SOFT_FAULT_REPORT_ALL_DEV`）批量上报；先 `drv_kernel_soft_fault_register` 注册节点，再 `drv_kernel_soft_fault_report` 上报。

[drv_kernel_soft_fault_report — drv_kernel_soft.c:124-151](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.c#L124-L151) 构造 `soft_fault`：`err_type = SOFT_FAIL_CANNOT_RECOVER(0x0C)`、`assertion = GENERAL_EVENT_TYPE_OCCUR`（与 OS init 的 ONE_TIME 不同，这是持续故障，需要配对 RESUME 才会消除）。

两类特性是否编译由 `soft_fault.mk` 按 `PRODUCT` 开关控制：

[soft_fault.mk 特性宏 — soft_fault.mk:45-61](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.mk#L45-L61) 对 `ascend910B` 与 `ascend950` 均开启 `CFG_FEATURE_OS_INIT_EVENT` 与 `CFG_FEATURE_DRV_KERNEL_SOFT_EVENT`，于是 os_reset.c 和 drv_kernel_soft.c 的关键代码段（被这两个宏包裹）才被编进 `.ko`。这正是 u3-l5 讲过的「编译期特性宏冻结能力」在内核侧的体现。

#### 4.3.4 代码实践

> **实践目标**：对比两类内置故障源，理解它们在“恢复语义”上的差异。

1. 打开 [`os_reset.c:42-48`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c#L42-L48)，记录 `os_reset_report` 用的 `assertion = GENERAL_EVENT_TYPE_ONE_TIME`、`err_type = OS_INIT`。
2. 打开 [`drv_kernel_soft.c:140-147`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.c#L140-L147)，记录 `drv_kernel_soft_fault_report` 用的 `assertion = GENERAL_EVENT_TYPE_OCCUR`、`err_type = SOFT_FAIL_CANNOT_RECOVER`。
3. **需要观察的现象**：
   - OS init 是 **ONE_TIME**：扫描时上报一次即删，表示“一次性的事件通知”（设备 OS 起来了）。
   - drv kernel soft 是 **OCCUR**：会持续留在队列里，直到收到对应的 RESUME（或节点注销时随 [`soft_fault_event_free`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L394-L412) 清理），表示“一个尚未解除的故障状态”。
4. **预期结论**：ONE_TIME 适合“状态变迁通知”，OCCUR/RESURE 适合“持续故障的生命周期”。两者复用同一队列，靠 `assertion` 区分语义。
5. 若无法在本地触发，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`os_reset_report` 为什么用 `ONE_TIME` 而不是 `OCCUR`？
**答**：设备 OS 启动完成是一个“一次性事件”，不存在“恢复”的对端语义；用 ONE_TIME 让它在被扫描上报一次后自动从队列删除，不残留。若用 OCCUR，它会一直留在队列里等待一个永远不会到来的 RESUME。

**练习 2**：`hal_kernel_drv_soft_fault_report` 为什么要用 `EXPORT_SYMBOL_GPL` 导出？
**答**：因为它供**其他内核模块**（如发现不可恢复故障的驱动子模块）调用，跨 `.ko` 调用必须导出符号；`EXPORT_SYMBOL_GPL` 表示该符号遵循 GPL 许可，仅 GPL 兼容模块可用，符合昇腾驱动的许可策略。

---

### 4.4 用户态上报入口与白名单访问控制：dms_sensor_interface

#### 4.4.1 概念说明

4.3 的两类生产者是**内核内部**自产自销。但还有很多软故障来自**用户态守护进程**（如 `iammgr`、`process-manager`、`tsdaemon`、`hccp_service.bin`）。这些进程需要一种手段：注册自己的传感器节点、上报故障值。这条通道就是 `dms_sensor_interface.c` 提供的 **3 个 URD 命令**。

然而，让任意用户态进程都能往内核故障框架里“灌”故障是危险的——恶意或越权进程可以伪造故障、触发不必要的设备恢复甚至复位。因此近期提交「**Add soft_fault whitelist**」引入了一张**白名单**：只有「**特定进程名 + 特定故障类型 + 特定用户角色 + 特定运行环境**」四维全部命中的上报才被接受。本节就把这条「用户态 ioctl → URD → handler → 白名单 → 队列」的链路讲透。

#### 4.4.2 核心流程

```
用户态进程 ioctl(/dev/davinci_manager, ...)
  └─ URD 按 (DMS_MAIN_CMD_SOFT_FAULT, sub_cmd) 派发
       ├─ DMS_SUBCMD_SENSOR_NODE_REGISTER   → soft_node_register
       ├─ DMS_SUBCMD_SENSOR_NODE_UNREGISTER → soft_node_unregister
       └─ DMS_SUBCMD_SENSOR_NODE_UPDATE_VAL → soft_node_update_state

soft_node_register / soft_node_update_state 的统一前置检查：
  1. 参数校验（结构体长度、name 长度、node_type/sensor_type 合法性）
  2. soft_trans_and_check_id: 逻辑 devid → 物理 phy_id（UDA 翻译）
  3. 白名单检查（见下）
  4. 通过后才进入 4.2 的注册/上报逻辑

白名单检查 soft_fault_whitelist_check(handle, val):
  1. soft_fault_get_whitelist: 按芯片型号(950+ vs 老芯片)选表
  2. SOFT_PARSE_HANDLE: 把 64 位 handle 拆成 (node_type, sensor_idx, sensor_type)
  3. 按 (node_type, sensor_type, err_type=val) 在表里查 fault_index
  4. 三维校验，全部 AND 通过才放行：
       - 用户角色：root / HwDmUser / HwHiAiUser组 / 普通用户
       - 运行环境：物理机 / 虚拟机 / docker / admin docker
       - 进程名：与白名单指定进程名严格匹配（空串表示不限制）
```

#### 4.4.3 源码精读

3 个命令处理函数与命令表声明都在 [`dms_sensor_interface.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c)：

[命令表声明 — dms_sensor_interface.c:930-954](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L930-L954) 用 `BEGIN_DMS_MODULE_DECLARATION(soft_fault)` + 三条 `ADD_FEATURE_COMMAND` 把 register/unregister/update_state 三个处理函数注册成 URD 命令。对照 [`pbl_urd.h:60-110`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/pbl/pbl_urd.h#L60-L110)，这套宏在 `init_module_soft_fault()`（由 [`soft_init`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L703) 里 `CALL_INIT_MODULE(soft_fault)` 触发）时把命令逐条 `dms_feature_register` 进 URD。

[soft_node_update_state — dms_sensor_interface.c:813-850](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L813-L850) 是用户态上报故障值的主入口：校验 → `soft_trans_and_check_id` 翻译设备号 → `soft_fault_whitelist_check` → `dms_update_sensor_state` → 最终走到 [`soft_fault_event_handler`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L328-L392) 入队。

[soft_node_register — dms_sensor_interface.c:727-779](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L727-L779) 注册流程类似，但它做的是 `soft_sensor_whitelist_check`（只按 node_type + sensor_type 匹配），并在成功后返回一个 64 位 `handle` 给用户态作为后续操作的凭证。handle 编码见 [user_dev_node_register:419-420](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L419-L420)：高 32 位 node_type、中 16 位 sensor_idx、低 16 位 sensor_type，与 [`SOFT_PARSE_HANDLE`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L28-L33) 严格互逆。

**白名单机制**：

[白名单表与选表 — dms_sensor_interface.c:46-65](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L46-L65) 定义两份表：`g_soft_fault_whitelist`（来自 [`soft_fault_whitelist.inc`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist.inc)，仅 950 及以后）与 `g_soft_fault_whitelist_legacy`（来自 `soft_fault_whitelist_legacy.inc`，950 之前）。`soft_fault_get_whitelist` 用 `uda_get_chip_type(devid)` 在 `HISI_CLOUD_V4/V5` 时选新表，否则选老表。

[白名单条目结构 — drv_kernel_soft.h:46-53](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.h#L46-L53) `struct soft_fault_acc` 六字段：`proc_name`/`node_type`/`sensor_type`/`err_type`/`user_acc`/`run_env`。后两个是位掩码，定义在同文件 [drv_kernel_soft.h:20-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/drv_kernel_soft.h#L20-L44)：`SOFT_FAULT_ACC_ROOT/OPERATE/DM_USER/USER`、`SOFT_FAULT_ENV_PHYSICAL/VIRTUAL/DOCKER/ADMIN_DOCKER`。

[白名单三维校验 — dms_sensor_interface.c:173-218](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L173-L218) `soft_fault_whitelist_check` 依次做：查 fault_index → `soft_fault_get_cur_user_role`（按 `cred->euid/egid` 判定 root/DM/operate/user）→ `soft_fault_get_cur_run_env`（物理/虚机/docker/admin docker）→ `soft_fault_get_cur_process_name`（读 `current->active_mm->exe_file` 的可执行名）。三者与白名单条目的对应位掩码逐项 AND，任一不匹配即拒绝。

来看真实白名单内容（节选）：

[soft_fault_whitelist.inc 表项 — inc/fms/soft_fault_whitelist.inc:26-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist.inc#L26-L49) 例如 `{"tsdaemon", 0x60B, 0xD0, 0x05, SOFT_FAULT_ACC_OPERATE, SOFT_FAULT_ENV_PHYSICAL}` 表示：只有可执行名为 `tsdaemon`、在物理机环境、以 operate 角色（HwHiAiUser/HwBaseUser 组）运行的进程，才允许对 node_type=0x60B、sensor_type=0xD0 上报 err_type=0x05 的故障。可以看到这些被允许的进程都是昇腾系统组件（`process-manager`、`iammgr`、`tsdaemon`、`hccp_service.bin`、`proc_launcher` 等），普通业务进程一概被拒。

#### 4.4.4 代码实践

> **实践目标**：结合近期提交「Add soft_fault whitelist」，理解白名单作为“安全门”的必要性，并能在表里查证一条规则。

1. 用 `git show e29d066 --stat` 复核该提交确实引入了白名单相关文件（提交信息「Add soft_fault whitelist」）。
2. 打开 [`dms_sensor_interface.c:843-847`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L843-L847)，确认 `soft_node_update_state` 在进入真正的 `dms_update_sensor_state` **之前**先调用 `soft_fault_whitelist_check`，不通过直接返回 `-EINVAL`。这就是“门”的位置。
3. **思考题演算**：假设有一个普通业务进程 `my_app`（非 root、HwHiAiUser 组），尝试上报 `{node_type=0x60B, sensor_type=0xD0, err_type=0x05}`。
   - 查 [白名单表](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist.inc#L37-L38)：这条规则的 `proc_name = "tsdaemon"`。
   - `soft_fault_check_proc_name("my_app", "tsdaemon")` 返回 false（[dms_sensor_interface.c:158-171](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L158-L171)）。
   - 结论：上报被拒，`dmesg` 会打印 `Check process name fail.`。
4. **预期结果**：白名单把“谁能报什么故障”收窄到一组确定的系统守护进程，阻止越权/伪造故障注入。这是软故障通道从“任意可写”到“按需放行”的关键加固。
5. 该实践为源码阅读推演，实际触发需在设备环境且具备对应芯片，属「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：白名单为什么分 `whitelist`（950+）和 `whitelist_legacy`（950 之前）两份？
**答**：不同代际芯片上的系统组件构成、故障类型编码可能不同（例如 legacy 表多了 `aicpu_scheduler` 的几条 [0x09/0x0A/0x0B](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist_legacy.inc#L41-L43)，且 iammgr 的角色从 ROOT 放宽到 OPERATE）。运行时用 `uda_get_chip_type` 选表，保证一份二进制在多芯片上都用对的规则。

**练习 2**：`handle` 为什么要在 register 时返回给用户态、update 时再传回来？
**答**：`handle` 是一个 64 位“凭证”，把 `(node_type, sensor_idx, sensor_type)` 压进一个整数。用户态持有它，后续 update/unregister 时无需再传完整三元组，只传 handle 即可；内核侧用 `SOFT_PARSE_HANDLE` 还原。这减少了用户态/内核态的参数拷贝，也隐藏了内部 sensor 表的下标细节。

---

## 5. 综合实践

把本讲四个模块串成一条完整的软故障端到端链路。请阅读源码后，**绘制一张时序/数据流图**并配文字说明，覆盖以下场景：

> **场景**：一张 NPU 卡上，设备 OS 刚刚完成一次软重启；同时主机侧的 `tsdaemon` 守护进程检测到一个调度软故障并上报；几秒后该调度故障恢复。

要求你的图与文字覆盖：

1. **装配阶段**：`asdrv_fms.ko` 加载时，dtm 建控制块、smf 建传感器/事件框架、特性自动初始化加载 soft_fault（引用 [fms_module.c:23-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/fms_module.c#L23-L49) 与 [soft_fault.c:699-753](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L699-L753)）。
2. **OS init 事件**：设备 OS 启动完成 → DMS_H2D_EVENT → `os_device_notifier_func` → 注册节点 + `os_reset_report`（ONE_TIME）→ `soft_fault_event_handler` 入队 → 扫描上报一次即删（引用 [os_reset.c:150-161](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/os_reset.c#L150-L161) 与 [soft_fault.c:265-326](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L265-L326)）。
3. **tsdaemon 上报**：`tsdaemon` 经 ioctl/URD 调 `soft_node_update_state` → 白名单三维校验通过（`proc_name="tsdaemon"` 命中）→ `dms_update_sensor_state` → `soft_fault_event_handler`（OCCUR）入队（引用 [dms_sensor_interface.c:813-850](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L813-L850) 与 [白名单表](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/inc/fms/soft_fault_whitelist.inc#L37-L38)）。
4. **恢复**：tsdaemon 上报 RESUME → `is_soft_fault_recover` → 依据状态表决定标 RECOVERED 或删除（引用 [soft_fault.c:221-246](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L221-L246) 与 [状态表](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L203-L210)）。
5. **异常退出兜底**：若 tsdaemon 在恢复前异常崩溃，进程退出触发 profile task-exit 通知 → `soft_fault_release_prepare` → `soft_client_release` 自动回收它注册的节点/传感器/事件，避免泄漏（引用 [soft_fault.c:562-588](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/soft_fault.c#L562-L588) 与 [dms_sensor_interface.c:897-928](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/fms/soft_fault/dms_sensor_interface.c#L897-L928)）。

完成后再回答一个综合问题：**为什么 OS init 用 ONE_TIME、tsdaemon 的调度故障用 OCCUR/RESUME，而白名单只挡后者不挡前者？** （提示：前者是内核内部可信源经 DMS 通知链触发的，后者是用户态进程经 ioctl 主动上报的，必须过访问控制。）

## 6. 本讲小结

- **FMS = `asdrv_fms.ko`**，用「子模块表 + 正向 init/失败逆序回滚」装配：`dtm` 建“设备控制块容器”，`smf` 建“传感器/事件调度框架”，`fpdc` 收故障数据，`module_feature_auto_init` 按 stage 加载 soft_fault 等特性子模块。
- **soft_fault 的数据层级**是 `设备 → 用户(client) → 节点(soft_dev) → 子传感器事件队列(soft_event) → 故障条目(soft_error_list)`，用 `(dev_id, user_id, node_type, node_id, sub_id)` 五元组精确定位。
- **生产者 `soft_fault_event_handler`** 负责 OCCUR/ONE_TIME 入队（带去重）与 RESUME 恢复；**消费者 `soft_fault_event_scan`** 被传感器框架每 ~120ms 调用、把事件上报并按状态表决定删除时机——3×3 状态表解决了“恢复早于首次扫描”的漏报竞态。
- **两类内置故障源**：`os_reset` 上报设备 OS（重）初始化完成的 ONE_TIME 事件（恢复就绪信号），`drv_kernel_soft` 经 `EXPORT_SYMBOL_GPL` 上报驱动内核态不可恢复软故障（OCCUR）；二者由 `CFG_FEATURE_*` 编译期宏按芯片开关。
- **用户态入口 `dms_sensor_interface`** 提供 register/unregister/update_state 三个 URD 命令；近期「Add soft_fault whitelist」提交引入按「进程名 + 故障类型 + 用户角色 + 运行环境」四维的白名单，把软故障上报收窄到一组确定的系统守护进程，是必要的访问控制安全门。
- 全链路用通知链（H2D 事件、task-exit、URD release）打通“检测—上报—扫描—恢复—回收”，进程异常退出时也能自动释放其注册的故障节点，不泄漏。

## 7. 下一步学习建议

- **沿 smf 往事件分发走**：本讲只到“事件被 scan 出队”，这些事件随后进入 `src/sdk_driver/fms/smf/event/`（`dms_event_distribute.c`、`dms_event_converge.c`）做分发与收敛，建议阅读它们理解“事件出去之后被谁消费、如何聚合”。
- **看 sensor 框架的扫描调度**：`src/sdk_driver/fms/smf/sensor/` 与 `smf/core/dms_sensor_init.c` 解释了 ~120ms 扫描周期是如何注册与驱动的，能补全 soft_fault_event_scan 的“谁来调”这一环。
- **对比 u6-l3/u6-l4 的任务调度**：你会看到 trsdrv、ts_agent、FMS 都用同一套「子模块表 + 逆序回滚 + 通知链 + EXPORT_SYMBOL」内核工程手法，掌握这套范式后阅读任何昇腾内核模块都能快速上手。
- **回到用户态视角**：可对照 u5 系列（DMC 的 device_monitor、logdrv、prof）理解“内核产生的事件/日志如何被搬到用户态工具（如 msnpureport）展示”，形成内核产生→用户消费的完整闭环。
