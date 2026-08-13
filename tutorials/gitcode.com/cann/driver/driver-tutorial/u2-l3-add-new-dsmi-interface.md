# 实战：从零新增一个 DSMI 接口

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚在 driver 中**新增一个 DSMI 接口**需要改动哪几个文件、每个文件改什么。
- 独立写出一个符合规范的 DSMI 查询接口：包含**参数校验**、**`drvGetDevInfo` 调用**、**错误码返回**三件套。
- 区分 DSMI 的两条数据路径（**本地 ioctl 查询** vs **DMP 报文往返**），并判断一个新接口该走哪条路。
- 用 `build.sh` 重新编译驱动、部署 run 包，并写一个最小用户态程序调用自研接口验证返回值。

本讲是一篇「实战篇」：我们不只是读代码，而是真的动手加一个接口。承接 [u2-l1（DCMI 接口总览与初始化）](u2-l1-dcmi-overview-and-init.md) 与 [u2-l2（DSMI 接口实现）](u2-l2-dsmi-interface-impl.md)，把前面学到的「门面 + DMP 命令 + `drvGetDevInfo`」三件套，落成一次端到端开发。

## 2. 前置知识

在动手前，先回顾两个关键认知（详细版见 u2-l2）：

- **DSMI 接口的三文件分工**：`dsmi_common_interface.c` 是对外门面（做参数校验与路径分派），`dsmi_dmp_command.c` 用 `DM_COMMAND_*` 宏构造并收发 DMP（设备管理协议）命令，`dsmi_common.c` 提供通信基础设施。
- **两条数据路径**：一条查询的数据，要么来自**本地 ioctl**（`drvGetDevInfo` 陷入本机内核读取设备缓存信息，无报文往返），要么来自 **DMP 报文往返**（经 HDC/UDP 送到设备固件再返回）。

还有一个本讲要反复用到的术语：

- **ioctl（I/O control）**：用户态程序通过一个系统调用「陷入」内核态，请求内核驱动帮自己完成某件事。这里 `drvGetDevInfo` 就是把「取设备信息」的请求交给本机内核态的 SDK-driver 去做。

> 提示：本讲要新增的接口 `dsmi_get_host_device_connect_type` 在当前代码库（HEAD `e29d066`）中**并不存在**——它正是 `QUICKSTART.md` 开发指南给出的「请你来新增」的示例接口。如果你在源码里 `grep` 它，会一无所获，这是正常的，因为它就是我们要亲手加进去的那一个。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `docs/zh/QUICKSTART.md` | 快速入门与开发指南 | 它是本实战的「需求说明书」，给出了要新增接口的完整样例代码 |
| `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c` | DSMI 对外门面实现 | 在这里**新增接口的实现**，并对照已有的 `dsmi_get_pcie_info` 学习写法 |
| `pkg_inc/dsmi_common_interface.h` | DSMI 对外头文件门面 | 在这里**声明新接口**（`DLLEXPORT` + Doxygen 注释） |
| `src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c` | `drvGetDevInfo` 的实现 | 理解新接口依赖的「取设备信息」底层调用如何工作 |
| `src/ascend_hal/dmc/dsmi/include/dsmi_common.h` | DMP 命令宏 `DM_COMMAND_*` 定义 | 对比「本地查询」与「DMP 往返」两条路径的差异 |
| `pkg_inc/ascend_hal_error.h` | 驱动统一错误码枚举 | 查 `DRV_ERROR_INVALID_VALUE`、`DRV_ERROR_RESOURCE_OCCUPIED` 的含义 |
| `build.sh` | 唯一编译入口 | 学习 `--soc` / `--pkg` 参数，重新编译并生成 run 包 |

## 4. 核心概念与源码讲解

### 4.1 新增接口的全景：改哪里、走哪条路

#### 4.1.1 概念说明

「新增一个 DSMI 接口」听起来抽象，落到代码上其实只动**三个地方**：

1. **头文件** `pkg_inc/dsmi_common_interface.h`：声明函数原型（让上层能看见、能链接）。
2. **实现文件** `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c`：写出函数体（参数校验 + 取数据 + 返回）。
3. **重新编译部署**：用 `build.sh` 重新生成 run 包并安装，新接口才会进入 `libascend_hal.so`。

`QUICKSTART.md` 的「开发指南」章节正是按这三步给出了一份完整样例，我们的实战就照它来做：

- 开发指南入口：[docs/zh/QUICKSTART.md:177-253](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L177-L253) ——「三、开发指南」，明确说「以新增 DCMI 接口为例」，并给出了 dsmi 实现样例（L186-L203）、头文件声明样例（L208-L218）、dcmi 包装样例（L220-L245）。

#### 4.1.2 核心流程

在动手前，先做一次「路径选择」判断——这是写新接口最关键的设计决策：

```text
       新接口要返回的数据，从哪里来？
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  本地内核已缓存吗？          必须问设备固件吗？
  (设备信息表里已有)          (health/温度/电量等)
        │                        │
        ▼                        ▼
  走「本地查询」路径         走「DMP 往返」路径
  drvGetDevInfo 一把取        DM_COMMAND_* 宏 DSL
  不用造新命令               要定义命令码并下发
```

本讲要加的 `host_device_connect_type`（主机-设备连接类型，例如 PCIe / HCCS 等）**属于「本地内核已缓存」那一类**：它早已是 `drvGetDevInfo` 返回的设备信息结构体里的一个字段。所以我们走最简单的「本地查询」路径——只调 `drvGetDevInfo`，**不需要**新造 DMP 命令。这也是 `QUICKSTART` 选它当教学示例的原因：门槛最低，又能把三件套（声明/实现/编译）完整走一遍。

> 经验法则：先去 `drvGetDevInfo` 返回的结构体里找你想要的字段；找得到就走本地查询路径，找不到才考虑 DMP 往返路径。

#### 4.1.3 源码精读

我们说过 `drvGetDevInfo` 返回的设备信息里**已经包含** `host_device_connect_type`。验证这一点要看两处：

第一，字段确实定义在设备信息结构体里：[src/ascend_hal/inc/dms/dms_drv_internal.h:54-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dms/dms_drv_internal.h#L54-L80) —— `struct devdrv_device_info` 的定义，其中 L80 就是 `unsigned int host_device_connect_type;`。

第二，`drvGetDevInfo` 确实会填充这个字段：[src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c:120](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c#L120) —— `info->host_device_connect_type = dev_info.host_device_connect_type;`，把内核返回的值拷到输出参数里。

也就是说，新接口要做的事，本质就是「调一次 `drvGetDevInfo`，把它的某个字段抄给调用者」。

#### 4.1.4 代码实践（阅读型）

1. **目标**：确认「本地查询」路径不需要造新命令。
2. **步骤**：在本仓根目录执行检索，确认 `host_device_connect_type` 已经是设备信息结构体的固有字段，而不是某个 DMP 命令的产物。
   ```bash
   grep -rn "host_device_connect_type" src/ascend_hal/inc/ src/sdk_driver/dms/
   ```
3. **观察现象**：你会看到该字段同时出现在用户态头文件（`dms_drv_internal.h`）、内核态消息头（`devdrv_manager_common_msg.h` / `urd_msg.h`）和 `drvGetDevInfo` 的赋值处。
4. **预期结果**：字段定义与赋值点都能找到，说明它由内核侧 `DEVDRV_MANAGER_GET_DEVINFO` 这个 ioctl 一次性带回，**无需新增任何 DMP 命令**。
5. 运行结果：待本地验证（取决于本地是否已 clone 完整源码）。

#### 4.1.5 小练习与答案

- **练习 1**：如果新接口想返回的是「设备当前温度」，还能走本地查询路径吗？
  - **答**：通常不能。温度是设备运行时实时量，不在 `drvGetDevInfo` 缓存的静态设备信息里，需要走 DMP 往返，参见 `dsmi_cmd_get_device_temperature`。
- **练习 2**：`drvGetDevInfo` 返回非 0 但又不是 `DRV_ERROR_RESOURCE_OCCUPIED` 时，本讲的样例代码会怎样？
  - **答**：样例只判断了 `RESOURCE_OCCUPIED` 这一个分支，其余非 0 返回值会被忽略、继续往下读字段并返回 0（成功）。这是一个可以加固的点，见 4.3.5 练习。

---

### 4.2 接口声明：在头文件中导出

#### 4.2.1 概念说明

光在 `.c` 里写函数体不够，必须**在对外头文件里声明**，上层模块才能「看见」并链接到它。driver 用两个约定来声明对外接口：

- **`DLLEXPORT` 宏**：在 Linux 下展开为 `__attribute__((visibility("default")))`，表示这个符号要从动态库里导出，供外部链接。定义见 [pkg_inc/dsmi_common_interface.h:17-20](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L17-L20)。
- **Doxygen 注释块**：每个对外接口上方都有一段 `@ingroup / @brief / @param / @return / @note Support` 格式的注释，既给文档生成器用，也给人读。

#### 4.2.2 核心流程

声明一个新接口的标准动作：

```text
1. 在 dsmi_common_interface.h 中找一个语义相近的已有声明作参照
2. 抄它的 Doxygen 注释格式
3. 写出原型：DLLEXPORT 返回类型 函数名(参数列表);
4. （可选）在 @note Support 里列出支持的芯片型号
```

#### 4.2.3 源码精读

先看两个现成的声明范例，照着抄即可：

- `dsmi_get_device_count` 的声明与注释：[pkg_inc/dsmi_common_interface.h:2595-2603](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L2595-L2603) —— 注意它返回 `int`，注释里有完整的 `@param [out]` 与 `@note Support` 芯片列表。
- `dsmi_get_pcie_info` 的声明：[pkg_inc/dsmi_common_interface.h:2719](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L2719) —— `DLLEXPORT int dsmi_get_pcie_info(int device_id, struct tag_pcie_idinfo *pcie_idinfo);`，入参 `device_id` + 出参指针，是查询类接口最典型的签名。

`QUICKSTART` 给出的新接口声明正是照这个范式写的（见 [docs/zh/QUICKSTART.md:208-218](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L208-L218)）：

```c
/**
* @ingroup driver
* @brief host-device connect types
* @attention null
* @param [in]  device_id  device id
* @param [out] connect_type  host-device connect types
* @return  0 for success, others for fail
*/
int dsmi_get_host_device_connect_type(int device_id, unsigned int *connect_type);
```

> 说明：`QUICKSTART` 样例里省略了 `DLLEXPORT` 前缀和 `@note Support` 芯片列表。实际贡献代码时建议补上 `DLLEXPORT`（保证符号被导出），并按真实适配情况补 `@note Support`。

#### 4.2.4 代码实践（阅读 + 动手）

1. **目标**：把新接口声明加到头文件正确位置。
2. **步骤**：打开 `pkg_inc/dsmi_common_interface.h`，定位到 `dsmi_get_pcie_info` 声明（L2719 附近），在其**下方**插入上面的 `dsmi_get_host_device_connect_type` 声明（建议加上 `DLLEXPORT` 前缀）。
3. **观察现象**：保存后，用 `grep -n dsmi_get_host_device_connect_type pkg_inc/dsmi_common_interface.h` 应能命中你新增的一行。
4. **预期结果**：头文件中出现带 Doxygen 注释的新接口声明，且以 `DLLEXPORT` 导出。
5. 运行结果：待本地验证。

#### 4.2.5 小练习与答案

- **练习 1**：为什么对外接口要加 `DLLEXPORT`（即 `visibility("default")`）？不加会怎样？
  - **答**：它告诉链接器把这个符号放进动态符号表，供外部程序链接。若不加，在启用符号可见性裁剪的构建里，该符号可能被隐藏，外部调用会变成「未定义符号」链接失败。
- **练习 2**：`@param [in]` 与 `@param [out]` 有什么区别？
  - **答**：`[in]` 表示调用者传入、函数内只读的入参（如 `device_id`）；`[out]` 表示函数内写入、用来回传结果给调用者的出参指针（如 `connect_type`）。

---

### 4.3 接口实现：参数校验 + drvGetDevInfo + 错误码

#### 4.3.1 概念说明

接口实现要遵循 DSMI 门面既定的「三段式」写法，这也是 u2-l2 讲过的门面职责：

1. **参数校验**：出参指针不能为 `NULL`，否则返回参数错误码。
2. **取数据**：调用 `drvGetDevInfo` 获取设备信息（本地 ioctl），并处理「设备忙」这一特殊情况。
3. **填出参、返回**：把目标字段抄给出参指针，成功返回 `0`。

错误码体系要统一用 `pkg_inc/ascend_hal_error.h` 里的枚举，DSMI 接口返回 `int`，**成功即 0**。

#### 4.3.2 核心流程

新接口的实现骨架（本地查询路径）：

```text
int dsmi_get_xxx(int device_id, T *out)
    ├─ if (out == NULL) return DRV_ERROR_INVALID_VALUE;   // 1. 参数校验
    ├─ ret = drvGetDevInfo(device_id, &dev_info);          // 2. 本地 ioctl 取信息
    ├─ if (ret == DRV_ERROR_RESOURCE_OCCUPIED)             //    处理设备忙
    │       return DRV_ERROR_RESOURCE_OCCUPIED;
    ├─ *out = dev_info.<字段>;                             // 3. 抄字段
    └─ return 0;                                           //    成功
```

`drvGetDevInfo` 内部做了什么（见 4.3.3）：它先校验 `devId` 和指针，再发起一次 `DEVDRV_MANAGER_GET_DEVINFO` ioctl 陷入本机内核，内核把一整块设备信息回填，然后它把各字段（含 `host_device_connect_type`）逐个拷到输出结构体。所以对上层而言，它就是一个「一次 ioctl 拿全量设备信息」的本地调用。

#### 4.3.3 源码精读

**先看 `drvGetDevInfo` 的真身**：[src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c:70-94](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c#L70-L94)。关键几行：

```c
drvError_t drvGetDevInfo(uint32_t devId, struct devdrv_device_info *info)
{
    ...
    if (devId >= ASCEND_DEV_MAX_NUM || info == NULL) { ...; return DRV_ERROR_INVALID_VALUE; }
    dev_info.dev_id = devId;
    ret = drv_common_ioctl(&dev_info_buf, DEVDRV_MANAGER_GET_DEVINFO);  // 本地 ioctl
    if (ret != 0) {
        if (ret == DRV_ERROR_RESOURCE_OCCUPIED || ret == DRV_ERROR_BUSY) {
            ...; return DRV_ERROR_RESOURCE_OCCUPIED;   // 设备忙
        } else { ...; return ret; }
    }
    info->ai_core_num = dev_info.ai_core_num;
    ...                       // 大量字段拷贝
```

随后在 [devdrv_manager_dev_info_api.c:120](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/drv_devmng/ascend910/devdrv_manager_dev_info_api.c#L120) 把 `host_device_connect_type` 拷进输出结构体。这一行就是我们新接口的数据来源。

**再看一个最接近的现成实现 `dsmi_get_pcie_info`**，它是「本地查询路径」的范本：[src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c:588-613](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L588-L613)。精简后：

```c
int dsmi_get_pcie_info(int device_id, struct tag_pcie_idinfo *pcie_idinfo)
{
    int ret;
    struct devdrv_device_info dev_info = {0};
    ret = drvGetDevInfo((uint32_t)(device_id), &dev_info);   // 先探活/取信息
    if (ret == (int)DRV_ERROR_RESOURCE_OCCUPIED) {
        return DRV_ERROR_RESOURCE_OCCUPIED;
    }
    ret = drvDeviceGetPcieIdInfo(...);                        // 再取 PCIe 细节
    ...
    return 0;
}
```

注意它和 `dsmi_get_device_count`（[dsmi_common_interface.c:147-175](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L147-L175)）一样，都是「调底层 helper → 检查 `RESOURCE_OCCUPIED` → 填出参 → 返回 0」的同款套路。文件顶部还有一个等价的便捷宏 `CHECK_DEVICE_BUSY`（[dsmi_common_interface.c:91-99](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L91-L99)），但仓内多数接口选择把检查**内联**写出来，`QUICKSTART` 样例也采用了内联写法。

**于是 `QUICKSTART` 给出的新接口实现**（[docs/zh/QUICKSTART.md:186-203](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L186-L203)）就顺理成章，完全照搬了 `dsmi_get_pcie_info` 的范式：

```c
int dsmi_get_host_device_connect_type(int device_id, unsigned int *connect_type)
{
    int ret;
    struct devdrv_device_info dev_info = { 0 };

    if (connect_type == NULL) {                 // 1. 参数校验
        return DRV_ERROR_INVALID_VALUE;
    }

    /* drvGetDevInfo：获取npu的设备信息 */
    ret = drvGetDevInfo((unsigned int)device_id, &dev_info);   // 2. 本地 ioctl
    if (ret == (int)DRV_ERROR_RESOURCE_OCCUPIED) {             //    设备忙
        return DRV_ERROR_RESOURCE_OCCUPIED;
    }

    *connect_type = dev_info.host_device_connect_type;         // 3. 抄字段
    return 0;
}
```

用到的两个错误码都在统一枚举里：`DRV_ERROR_INVALID_VALUE = 3`（[pkg_inc/ascend_hal_error.h:37](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_error.h#L37)）、`DRV_ERROR_RESOURCE_OCCUPIED = 87`（[pkg_inc/ascend_hal_error.h:131](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_error.h#L131)）。

#### 4.3.4 代码实践（阅读型）

1. **目标**：体会「本地查询」与「DMP 往返」在源码层面的本质差别。
2. **步骤**：对比 `dsmi_get_pcie_info`（本地查询，L588-L613）与 DMP 命令 `dsmi_cmd_get_device_health`（[dsmi_dmp_command.c:54-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_dmp_command.c#L54-L60)）。
3. **观察现象**：`dsmi_get_pcie_info` 直接调 `drvGetDevInfo` 后就地返回；而 `dsmi_cmd_get_device_health` 用一串 `DM_COMMAND_BIGIN/SEND/PUSH_OUT/END` 宏，宏内部会构造报文并调用 `dsmi_send_msg_rec_res` 把报文送到设备固件。
4. **预期结果**：你能说出「本地查询路径不需要 `DM_COMMAND_*` 宏，DMP 往返路径才需要」。本讲的新接口走的是前者，所以**不碰** `dsmi_dmp_command.c`。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：`QUICKSTART` 样例在 `drvGetDevInfo` 返回后只判了 `RESOURCE_OCCUPIED`。请改写得更健壮：当 `ret != 0` 时直接 `return ret;`。这样改有什么好处？
  - **答**：好处是任何 ioctl 失败（如设备不存在、ioctl 内部错误）都能如实上报给调用者，而不是带着未初始化/旧数据假装成功返回 0。这是比样例更严谨的工业级写法。
- **练习 2**：为什么 `DM_COMMAND_BIGIN` 宏内部也调了一次 `drvGetDevInfo`（见 [dsmi_common.h:451](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/include/dsmi_common.h#L451)）？这是否与本讲的「本地查询」重复？
  - **答**：DMP 路径里那次 `drvGetDevInfo` 的主要作用是**在线探活**——确认设备在线、非虚拟机/离线等前置条件满足后再下发报文，顺带也拒绝「设备忙」。它和本讲的「本地查询」目的不同：前者是为了「能不能发报文」做检查，后者是「直接拿它的返回字段当结果」。两者并不冲突。

---

### 4.4 编译、部署与验证：build.sh 的参数与产物

#### 4.4.1 概念说明

代码改完不会自动生效。DSMI 的实现编译进 `libascend_hal.so`（由 `src/ascend_hal` 构建出，见 [src/ascend_hal/build/CMakeLists.txt:10](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/build/CMakeLists.txt#L10) 的 `add_library(ascend_hal SHARED)`），而 `.so` 又被打进 driver run 包。所以要经过：**清理 → 编译 → 部署 → 验证** 四步。这四步的入口都是 `build.sh`。

#### 4.4.2 核心流程

```text
1. 清理上次编译缓存（避免脏构建）
      bash build.sh --make_clean
2. 编译并打 run 包
      bash build.sh --pkg --soc=ascend910b
      → 产物：build_out/Ascend-hdk-<chip>-driver-<ver>_<os>-<arch>.run
3. 部署安装（需 root）
      ./Ascend-hdk-910b-driver-*.run --full
4. 写最小程序调用新接口，编译运行看返回值
```

`build.sh` 解析参数用的是 shell 的 `getopts`，关键参数有 `--soc`（指定芯片，驱动 `get_product` 选定 `PRODUCT`）、`--pkg`（打 run 包）、`--ube`（灵衢超节点，仅 ascend950）。

#### 4.4.3 源码精读

- 用法总览：[build.sh:20-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L20-L44) —— 列出 `-j/-k/--soc/--pkg/--ube/--make_clean` 等选项与示例。
- `--soc` 到 `PRODUCT` 的映射：[build.sh:46-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L46-L66) —— `ascend910b`→`ascend910B`；`ascend910_93`→`ascend910B`（并置 `ASCEND910_93_EX=TRUE`，对应 A3 包名）；`ascend950`→`ascend950`。
- 参数解析主循环 `checkopts`：[build.sh:113-170](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L113-L170)，其中 `--soc=*` 分支在 [build.sh:144-147](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L144-L147) 取值后立即调用 `get_product`；`--pkg` 分支在 [build.sh:135-137](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L135-L137) 置 `ENABLE_PACKAGE="TRUE"`。
- 一个值得知道的构建细节 `prepare_src`：[build.sh:183-191](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L183-L191) —— 编译前会把定制层 `src/custom/dev_prod/user/dsmi_product_ext` **拷贝**进 `src/ascend_hal/dmc/dsmi/` 一起参与 `libascend_hal.so` 构建。这说明 dsmi 的最终产物是「主源码 + 定制扩展」合并编译的结果，也提示我们：dsmi 相关实现既可能在本目录，也可能来自被并入的 `dsmi_product_ext`。

> 编译会自动联网拉取开源第三方库与 driver 开源二进制库（见 [QUICKSTART.md:77](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L77)），请保持网络畅通。

#### 4.4.4 代码实践（操作型）

1. **目标**：成功生成并部署一个包含新接口的 driver run 包。
2. **步骤**：
   ```bash
   # 在仓库根目录
   bash build.sh --make_clean                       # 先清理
   bash build.sh --pkg --soc=ascend910b             # 编译并打包
   ls build_out/                                    # 查看生成的 .run
   sudo ./build_out/Ascend-hdk-910b-driver-*.run --full   # 部署（需 root）
   ```
3. **观察现象**：`build_out/` 下出现 `Ascend-hdk-910b-driver-<version>_<os>-<arch>.run`；部署脚本解压、安装、替换已安装驱动。
4. **预期结果**：部署完成后，新接口符号 `dsmi_get_host_device_connect_type` 应已进入系统里的 `libascend_hal.so`。
5. 运行结果：待本地验证（需要真实 NPU 环境与 root 权限；无硬件时编译可完成，但部署与运行验证无法进行）。

> 若编译报缓存相关错误，回到第 1 步重新 `--make_clean`；常见编译安装问题可查 [docs/zh/FAQ.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/FAQ.md)。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `QUICKSTART` 强调「重复编译前要先 `--make_clean`」？
  - **答**：避免上一次的中间产物（目标文件、CMake 缓存）残留导致脏构建或符号不一致，保证新加的接口实现确实被重新编译进库。
- **练习 2**：`ascend910b` 与 `ascend910_93` 在 `get_product` 里都映射成 `PRODUCT=ascend910B`，那它们编译出的包怎么区分？
  - **答**：通过额外置位的 `ASCEND910_93_EX=TRUE` 标志，在后续打包环节区分出 A3 包名（910_93）与普通 910b 包名。

---

## 5. 综合实践：端到端新增并验证 `dsmi_get_host_device_connect_type`

把 4.1～4.4 串起来，完成一次真正的端到端开发。本任务**直接照搬 `QUICKSTART` 开发指南**（[docs/zh/QUICKSTART.md:177-253](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L177-L253)），分六步。

### 步骤 1：在头文件中声明接口

在 `pkg_inc/dsmi_common_interface.h` 中（建议紧跟 `dsmi_get_pcie_info` 声明之后）加入：

```c
/**
* @ingroup driver
* @brief host-device connect types
* @param [in]  device_id  device id
* @param [out] connect_type  host-device connect types
* @return  0 for success, others for fail
*/
DLLEXPORT int dsmi_get_host_device_connect_type(int device_id, unsigned int *connect_type);
```

### 步骤 2：在实现文件中写出函数体

在 `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c` 中（建议紧跟 `dsmi_get_pcie_info` 之后）加入。这里给出**比 QUICKSTART 更健壮**的版本（在 ioctl 失败时如实返回）：

```c
/* 示例代码：本讲在 QUICKSTART 样例基础上加固了错误返回 */
int dsmi_get_host_device_connect_type(int device_id, unsigned int *connect_type)
{
    int ret;
    struct devdrv_device_info dev_info = { 0 };

    if (connect_type == NULL) {
        return DRV_ERROR_INVALID_VALUE;
    }

    /* drvGetDevInfo：获取 npu 的设备信息（本地 ioctl） */
    ret = drvGetDevInfo((unsigned int)device_id, &dev_info);
    if (ret == (int)DRV_ERROR_RESOURCE_OCCUPIED) {
        return DRV_ERROR_RESOURCE_OCCUPIED;
    }
    if (ret != 0) {
        return ret;                 /* 比 QUICKSTART 多加的一行：如实上报其他错误 */
    }

    *connect_type = dev_info.host_device_connect_type;
    return 0;
}
```

### 步骤 3（推荐）：在 DCMI 层加一个包装接口

`QUICKSTART` 还演示了在上层 DCMI 里包一层（见 [QUICKSTART.md:220-245](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L220-L245)）。在 `src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c` 中加入：

```c
/* 示例代码：DCMI 包装层，转调 dsmi 接口（节选自 QUICKSTART） */
int dcmi_get_host_device_connect_type(int device_id, unsigned int *connect_type)
{
    int ret;

    if (dcmi_get_run_env_init_flag() != TRUE) {
        gplog(LOG_ERR, "not init.");
        return DCMI_ERR_CODE_NOT_REDAY;
    }
    if ((connect_type == NULL) || (device_id < 0)) {
        gplog(LOG_ERR, "para is invalid");
        return DCMI_ERR_CODE_INVALID_PARAMETER;
    }

    ret = dsmi_get_host_device_connect_type(device_id, connect_type);
    if (ret != DCMI_OK) {
        gplog(LOG_ERR, "call dsmi_get_host_device_connect_type failed. err is %d.", ret);
        return ret;
    }
    return DCMI_OK;
}
```

> 这一步正好呼应 u2-l1：DCMI 是 DSMI 之上的门面，做初始化与参数校验后转调 dsmi。如果你只想验证 dsmi 接口本身，可跳过本步，直接走步骤 5 的「直接调用」方案。

### 步骤 4：重新编译并部署

```bash
bash build.sh --make_clean
bash build.sh --pkg --soc=ascend910b
sudo ./build_out/Ascend-hdk-910b-driver-*.run --full
```

### 步骤 5：写最小用户态程序调用新接口

下面给出两种验证写法，任选其一。

**方案 A（推荐，走 DCMI 包装，链接 `-ldcmi`，与 examples/dcmi 一致）**：

```c
/* 示例代码：最小验证程序 main.c */
#include <stdio.h>
#include "dcmi_interface_api.h"

int main(void)
{
    int ret = dcmi_init();
    if (ret != 0) {
        printf("dcmi_init failed: %d\n", ret);
        return ret;
    }

    unsigned int connect_type = 0;
    ret = dcmi_get_host_device_connect_type(0, &connect_type);  /* device_id = 0 */
    if (ret != 0) {
        printf("get connect_type failed: %d\n", ret);
        return ret;
    }
    printf("host-device connect type = %u\n", connect_type);
    return 0;
}
```

参照 examples 的编译命令（[examples/dcmi/dcmi/run.sh:19](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/run.sh#L19)）：

```bash
gcc main.c -I/usr/local/dcmi/ -Isrc/custom/include -Ipkg_inc \
           -L/usr/local/dcmi/ -L/usr/local/Ascend/driver/lib64/driver -ldcmi -o main
```

**方案 B（直接调 dsmi，链接 `-lascend_hal`）**：

```c
/* 示例代码：直接调用 dsmi 接口 */
#include <stdio.h>
#include "dsmi_common_interface.h"

int main(void)
{
    unsigned int connect_type = 0;
    int ret = dsmi_get_host_device_connect_type(0, &connect_type);
    if (ret != 0) {
        printf("dsmi get connect_type failed: %d\n", ret);
        return ret;
    }
    printf("host-device connect type = %u\n", connect_type);
    return 0;
}
```

dsmi 符号位于 `libascend_hal.so`，链接时改用 `-lascend_hal`（具体库搜索路径与是否需先 `dsmi` 侧的显式初始化，**待本地验证**）。

### 步骤 6：运行并核对返回值

```bash
sudo ./main
```

- **预期结果**：程序打印一个非负整数 `host-device connect type = N`，`N` 对应实际连接类型编码（如某类 PCIe/HCCS 连接）；返回码为 0。
- 若返回 `DRV_ERROR_RESOURCE_OCCUPIED(87)`：说明设备忙，稍后重试。
- 若返回参数错误码：检查出参指针是否为 `NULL`、`device_id` 是否合法。
- 运行结果：待本地验证（依赖真实 NPU 硬件与已部署的自编译驱动）。

> 调试提示：若程序报「未定义符号」，多半是步骤 4 的部署没生效（旧 `.so` 还在），或方案 B 的链接库路径不对。可先用 `nm -D /usr/local/Ascend/driver/lib64/driver/libascend_hal.so | grep dsmi_get_host_device_connect_type` 确认符号是否真的被导出。

## 6. 本讲小结

- 新增一个 DSMI 接口只需改**三处**：头文件声明（`dsmi_common_interface.h`）、实现（`dsmi_common_interface.c`）、用 `build.sh` 重编部署。
- 写实现要守「**参数校验 → `drvGetDevInfo` → 处理 `RESOURCE_OCCUPIED` → 填出参 → 返回 0**」的门面范式，错误码统一用 `ascend_hal_error.h` 枚举，成功即 0。
- 关键是**先选路径**：要返回的字段若已在 `drvGetDevInfo` 的设备信息里（如 `host_device_connect_type`），走「本地查询」即可，无需 DMP 命令；否则才走 `DM_COMMAND_*` 宏的「DMP 往返」。
- `drvGetDevInfo` 本质是一次本地 ioctl（`DEVDRV_MANAGER_GET_DEVINFO`），把内核缓存的整块设备信息一次性带回，是新接口的数据来源。
- 编译入口是 `build.sh`：`--soc` 经 `get_product` 选定 `PRODUCT`，`--pkg` 打 run 包，`--make_clean` 防脏构建；产物在 `build_out/`，`./xxx.run --full` 部署。
- 验证可走 DCMI 包装（`-ldcmi`，与 examples 一致）或直接调 dsmi（`-lascend_hal`），用最小 `main.c` 打印返回值即可确认端到端打通。

## 7. 下一步学习建议

- **横向对比 DMP 往返路径**：回到 [u2-l2](u2-l2-dsmi-interface-impl.md)，精读 `dsmi_dmp_command.c` 里的 `DM_COMMAND_*` 宏，试着为本讲接口「假想」一个 DMP 版本（定义命令码、用宏下发），体会两条路径的取舍。
- **向上打通 DCMI**：阅读 `src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c`，看更多 DCMI 包装如何转调 dsmi，巩固 u2-l1 的「DCMI 门面 + DSMI 实现」分层。
- **向下追到内核**：好奇 `DEVDRV_MANAGER_GET_DEVINFO` 这个 ioctl 在内核侧怎么填数据？那是单元 6「SDK-driver 内核层」的主题，可先记下这个入口语义，学完 [u6-l1（SDK-driver 与 kernel_adapt）](u6-l1-sdk-driver-and-kernel-adapt.md) 再回来看。
- **贡献流程**：如果打算把自研接口提交回社区，结合 [u8-l4（编码规范与社区贡献）](u8-l4-coding-standard-and-contributing.md) 了解 `.clang-format`、pre-commit 与 PR 流程。
```
