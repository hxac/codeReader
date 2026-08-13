# DSMI 设备系统管理接口实现

## 1. 本讲目标

本讲聚焦 DSMI（Device System Management Interface，设备系统管理接口）**实现层**。上一讲（u2-l1）我们理解了 DCMI 的初始化与缓存探测；本讲顺着同一套思路往下走一层，回答以下问题：

- 用户调用一个 `dsmi_*` 查询接口后，代码在 `ascend_hal` 用户态库内部是如何一步步执行的？
- `dsmi_common_interface.c`、`dsmi_dmp_command.c`、`dsmi_common.c` 这三个文件分别承担什么职责，为什么要把 DSMI 拆成这三块？
- 什么是 DMP（Device Management Protocol）命令？`dsmi_cmd_*` 系列函数为何看起来都长得几乎一样？
- `drvGetDevInfo` 这类 helper 是怎么获取设备信息的？它和 DMP 命令是什么关系？

学完后，你应当能够：读懂任意一个 `dsmi_*` 查询接口的实现，说出它的数据来自「Host 本地 ioctl」还是「设备固件往返」，并能画出从用户调用到设备再返回的完整调用链。

## 2. 前置知识

### 2.1 回顾：DSMI 在三层架构中的位置

在 u1-l1 建立的心智模型里，`driver` 仓分为 DCMI / HAL / SDK-driver 三层。DSMI 属于 HAL 层（编译进用户态动态库 `libascend_hal.so`），与 `hal_*` 接口并列，但面向的是「设备系统管理」类调用者（如 `npucli`、监控运维工具），而不是计算运行时。它的接口声明集中在 `pkg_inc/dsmi_common_interface.h`，命名是 `snake_case`，返回值统一是 `int`，成功为 `0`（详见 u1-l5）。

### 2.2 两种「设备信息」从哪里来

理解本讲最关键的一个直觉是：**不是所有设备信息都要去问设备固件**。一条 `dsmi_*` 查询拿到结果，数据可能来自两个完全不同的地方：

1. **Host 本地的设备管理器（Device Manager）**。昇腾内核驱动在加载时已经把芯片型号、AI Core 数、PCIe 拓扑、设备数量等信息登记在内核侧。用户态通过 `ioctl` 陷入内核即可直接读到，**不需要往返设备固件**，速度快、开销小。代表 helper：`drvGetDevInfo`、`drvGetDevNum`。
2. **设备固件（Device Firmware）往返**。温度、健康状态、功耗、传感器读数等「实时运行态」数据只存在于设备侧，必须把一条命令发到设备、等设备处理后把结果带回来。这条往返通道就是 DMP 命令，底层走 HDC（或 UDP / IAM）通信链路。

本讲要讲清的就是：DSMI 实现如何在这两条路径之间做分派，以及第二条路径上的 DMP 命令是如何构造、发送、回收的。

### 2.3 几个术语

| 术语 | 含义 |
|------|------|
| DMP | Device Management Protocol，DSMI 用来和设备固件交互的应用层报文协议 |
| opcode | 操作码，一个 `unsigned short`，编码了「功能码高字节 + 命令低字节」 |
| HDC | Host-Device Communication，主机与设备间的事件驱动通信底座（详见 u3-l2）|
| `drvError_t` | 驱动通用错误码类型，`DRV_ERROR_NONE`(0) 表示成功，DSMI 复用同一套 `DRV_ERROR_*` |
| ioctl | 用户态陷入内核态的系统调用机制 |

## 3. 本讲源码地图

本讲涉及的源码文件及其作用：

| 文件 | 所属最小模块 | 作用 |
|------|------------|------|
| `pkg_inc/dsmi_common_interface.h` | （公共头）| 对外声明所有 `dsmi_*` 接口、定义 DSMI 用到的结构体、错误码映射、DMP 报文结构、主/子命令枚举 |
| `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c` | dsmi_common_interface | DSMI 对外 API 的**实现门面**：参数校验 + 路径分派 |
| `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_dmp_command.c` | dsmi_dmp_command | `dsmi_cmd_*` 系列函数：用宏 DSL 构造并发送 DMP 命令、回收结果 |
| `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c` | dsmi_common | 通信基础设施：`dmp_command_init`、`dsmi_send_msg_rec_res`、通道初始化、`drvGetDevInfo` 等 helper 的调用点 |
| `src/ascend_hal/dmc/dsmi/include/dsmi_common.h` | （内部头）| 定义 `DM_COMMAND_*` 宏 DSL 与命令表实例化宏 |
| `src/ascend_hal/dmc/dsmi/include/dsmi_cmd_info_def.h` | （内部头）| 用宏批量实例化 DMP 命令表 `g_dmp_cmd_def_*` |
| `src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c` | （设备管理器）| `drvGetDevInfo` 的定义：通过 ioctl 从内核读设备信息 |

> 提示：`drvGetDevInfo` 虽然被 DSMI 大量调用，但它的**定义**不在 `dsmi/` 目录下，而在 `dms/`（Device Management System）的设备管理器里。这是本讲一个容易让人困惑的点——调用方和定义方分属两个模块，我们在 4.3 节专门解释。

## 4. 核心概念与源码讲解

### 4.1 dsmi_common_interface：对外 API 门面与路径分派

#### 4.1.1 概念说明

`dsmi_common_interface.c` 是 DSMI 对外暴露的 `DLLEXPORT int dsmi_*()` 函数集合的**实现入口**。它与头文件 `dsmi_common_interface.h` 一一对应：头文件里声明了多少个 `dsmi_*`，这个 `.c` 里就实现了多少个。

这个文件的职责被刻意限制得很薄，每个函数通常只做三件事：

1. **参数校验**：空指针检查、`device_id` 合法性、缓冲区长度等。校验失败立即返回 `DRV_ERROR_PARA_ERROR`。
2. **路径分派**：决定这条查询走「本地 ioctl」还是「DMP 往返」，并调用对应的 helper。
3. **错误码归一**：把下层返回的 `drvError_t` 透传或转换后返回给调用者。

把复杂度下放到 helper（`drvGetDevInfo`、`dsmi_cmd_*`、`udis_*`），让门面层保持「薄而整齐」，是这套代码最显眼的风格。这样做的好处是：上百个查询接口长得很像，阅读时只需抓住「校验 → 分派 → 返回」三段式即可。

#### 4.1.2 核心流程

一个典型的「本地路径」查询（如获取设备数量）流程：

```
dsmi_get_device_count(device_count)
   ├── 1. 校验 device_count != NULL
   ├── 2. 调用 drvGetDevNum(device_count)   // 本地 ioctl，不往返设备
   └── 3. 处理返回值：0 成功；否则按错误码返回
```

一个典型的「DMP 往返路径」查询（如获取设备健康状态）流程：

```
dsmi_get_device_health(device_id, phealth)
   ├── 1. 校验参数
   └── 2. 调用 dsmi_cmd_get_device_health(device_id, phealth)
              └── （见 4.2，构造并发送 DMP 命令，等待设备回包）
```

还有一些接口会**先尝试本地/缓存路径，失败再回退到 DMP 往返**，例如获取功耗信息会先查 `udis_*`（统一设备信息服务）缓存，未命中再下发 DMP 命令。这是为了在数据可得时尽量避免昂贵的设备往返。

#### 4.1.3 源码精读

**例 1：本地路径——获取设备数量。** `dsmi_get_device_count` 是最简单的门面函数，校验指针后直接调用 `drvGetDevNum`：

[dsmi_common_interface.c:147-175](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L147-L175) — 校验 `device_count` 非空，调用 `drvGetDevNum` 取设备数；对 `DRV_ERROR_RESOURCE_OCCUPIED`（设备被占用）单独打日志，并对「返回 0 但设备数为 0」的异常情况映射为 `DRV_ERROR_INNER_ERR`。这段体现了门面层「校验 + 分派 + 错误码归一」的标准写法。

**例 2：本地路径——获取 PCIe 信息。** `dsmi_get_pcie_info` 用 `drvGetDevInfo` 取设备上下文，再用 `drvDeviceGetPcieIdInfo` 取 BDF 地址：

[dsmi_common_interface.c:588-613](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L588-L613) — 先 `drvGetDevInfo(device_id, &dev_info)`（注意 `dev_info` 此处用于「探活 + 取上下文」，设备被占用时直接返回 `DRV_ERROR_RESOURCE_OCCUPIED`），再调用 `drvDeviceGetPcieIdInfo` 把厂商/设备 ID 与 BDF 三元组填入 `tag_pcie_idinfo`。注意整段被 `#ifndef CFG_SOC_PLATFORM_RC` 包裹——在 RC（Root Complex，通常指设备做主机）形态下此接口返回 `DRV_ERROR_NOT_SUPPORT`，说明同一段代码在不同产品形态下行为不同。

**例 3：DMP 往返路径——获取设备健康状态。** 这是「本地不可得、必须问设备」的典型，门面层把活儿全交给 `dsmi_cmd_get_device_health`：

[dsmi_common_interface.c:563-586](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L563-L586) — `dsmi_get_device_power_info` 展示了「先缓存后回退」的混合策略：先 `udis_get_lp_info(device_id, "power_limit", ...)` 查本地缓存，命中则直接返回；未命中（`ret != 0`）才调用 `dsmi_cmd_get_device_power_info` 走 DMP 往返。整段还被 `#ifdef CFG_FEATURE_POWER` 包裹，未开启该特性时返回 `DRV_ERROR_NOT_SUPPORT`。

> 关键观察：门面层几乎不直接碰通信细节。通信由 `dsmi_cmd_*`（DMP 路径）和 `drvGetDevInfo`（本地路径）封装，门面只负责「选哪条路 + 校验 + 报错」。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍门面层的「三段式」，并区分两条数据路径。

**操作步骤**：

1. 打开 `dsmi_common_interface.c`，定位 `dsmi_get_device_count`（约第 147 行）。
2. 在同文件中用搜索找到 `dsmi_get_pcie_info`（约第 588 行）和 `dsmi_get_device_power_info`（约第 563 行）。
3. 再搜索 `int dsmi_get_device_temperature`，观察它调用的是 `dsmi_cmd_*` 还是 `drvGetDevInfo`。

**需要观察的现象**：

- 这几个函数的前几行几乎都是 `if (xxx == NULL) { ...; return DRV_ERROR_PARA_ERROR; }`，这就是统一的参数校验。
- 它们调用的 helper 分成两派：`drvGetDevInfo` / `drvGetDevNum` / `drvDeviceGetPcieIdInfo`（本地派）与 `dsmi_cmd_*`（DMP 派）。
- 部分函数被 `#ifdef CFG_FEATURE_*` 或 `#ifndef CFG_SOC_PLATFORM_RC` 包裹。

**预期结果**：你能把任一 `dsmi_*` 查询归入「本地路径」「DMP 路径」「混合路径（先本地后 DMP）」三类之一。本实践为纯源码阅读，无需硬件，可立即完成。

#### 4.1.5 小练习与答案

**练习 1**：`dsmi_get_device_count` 为什么不需要 `device_id` 参数，而 `dsmi_get_pcie_info` 需要？

> **答案**：设备数量是「整机/整机总线」属性，属于 Host 侧已知的全局信息，由 `drvGetDevNum` 直接从设备管理器读出，不针对某个具体设备；而 PCIe 信息是「每设备」属性，必须指明 `device_id` 才能定位到具体那张卡。

**练习 2**：`dsmi_get_device_power_info` 里 `udis_get_lp_info` 返回成功（`ret == 0`）时，还会不会下发 DMP 命令？

> **答案**：不会。代码在 `ret == 0` 时填好结构体并 `return 0` 直接返回，只有缓存未命中（`ret != 0`）才会走到 `dsmi_cmd_get_device_power_info`。这是一种「能用缓存就不打扰设备」的优化。

---

### 4.2 dsmi_dmp_command：DMP 命令的构造与收发（宏 DSL）

#### 4.2.1 概念说明

当数据必须从设备固件拿时，门面层会调用一个 `dsmi_cmd_*` 函数。这些函数全部集中在 `dsmi_dmp_command.c`，它们是 DSMI 与设备固件对话的「翻译官」：把一次高层查询翻译成一条 DMP 报文，发出去，等回包，再把回包里的字节拆回成 C 结构体。

这个文件最值得学习的地方，是它用一组宏定义了一套**迷你领域专用语言（DSL）**。每个 `dsmi_cmd_*` 函数的函数体不是普通的 C 语句，而是几个宏的固定排列：

```c
int dsmi_cmd_xxx(...)
{
    DM_COMMAND_BIGIN(命令名, device_id, 入参总长, 出参总长)   // 初始化 + 探活 + 分配报文
    DM_COMMAND_ADD_REQ(&入参, sizeof(入参))                    // 往请求报文里塞字段
    DM_COMMAND_SEND()                                          // 发送并等回包
    DM_COMMAND_PUSH_OUT(出参缓冲, sizeof(出参))                // 从回包里取字段
    DM_COMMAND_END()                                           // 释放资源并 return OK
}
```

这套宏的精妙之处在于：它把「分配报文 → 填字段 → 发送 → 收包 → 取字段 → 释放」这条繁琐流程封装成接近「声明式」的写法，于是上百个命令函数都能写得又短又整齐，新增一个命令几乎就是照着模板填空。这正是上一讲 DCMI、以及后续接口扩展（u2-l3「新增 DSMI 接口」）能高效开发的原因。

#### 4.2.2 核心流程

一条 DMP 命令的完整生命周期：

```
dsmi_cmd_get_device_health(device_id, phealth)
   │
   ├─ DM_COMMAND_BIGIN(...)
   │    ├─ 校验入参/出参长度
   │    ├─ dsmi_check_device_id(device_id)          // device_id 合法性
   │    ├─ drvGetDevInfo(device_id, &dev_info)      // 探活：设备是否在线（见 4.3）
   │    └─ dmp_command_init(device_id, opcode, ...) // 分配并初始化报文（见 4.3）
   │
   ├─ DM_COMMAND_SEND()
   │    └─ dsmi_send_msg_rec_res(dm_dmp)            // 经 HDC 发往设备，等回包（见 4.3）
   │
   ├─ DM_COMMAND_PUSH_OUT(phealth, sizeof(...))     // 把回包字节拷进 phealth
   │
   └─ DM_COMMAND_END()
        └─ dsmi_cmd_req_free(dm_dmp); return OK;    // 释放报文，返回成功
```

报文本身的结构在公共头里定义。请求与响应都是「定长头 + 变长 data」：

- 请求头 `DSMI_CMD_CODE` 含 `lun / optype / opcode / offset / length`，后跟 `send_data`；
- 响应头 `DSMI_DFT_RES_CMD` 含 `errorcode / opcode / total_length / length`，后跟 `response_data`。

`opcode` 是报文的「身份证」。它由命令表（见 4.2.3）按 `功能码高字节 << 8 | 命令号` 拼成，例如 `DEV_MON_CMD_GET_HEALTH_STATE` 的 opcode = `DEV_MON_SMB_FUN_CODE_COMMON << 8 | DEV_MON_CMD_GET_HEALTH_STATE`。设备固件拿到 opcode 就知道要做什么。

#### 4.2.3 源码精读

**先看宏定义本体。** `DM_COMMAND_BIGIN` 是整套 DSL 的核心，它在展开后会声明一批局部变量、做校验、调用 `drvGetDevInfo` 探活、再调用 `dmp_command_init` 分配报文：

[dsmi_common.h:427-470](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/include/dsmi_common.h#L427-L470) — 注意第 451 行 `ret = (int)drvGetDevInfo(...)`：**每一条 DMP 命令在构造前都会先调用 `drvGetDevInfo` 做一次「设备在线探活 + 取上下文」**。这是本讲最关键的一个连接点——`drvGetDevInfo`（本地路径）和 DMP 命令（往返路径）不是二选一，而是「先探活，再往返」的前后关系。设备被占用时返回 `DRV_ERROR_RESOURCE_OCCUPIED`，直接拦截，不会发出无谓的报文。

**发送宏。** `DM_COMMAND_SEND` 把「发送 + 等回包」收敛成一行：

[dsmi_common.h:492-500](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/include/dsmi_common.h#L492-L500) — 调用 `dsmi_send_msg_rec_res(dm_dmp)`，失败时 `dsmi_cmd_req_free` 释放报文并返回错误码。注意对 `DRV_ERROR_NOT_EXIST` 不打 NOTSUPPORT 告警——这是为了避免「设备不存在」这类高频正常情况淹没日志。

**命令表是如何生成的。** `g_dmp_cmd_def_<命令名>` 这个变量并非手写，而是由宏 `DSMI_CMD_DEF_COMMON_INSTANCE` 批量实例化：

[dsmi_common.h:35-42](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/include/dsmi_common.h#L35-L42) — 每个命令声明一个静态 `DMP_CMD_DEF`，含 `name / opcode / length / resp_len / op_type`；opcode 由 `DEV_MON_SMB_FUN_CODE_COMMON << 8 | (命令号)` 拼接。`dsmi_cmd_info_def.h` 里集中罗列了全部命令实例：

[dsmi_cmd_info_def.h:20-40](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/include/dsmi_cmd_info_def.h#L20-L40) — 例如 `DEV_MON_CMD_GET_HEALTH_STATE` 的入参长 0、响应长 `sizeof(unsigned char)`、类型 `STATE_MANAGE_TYPE`。这就是「表驱动 + 宏」的设计：改一条命令的元信息只需改一行表。

**一个最简单的命令函数。** `dsmi_cmd_get_device_health` 无入参、出参 1 字节，函数体只有四行宏：

[dsmi_dmp_command.c:54-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_dmp_command.c#L54-L60) — `DM_COMMAND_BIGIN(DEV_MON_CMD_GET_HEALTH_STATE, device_id, 0, sizeof(unsigned char))` 用命令表里的 opcode 初始化报文；`DM_COMMAND_SEND()` 发包等回包；`DM_COMMAND_PUSH_OUT(phealth, sizeof(unsigned char))` 把回包里的健康字节拷给 `phealth`；`DM_COMMAND_END()` 释放并返回。

**一个带入参的命令函数。** `dsmi_cmd_dft_get_elabel` 多了一步 `DM_COMMAND_ADD_REQ` 塞入参：

[dsmi_dmp_command.c:35-46](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_dmp_command.c#L35-L46) — 依次 `ADD_REQ` 把 `item_type`、`eeprom_index` 塞进请求报文，`SEND` 后用 `GET_RSP_LEN` 取回包长度，再 `PUSH_OUT` 把标签数据拷出。这就是「多字段请求」的标准写法，对照它就能读懂任何带入参的 `dsmi_cmd_*`。

#### 4.2.4 代码实践

**实践目标**：读懂宏 DSL 写出的命令函数，并验证「每个命令函数都先探活再发包」。

**操作步骤**：

1. 打开 `dsmi_dmp_command.c`，阅读 `dsmi_cmd_get_device_temperature`（约第 68 行）、`dsmi_cmd_get_deviceid`（约第 96 行）。
2. 对照 `dsmi_common.h:427` 的 `DM_COMMAND_BIGIN` 展开，确认这两个函数都隐式调用了 `drvGetDevInfo`。
3. 在 `dsmi_cmd_info_def.h` 中找到 `DEV_MON_CMD_GET_CHIP_TEMP` 与 `DEV_MON_CMD_GET_DID` 的命令表条目，记下它们的响应长度。

**需要观察的现象**：这几个函数体几乎逐字相同，差别只在「命令名」和「出入参长度」。

**预期结果**：你能口头复述——「`dsmi_cmd_get_device_temperature` 先用 `drvGetDevInfo` 探活，再用 `DEV_MON_CMD_GET_CHIP_TEMP` 的 opcode 初始化报文，发包等回包，把 2 字节温度拷给 `ptemperature`」。本实践为源码阅读型，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dsmi_cmd_*` 函数体里看不到 `malloc` / `send` / `free` 这些字样，但报文确实被分配、发送、释放了？

> **答案**：这些操作被封装进了 `DM_COMMAND_*` 宏。`BIGIN` 里调 `dmp_command_init`（内含 `malloc`），`SEND` 里调 `dsmi_send_msg_rec_res`（内含发送），`END` 里调 `dsmi_cmd_req_free`（内含 `free`）。宏把样板代码隐藏了，函数体才如此简洁。

**练习 2**：如果要新增一个「查询设备风扇转速」的 DMP 命令，除了写一个新的 `dsmi_cmd_*` 函数，还需要在哪里加一行？

> **答案**：还要在 `dsmi_cmd_info_def.h` 用 `DSMI_CMD_DEF_COMMON_INSTANCE(新命令号, 入参长, 响应长, 类型)` 实例化对应的 `g_dmp_cmd_def_*`，否则 `DM_COMMAND_BIGIN` 里引用的 `g_dmp_cmd_def_##cmd_name` 不存在，编译会报错。

---

### 4.3 dsmi_common：通信基础设施与 drvGetDevInfo helper

#### 4.3.1 概念说明

`dsmi_common.c` 是 DSMI 的「机房」：宏 DSL 里调用的底层设施都在这里实现。它提供三方面能力：

1. **报文对象的构造与销毁**——`dmp_command_init` 分配并初始化 `DSMI_DMP_COMMAND_ST`（含请求/响应两块缓冲），`dsmi_cmd_req_free` 负责释放。
2. **发送与回收**——`dsmi_send_msg_rec_res` / `_dsmi_send_msg_rec_res` 负责把报文经通信底座发往设备、等待并校验回包，自带重试。
3. **通信通道初始化**——`dsmi_init_channel` 根据编译宏选择 HDC / UDP / IAM 三种传输之一。

此外，本节还要澄清一个容易混淆的点：`drvGetDevInfo`、`drvGetDevNum` 这些 helper **并非定义在 `dsmi_common.c`**，而是定义在 `dms`（Device Management System）的设备管理器里（`devdrv_manager_dev_info_api.c` 等）。它们属于「设备管理器」对外提供的查询能力，DSMI 只是调用方。把它们放在这一节讲，是因为它们是 DSMI 实现里出现频率最高的 helper，理解它就理解了「本地路径」。

#### 4.3.2 核心流程

**报文构造（`dmp_command_init`）**：

```
dmp_command_init(device_index, opcode, optype, input_len, output_len)
   ├── dsmi_init()                          // 懒加载：首次调用时初始化通信通道
   ├── malloc(DSMI_DMP_COMMAND_ST)
   ├── malloc 请求缓冲 = sizeof(请求头) + input_len
   ├── malloc 响应缓冲 = sizeof(响应头) + output_len
   ├── 填请求头：lun / optype / opcode / length
   └── 返回 dmp 指针
```

**发送回收（`dsmi_send_msg_rec_res`）**：

```
dsmi_send_msg_rec_res(dmp)
   └── 重试循环（最多 3 次，每次间隔 1s）：
        └── _dsmi_send_msg_rec_res(dmp)
             ├── 选择目的地址初始化（HDC / UDP / IAM，由编译宏决定）
             ├── dev_mon_send_request(...)   // 经 device_monitor 通信底座发出
             ├── dsmi_wait_receive(dmp)      // 阻塞等待回包
             └── dsmi_check_out_valid(...)   // 校验回包 errorcode
```

**`drvGetDevInfo`（本地 ioctl 路径）**：

```
drvGetDevInfo(devId, &info)
   ├── 校验 devId 范围、info 非空
   ├── drv_common_ioctl(DEVDRV_MANAGER_GET_DEVINFO)   // 陷入内核读设备信息
   └── 把内核返回的字段拷进 info（ai_core_num / chip_id / die_id ...）
```

> 一句话区分：`drvGetDevInfo` 走 **ioctl 到本机内核**（不联网、不往返设备固件）；`dsmi_send_msg_rec_res` 走 **HDC/UDP/IAM 到设备固件**（有往返时延）。

#### 4.3.3 源码精读

**报文构造。** `dmp_command_init` 用 `malloc` 分配「控制结构 + 请求缓冲 + 响应缓冲」三块内存，并填好请求头：

[dsmi_common.c:498-548](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L498-L548) — 关键点：第 506 行先 `dsmi_init()`（懒加载，首次调用时才建立通信通道，之后幂等）；第 515–516 行根据 `input_len`/`output_len` 计算两块缓冲大小；第 542–546 行填请求头 `lun/optype/opcode/length`。每个 `dmp` 还通过 `list_append(g_cmd_req_list, dmp)` 挂到全局链表上，便于统一管理与泄漏排查。

**发送与回收（核心）。** `_dsmi_send_msg_rec_res` 是真正发包的地方：

[dsmi_common.c:578-643](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L578-L643) — 注意第 594–600 行的三选一：`CFG_FEATURE_DMP_UDP` → `dsmi_init_udp_dest_addr`，`IAM_CONFIG` → `dsmi_init_iam_dest_addr`，否则默认 `dsmi_init_hdc_dest_addr`。这解释了「同一段 DSMI 代码在不同编译形态下走不同传输」。第 609 行 `dev_mon_send_request(...)` 经 `device_monitor` 通信底座（见 u5-l1）把报文发出去，回调 `dsmi_msg_recev` 用于异步收包；第 625 行 `dsmi_wait_receive(dmp)` 阻塞等回包；第 638 行 `dsmi_check_out_valid` 校验回包的 `errorcode`。

**带重试的包装。** `dsmi_send_msg_rec_res` 在底层之上加了「发送失败重试」：

[dsmi_common.c:645-665](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L645-L665) — 当底层返回 `DRV_ERROR_SEND_MESG`（发送失败）或 `DRV_ERROR_REMOTE_NO_SESSION`（远端无会话，常见于 HDC 通道繁忙）时，最多重试 3 次、每次 `sleep(1)` 秒；其他错误立即返回。这就解释了 `DM_COMMAND_SEND` 调用者为何不必自己处理瞬时通信抖动。

**通道初始化（三选一传输）。** `dsmi_init_channel` 展示了 HDC/UDP/IAM 三种传输的初始化分支：

[dsmi_common.c:1086-1160](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L1086-L1160) — 例如 `CFG_FEATURE_DMP_HDC` 分支调用 `dm_hdc_init(&g_dsmi_intf, ...)` 建立 HDC 客户端，填好 `DM_HDC_ADDR_ST` 目的地址结构。`g_dsmi_intf` 是 DSMI 持有的通信句柄，被 `dev_mon_send_request` 使用。HDC 通信底座本身的设计在 u3-l2 详述。

**`drvGetDevInfo`（本地路径，定义在 dms）。** 它通过 ioctl 陷入内核读设备信息，与 DMP 往返完全不同：

[devdrv_manager_dev_info_api.c:70-94](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c#L70-L94) — 校验 `devId` 范围与 `info` 非空后，构造 `devdrv_manager_hccl_devinfo`，调用 `drv_common_ioctl(&dev_info_buf, DEVDRV_MANAGER_GET_DEVINFO)` 陷入内核；设备忙时返回 `DRV_ERROR_RESOURCE_OCCUPIED`（这正是 `DM_COMMAND_BIGIN` 与 `dsmi_get_pcie_info` 用它来「探活」的依据）。成功后把内核返回的 `ai_core_num / aicore_freq / chip_id / die_id` 等字段逐个拷进输出结构体。注意它**不发包、不等回包**，开销远低于 DMP 路径。

#### 4.3.4 代码实践

**实践目标**：把「本地 ioctl」与「DMP 往返」两条路径在源码里走通，并理解 `drvGetDevInfo` 为何既是「本地数据源」又是「DMP 探活门」。

**操作步骤**：

1. 在 `dsmi_common.c` 定位 `dmp_command_init`（约第 498 行），确认它内部调用了 `dsmi_init()`（懒加载）。
2. 定位 `_dsmi_send_msg_rec_res`（约第 578 行），看清第 594–600 行的传输三选一与第 609 行的 `dev_mon_send_request`。
3. 打开 `devdrv_manager_dev_info_api.c`，阅读 `drvGetDevInfo`（约第 70 行），确认它走的是 `drv_common_ioctl`，而非通信底座发包。
4. 回到 `dsmi_common.h:427` 的 `DM_COMMAND_BIGIN`，确认第 451 行对 `drvGetDevInfo` 的调用——这是「探活门」。

**需要观察的现象**：

- `drvGetDevInfo` 的函数体里没有任何 `send`/`receive`/`HDC` 字样，只有 `ioctl`——它是纯本地操作。
- `_dsmi_send_msg_rec_res` 里既有 `send_request` 又有 `wait_receive`——它有完整的往返。
- 二者通过 `DM_COMMAND_BIGIN` 串联：先 `drvGetDevInfo`（探活），再 `dsmi_send_msg_rec_res`（往返）。

**预期结果**：你能画出一条「DMP 路径」查询的完整时序——`dsmi_*` → `dsmi_cmd_*` → `DM_COMMAND_BIGIN`(内含 `drvGetDevInfo` 探活 + `dmp_command_init`) → `DM_COMMAND_SEND`(内含 `dsmi_send_msg_rec_res` → `dev_mon_send_request` → HDC → 设备) → 回包 → `DM_COMMAND_PUSH_OUT` → `DM_COMMAND_END`。本实践为源码阅读型，无需硬件；若需运行验证，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`dmp_command_init` 里为什么要调用 `dsmi_init()`？这是不是说明 DSMI 也像 DCMI 一样需要用户显式调用初始化？

> **答案**：`dsmi_init()` 是**懒加载**——首次构造报文时自动初始化通信通道（建立 HDC/UDP/IAM 客户端），之后幂等。用户**不需要**显式调用 DSMI 初始化（注意 `dsmi_init` 不是 `DLLEXPORT` 的对外接口）。这与 DCMI 必须先 `dcmi_init()` 不同，是两套接口的设计差异。

**练习 2**：`dsmi_send_msg_rec_res` 在什么情况下会重试？为什么把重试放在这一层而不是每个 `dsmi_cmd_*` 里？

> **答案**：当底层返回 `DRV_ERROR_SEND_MESG` 或 `DRV_ERROR_REMOTE_NO_SESSION`（HDC 会话繁忙）时，最多重试 3 次、间隔 1 秒。重试放在这一层，是因为所有 `dsmi_cmd_*` 都经 `DM_COMMAND_SEND` 调到它，集中处理就能让上百个命令函数自动获得统一的瞬时抖动容错，而不必各自重复重试逻辑。

**练习 3**：`drvGetDevInfo` 与 `dsmi_send_msg_rec_res` 各自的「对端」是谁？

> **答案**：`drvGetDevInfo` 的对端是**本机内核里的设备管理器**（通过 `drv_common_ioctl` 陷入内核）；`dsmi_send_msg_rec_res` 的对端是**设备固件**（经 HDC/UDP/IAM 通信底座跨到设备侧）。前者无网络往返，后者有。

---

## 5. 综合实践

**任务**：选取一个真实的 DSMI 查询接口，手工写出它从用户调用到设备再返回的完整调用链，并标注每一段数据来自哪里。

> 说明：本讲规格建议跟踪的 `dsmi_get_host_device_connect_type` 接口在当前代码库（HEAD `e29d066`）中**并不存在**（已检索确认），因此我们用真实存在的接口 `dsmi_get_device_health` 完成同等目标的练习——这正是「跟踪一个查询接口如何经 `drvGetDevInfo` 与 DMP 命令下发到设备」的本意。

**实践步骤**：

1. **选定接口**：以 `dsmi_get_device_health(device_id, &health)` 为对象（对应设备健康状态查询）。
2. **从门面读起**：在 `dsmi_common_interface.c` 找到它的实现，确认它校验参数后转调 `dsmi_cmd_get_device_health`。
3. **进入命令层**：在 `dsmi_dmp_command.c` 阅读 `dsmi_cmd_get_device_health`，识别出 `BIGIN / SEND / PUSH_OUT / END` 四步。
4. **展开宏**：对照 `dsmi_common.h:427` 的 `DM_COMMAND_BIGIN`，标出其中两处关键调用——第 451 行 `drvGetDevInfo`（探活，本地 ioctl）与第 455 行 `dmp_command_init`（分配报文）。
5. **追到通信层**：从 `DM_COMMAND_SEND` 追到 `dsmi_common.c` 的 `dsmi_send_msg_rec_res` → `_dsmi_send_msg_rec_res`，标出 `dev_mon_send_request`（发包）与 `dsmi_wait_receive`（等回包）。
6. **画出调用链**：用箭头画出完整路径，并在每个节点旁标注「本地 / 往返」。

**预期产出（参考答案）**：

```
用户: dsmi_get_device_health(dev_id, &health)            [门面层]
  └─ 校验参数 → 调 dsmi_cmd_get_device_health(dev_id, &health)
       │
       ├─ DM_COMMAND_BIGIN(DEV_MON_CMD_GET_HEALTH_STATE, dev_id, 0, 1)
       │    ├─ dsmi_check_device_id(dev_id)              [本地]
       │    ├─ drvGetDevInfo(dev_id, &dev_info)          [本地 ioctl 探活] ★
       │    └─ dmp_command_init(dev_id, opcode=COMMON<<8|HEALTH_STATE, ...)
       │         └─ dsmi_init()（懒加载通信通道）
       │
       ├─ DM_COMMAND_SEND()
       │    └─ dsmi_send_msg_rec_res(dmp)                [往返] ★
       │         └─ _dsmi_send_msg_rec_res
       │              ├─ dsmi_init_hdc_dest_addr(...)    [选 HDC 传输]
       │              ├─ dev_mon_send_request(...)        [发包到设备固件]
       │              └─ dsmi_wait_receive(dmp)           [等设备回包]
       │
       ├─ DM_COMMAND_PUSH_OUT(&health, 1)                 [从回包拷 1 字节]
       └─ DM_COMMAND_END() → dsmi_cmd_req_free(dmp); return OK
```

**观察要点**：

- 带星号 ★ 的两处分别是「本地 ioctl 探活」与「设备固件往返」，是本讲要区分的两条路径。
- `drvGetDevInfo` 在这里不是用来取健康数据（健康数据只能从设备拿），而是用来**确认设备在线**——这就是「`drvGetDevInfo` 既是数据源又是探活门」的双重角色。
- 整条链路上，门面层、命令层、通信层各司其职，没有一层越界去管另一层的细节。

> 若手头有昇腾设备，可编写最小程序：`#include "dsmi_common_interface.h"`，调用 `dsmi_get_device_health(0, &h)`，链接 `-ldsmi`（随 `libascend_hal.so` 一同交付），打印返回值与健康字节，对照上述调用链验证。若无设备，则本实践为纯源码阅读型，标注「待本地验证」。

## 6. 本讲小结

- DSMI 实现按职责拆成三文件：`dsmi_common_interface.c`（门面：校验 + 分派）、`dsmi_dmp_command.c`（DMP 命令构造收发）、`dsmi_common.c`（通信基础设施）。
- 一条 `dsmi_*` 查询的数据来自两条路径之一：**本地 ioctl**（`drvGetDevInfo`/`drvGetDevNum`，陷本机内核，无往返）或 **DMP 往返**（`dsmi_cmd_*` → 设备固件，经 HDC/UDP/IAM）。
- `dsmi_cmd_*` 用一套 `DM_COMMAND_BIGIN/ADD_REQ/SEND/PUSH_OUT/END` 宏 DSL 写成，把「分配→填字段→发送→收包→取字段→释放」样板隐藏，使上百个命令函数短小整齐。
- 命令表由 `DSMI_CMD_DEF_COMMON_INSTANCE` 宏在 `dsmi_cmd_info_def.h` 批量实例化为 `g_dmp_cmd_def_*`，opcode = 功能码高字节 << 8 | 命令号。
- 关键连接点：**每条 DMP 命令在 `DM_COMMAND_BIGIN` 里都先调用 `drvGetDevInfo` 做在线探活**——`drvGetDevInfo` 与 DMP 命令是「先探活后往返」的前后关系，不是二选一。
- `dsmi_send_msg_rec_res` 自带「发送失败/会话繁忙」重试（最多 3 次），为所有命令提供统一的瞬时抖动容错；DSMI 通信通道在首次构造报文时懒加载初始化，用户无需显式 init。

## 7. 下一步学习建议

- **下一讲 u2-l3「从零新增一个 DSMI 接口」**：把本讲学到的「门面 + 命令表 + 宏 DSL」三件套真正动手用起来，端到端新增一个查询接口并编译验证。
- **横向延伸 u3-l2「HDC 通信模型」**：本讲多次出现的 `dev_mon_send_request`、`dm_hdc_init`、`dsmi_init_hdc_dest_addr` 都建立在 HDC 通信底座之上，学完 HDC 能彻底看懂「报文是怎么物理上到达设备的」。
- **纵向延伸 u5-l1「DMC 与 device_monitor」**：`device_monitor` 是 DSMI/DMP 报文在 Host 侧的收发中枢，`dev_mon_send_request` 即出自该模块，了解它能补全「Host 侧消息通路」的拼图。
- **建议继续阅读的源码**：通读 `dsmi_dmp_command.c` 中 3～5 个不同形态的 `dsmi_cmd_*`（无入参、单入参、多入参、带 `GET_RSP_LEN` 的变长响应），巩固对宏 DSL 的直觉；再对照 `dsmi_cmd_info_def.h` 的命令表，体会「表驱动 + 宏」设计的可维护性。
