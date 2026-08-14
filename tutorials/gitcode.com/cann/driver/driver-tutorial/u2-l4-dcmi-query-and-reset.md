# DCMI 查询与复位接口实现

## 1. 本讲目标

本讲承接 u2-l1（DCMI 接口总览与初始化流程）。上一讲我们建立了这样一个认知：`dcmi_init()` 在首次调用时把板型、芯片型号、卡/设备列表「探测一次、填进全局缓存 `g_board_details`」，之后的查询接口只是读这份缓存。

学完本讲，你应当能够：

- 说清 DCMI **查询类接口**（以 `dcmi_basic_info_intf.c` 为代表）的「参数校验 → 形态判断 → 读缓存」三段式套路，并区分它和「需要设备往返」的运行信息查询。
- 说清芯片**热复位**的统一入口 `dcmi_set_device_reset`，以及它如何按 `channel_type` 分发到两条完全不同的底层通路。
- 区分**带内复位（INBAND_CHANNEL）**与**带外复位（OUTBAND_CHANNEL）**在实现、调用步骤、底层通信接口上的根本差异，并能对照 `examples/dcmi/dcmi/2_chip_reset` 下的两个样例讲明白。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（均在 u2-l1、u2-l2 建立）：

- **`g_board_details` 全局缓存**：`dcmi_init` 探测出的板型/芯片/卡列表都存在这里，查询接口直接读它，不再二次往返设备。
- **卡（card_id）与设备（device_id）两级定位**：一张 NPU 卡有一个 `card_id`，卡上每颗芯片有一个 `device_id`；很多接口还用到 `device_logic_id`（逻辑 ID）和 `device_phy_id`（物理 ID）。
- **DSMI 与 DMP**：DSMI（设备系统管理接口）是 DCMI 下层，`dsmi_*` 接口通过 DMP（设备管理协议）报文与设备固件往返；`drvGetDevInfo` 则走本地 `ioctl` 陷本机内核、不往返设备（见 u2-l2）。
- **错误码体系**：DCMI 用独立的负值体系（`DCMI_OK` 为 0），常见如 `DCMI_ERR_CODE_NOT_SUPPORT`、`DCMI_ERR_CODE_OPER_NOT_PERMITTED`、`DCMI_ERR_CODE_INVALID_PARAMETER`。
- **IPMI / BMC**：BMC（Baseboard Management Controller，基板管理控制器）是服务器主板上独立于 CPU/NPU 的管理芯片，通过 IPMI 协议通信，能在设备数据通路挂死时仍然物理复位芯片——这是「带外」复位的物理基础。初学者可把它理解成「另一条不经过 NPU 自身的遥控线」。

> 一个贯穿全讲的直觉：**带内 = 走 NPU 自己的数据通路下发复位命令；带外 = 走 BMC 的独立管理通路物理复位 NPU**。记住这一句，后面的代码就是它的注脚。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [src/custom/include/dcmi_interface_api.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h) | DCMI 对外头文件门面 | 定义 `enum dcmi_reset_channel`、`enum dcmi_unit_type` 与各接口原型 |
| [src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c) | 基础信息查询接口实现 | 查询接口的「缓存读取」范式代表 |
| [src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c) | 热复位接口实现（本讲主角） | 复位入口、权限校验、带内/带外两条通路 |
| [src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get.c) | DCMI 内部信息读取支撑 | 提供 `dcmi_get_card_info` 等内部 getter |
| [src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get_ext.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get_ext.c) | 内部信息读取（扩展） | 带外通道状态查询的真实实现 |
| [src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_ipmi.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_ipmi.c) | IPMI 报文收发 | 带外复位的最终落点（IPMI/BMC） |
| [examples/dcmi/dcmi/2_chip_reset/0_internal_reset/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/0_internal_reset/main.c) | 带内复位样例 | 一行复位调用 |
| [examples/dcmi/dcmi/2_chip_reset/1_external_reset/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/1_external_reset/main.c) | 带外复位样例 | 四步复位流程 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 讲查询接口范式；4.2 讲复位入口与通道分发；4.3 讲带内复位；4.4 讲带外复位。它们覆盖了规格要求的 `dcmi_basic_info_intf`、`dcmi_hot_reset_intf`、`dcmi_inner_info_get` 三个最小模块。

### 4.1 DCMI 查询接口：缓存读取模式（dcmi_basic_info_intf + dcmi_inner_info_get）

#### 4.1.1 概念说明

DCMI 里有大量「查询」接口：查逻辑 ID、查设备类型、查卡列表、查卡上设备数、查 PCIe 槽位号……这些「静态拓扑/基础信息」在 `dcmi_init` 阶段就已经探测好并写进全局缓存 `g_board_details`。因此这类接口的实现几乎不做设备往返，而是统一遵循一个三段式套路：

1. **参数校验**：出参指针非空、`card_id`/`device_id` 非负、缓冲区长度合法。
2. **芯片形态判断**：用 `dcmi_board_chip_type_is_ascend_950()` 等 helper 判断当前产品形态，对不支持的形态直接返回 `DCMI_ERR_CODE_NOT_SUPPORT`（这是 ascend950/A5 大量出现「This product does not support this api」的原因）。
3. **读缓存返回**：遍历 `g_board_details.card_info[]`，按 `card_id` 匹配到对应卡，从结构体里取出字段填进出参，成功返回 `DCMI_OK`。

> 注意与「运行信息查询」区分：`dcmi_running_info_intf.c` 里的 `dcmi_get_device_temperature`、`dcmi_get_device_health`、`dcmi_get_device_utilization_rate` 等是**实时**数据（温度、健康、利用率），它们必须经 DMP 报文往返设备才能拿到，**不是**读缓存。本讲的查询范式专指「基础/拓扑信息」那一类。区分二者最简单的办法：信息会不会随时间变？会变 → 往返设备；不变（逻辑 ID、设备类型、卡列表）→ 读缓存。

#### 4.1.2 核心流程

以 `dcmi_get_device_logic_id(card_id, device_id)` 为代表的查询流程伪代码：

```text
func dcmi_get_device_logic_id(*logic_id, card_id, device_id):
    if logic_id == NULL:               # ① 参数校验
        return INVALID_PARAMETER
    if card_id < 0 or device_id < 0:
        return INVALID_PARAMETER
    if 当前是 ascend950 且未开 ENABLE_EQUIPMENT:   # ② 形态判断
        return NOT_SUPPORT
    if run_env 未初始化:
        return NOT_REDAY
    if g_board_details.device_count == 0:
        return INVALID_DEVICE_ID
    for 每张卡 in g_board_details.card_info[]:    # ③ 遍历缓存匹配
        if 卡.card_id == card_id:
            if device_id >= 卡.device_count:
                return INVALID_PARAMETER 或 NOT_SUPPORT
            *logic_id = 卡.device_info[device_id].logic_id
            return OK
    return INVALID_PARAMETER
```

内部支撑函数 `dcmi_get_card_info` 则更进一步——直接返回指向缓存中 `dcmi_card_info` 结构体的**指针**，供复位等流程复用，避免重复遍历。

#### 4.1.3 源码精读

`dcmi_get_device_logic_id`：把 `(card_id, device_id)` 映射成 `logic_id`，典型三段式。注意它读的就是 `g_board_details` 缓存。

[dcmi_basic_info_intf.c:37-94](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L37-L94) —— 参数校验 → ascend950 形态判断 → 遍历 `g_board_details.card_info` 取出 `device_info[device_id].logic_id` 返回。

`dcmi_get_device_type`：判断设备是 NPU / MCU / CPU，同样是读缓存（按 `device_id` 落在 `device_count` / `mcu_id` / `cpu_id` 哪一段来判类型）。复位流程会反复调用它来确认目标是 `NPU_TYPE`。

[dcmi_basic_info_intf.c:162-210](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L162-L210) —— `device_id < device_count` 判为 `NPU_TYPE`；等于 `mcu_id` 判 `MCU_TYPE`；等于 `cpu_id` 判 `CPU_TYPE`。

枚举定义在头文件里，`enum dcmi_unit_type` 给出取值，复位流程据此分流：

[dcmi_interface_api.h:318-323](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L318-L323) —— `NPU_TYPE=0`、`MCU_TYPE=1`、`CPU_TYPE=2`、`INVALID_TYPE=0xFF`。

`dcmi_get_card_info`（内部 helper）：直接把缓存里匹配到的 `dcmi_card_info` 结构体指针交出去，是查询范式的「底层砖块」：

[dcmi_inner_info_get.c:154-165](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get.c#L154-L165) —— 遍历 `g_board_details.card_info[]`，按 `card_id` 命中后返回其地址，复位/重扫等流程都靠它拿卡信息。

#### 4.1.4 代码实践

**实践目标**：亲手验证「查询接口只是读缓存、不往返设备」。

**操作步骤**：

1. 打开 `dcmi_basic_info_intf.c`，找到 `dcmi_get_device_num_in_card`（[第 257-287 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L257-L287)）。
2. 阅读它，确认它既没有调用任何 `dsmi_*`，也没有 `ioctl`，全程只在 `g_board_details.card_info[]` 里找 `card_id` 命中后返回 `card_info->device_count`。
3. 用同样的方法看 `dcmi_get_card_list`（[第 212-254 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L212-L254)）。
4. 对照 `dcmi_running_info_intf.c` 里任意一个接口（如 `dcmi_get_device_temperature`），观察它是否含有 `dsmi_*` / DMP 报文收发调用。

**需要观察的现象**：基础信息接口体内只有遍历缓存与赋值；运行信息接口体内会出现向设备下发命令的调用。

**预期结果**：你能用一句话区分两类接口——「基础/拓扑信息读缓存，运行/实时信息往返设备」。

> 命令行编译运行需 NPU 环境与 root 权限；本实践以源码阅读为主，无法在普通机器上运行，结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dcmi_get_device_logic_id` 要在函数开头判断 `dcmi_board_chip_type_is_ascend_950()` 并返回 `NOT_SUPPORT`？

**答案**：ascend950（A5）走的是 `dcmiv2_*` 版本化接口体系（见 u2-l1），其拓扑/逻辑 ID 由 `dcmiv2_get_device_logic_id` 提供；v1 的 `dcmi_get_device_logic_id` 在该形态下不可用，故提前拦截返回 `DCMI_ERR_CODE_NOT_SUPPORT`，避免误用。

**练习 2**：若调用 `dcmi_get_device_type` 时 `dcmi_init` 尚未执行成功，函数会怎样？

**答案**：见 [第 178-181 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L178-L181)：当 `run_env` 未初始化且 `device_count == 0` 时，会把 `*device_type` 置为 `INVALID_TYPE` 并返回 `DCMI_OK`——即「没初始化时返回一个明确的无效类型，而不是报错」。调用方需自行检查返回的类型值。

---

### 4.2 复位入口与通道分发（dcmi_hot_reset_intf）

#### 4.2.1 概念说明

芯片复位是 DCMI 里最「危险」的操作之一——它会中断 NPU 上正在运行的任务、可能让 PCIe 链路掉线再重建。因此 driver 把所有复位动作收敛到**一个对外入口** `dcmi_set_device_reset(card_id, device_id, channel_type)`，由第三个参数 `channel_type`（类型为 `enum dcmi_reset_channel`）决定走哪条底层通路：

| 通道宏 | 值 | 含义 | 底层落点 |
| --- | --- | --- | --- |
| `OUTBAND_CHANNEL` | 0 | 带外复位 | IPMI 报文 → BMC 物理复位 |
| `INBAND_CHANNEL` | 1 | 带内复位 | DSMI/DMP 命令 → NPU 固件自复位 |

枚举定义见头文件：

[dcmi_interface_api.h:313-316](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L313-L316) —— `OUTBAND_CHANNEL = 0`，`INBAND_CHANNEL = 1`。

对外原型（含 `DCMIDLLEXPORT` 导出）：

[dcmi_interface_api.h:2064](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2064) —— `int dcmi_set_device_reset(int card_id, int device_id, enum dcmi_reset_channel channel_type);`

> 旁注：源码里还有 `dcmi_reset_device`（带外薄封装）和 `dcmi_reset_device_inband`（带内薄封装），它们位于 `#if defined DCMI_VERSION_1` 块内，内部其实就是转调 `dcmi_set_device_reset(..., OUTBAND_CHANNEL/INBAND_CHANNEL)`。新代码统一用 `dcmi_set_device_reset` 三参版本即可。

#### 4.2.2 核心流程

`dcmi_set_device_reset` 的总流程：

```text
func dcmi_set_device_reset(card_id, device_id, channel_type):
    err = dcmi_check_device_reset_permission(channel_type)   # ① 权限/形态大门
    if err != OK: return err
    if card_id == ALL_DEVICE_RESET_CARD_ID 且 channel != INBAND:
        return INVALID_PARAMETER                              # 全卡复位只允许带内
    # ② 取设备类型（950 走 dcmiv2_get_device_type，其余走 dcmi_get_device_type）
    device_type = get_device_type(...)
    if 是 310p 双芯片卡 且 device_id != 0:
        return NOT_SUPPORT                                     # SMP 模式只能复位 die0
    if device_type != NPU_TYPE:
        return NOT_SUPPORT                                     # 只复位 NPU
    # ③ 按通道分发
    return execute_npu_reset(card_id, device_id, channel_type)
```

`execute_npu_reset` 就是一个按 `channel_type` 的 switch：

```text
func execute_npu_reset(card_id, device_id, channel_type):
    switch channel_type:
        INBAND_CHANNEL:  return dcmi_set_npu_device_reset_inband(card_id, device_id)   # → 4.3
        OUTBAND_CHANNEL: return dcmi_set_npu_device_reset_outband(card_id, device_id)  # → 4.4
        default:         return NOT_SUPPORT
```

**权限校验是复位的第一道、也是最重的一道关**。`dcmi_check_device_reset_permission` 会依次检查：是否 root 用户、是否物理机（虚拟机/容器里默认只允许带内、且仅特定芯片形态的特权容器允许带外）、910B 标卡在 VM/容器里直接禁止热复位、A2/A3/A5 在特权容器里还要过 `dcmi_check_a2_a3_a5_device_reset_docker_permission`。任何一关不过都返回 `DCMI_ERR_CODE_OPER_NOT_PERMITTED`。这解释了为什么复位类接口对运行环境极其敏感。

#### 4.2.3 源码精读

`dcmi_set_device_reset` —— 对外入口，串联权限校验、设备类型确认与分发：

[dcmi_hot_reset_intf.c:336-384](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L336-L384) —— 注意第 351-362 行按 ascend950 分支选择 `dcmiv2_get_device_type` / `dcmi_get_device_type`，第 375 行调用 `execute_npu_reset` 分发。

`execute_npu_reset` —— 通道分发的 switch：

[dcmi_hot_reset_intf.c:317-334](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L317-L334) —— `INBAND_CHANNEL` 走 `dcmi_set_npu_device_reset_inband`，`OUTBAND_CHANNEL` 走 `dcmi_set_npu_device_reset_outband`。

`dcmi_check_device_reset_permission` —— 权限大门，理解复位「为什么在 VM/容器里经常失败」的关键：

[dcmi_hot_reset_intf.c:219-265](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L219-L265) —— 非 root 直接拒；非物理机走 `dcmi_check_permission_by_channel_type`（带外仅特定形态特权容器可用）；910B 标卡在 VM/容器里禁止热复位。

#### 4.2.4 代码实践

**实践目标**：理解「全卡复位只允许带内」这一约束的代码来源。

**操作步骤**：

1. 打开 [dcmi_hot_reset_intf.c:336-384](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L336-L384)。
2. 定位第 346-349 行：`if (card_id == ALL_DEVICE_RESET_CARD_ID && channel_type != INBAND_CHANNEL)` 返回 `INVALID_PARAMETER`。
3. 思考：为什么对「全部卡一起复位」要强制走带内？（提示：带外依赖逐卡 PCIe 槽位 + BMC，而全卡复位语义是「一次 DMP 命令复位整机 NPU」，对应 `dcmi_set_all_npu_hot_reset`，只能走带内 DMP。）

**预期结果**：能解释该约束来源于「带外复位是逐槽位/逐卡的 IPMI 操作，无法表达『一次复位所有卡』的语义」。

> 本实践为源码阅读型，无需运行，结论「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`dcmi_set_device_reset` 在分发前为什么一定要先确认 `device_type == NPU_TYPE`？

**答案**：因为一张卡上除 NPU 外还可能有 MCU（管理控制单元）、CPU，复位实现 `dcmi_set_npu_device_reset_inband/outband` 只针对 NPU 芯片。若放行 MCU/CPU，会走到未实现的分支，故提前返回 `NOT_SUPPORT`。

**练习 2**：`execute_npu_reset` 的 `default` 分支返回什么？为什么需要它？

**答案**：返回 `DCMI_ERR_CODE_NOT_SUPPORT`（[第 329-330 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L329-L330)）。`enum dcmi_reset_channel` 当前只有两个合法值，但 switch 不依赖枚举封闭性——防止调用方传入非法整数值（如随手传 2）时落入未定义行为，是防御性编程。

---

### 4.3 带内热复位（INBAND_CHANNEL）

#### 4.3.1 概念说明

**带内复位**让 NPU「自我了断」：复位命令经 NPU **自身的数据通路**（即正常访问设备的那条路）下发，由设备固件收到后执行自复位。这条路的载体就是 u2-l2 讲过的 DSMI/DMP 协议——具体是 `dsmi_hot_reset_atomic(device_id, DSMI_SUBCMD_HOTRESET_ASSEMBLE)`，它构造一条 DMP 热复位子命令往返设备。

带内复位的特点：

- **前提**：设备数据通路必须活着。如果 NPU 已经挂死、DMP 报文收不到响应，带内复位就会失败——这正是带外复位存在的理由。
- **步骤极简**：从用户角度看，带内复位就是一次 `dcmi_set_device_reset(card_id, device_id, INBAND_CHANNEL)` 调用，无需 pre-reset / rescan，因为链路不会整体掉线重建。
- **支持「全片/全卡」语义**：当 HCCS 互联（多芯片高速互联）开启时，带内会自动升级为「全片一起复位」（`hccs_reset_all` → `dcmi_set_all_npu_hot_reset`），避免只复位一片导致互联状态不一致。

#### 4.3.2 核心流程

```text
dcmi_set_device_reset(.., INBAND_CHANNEL)
  └─ execute_npu_reset → dcmi_set_npu_device_reset_inband(card_id, device_id)
       ├─ 校验板型（必须是 card/server/model）
       ├─ dcmi_get_hccs_status_inband(...)            # 判 HCCS 互联
       │     若 HCCS_ON → hccs_reset_all()            # 全片复位，提前返回
       ├─ (910_93/910B) dcmi_clear_running_proc()     # 清理占用 NPU 的白名单进程
       ├─ (910_93) 兄弟卡联动复位 dcmi_reset_brother_card()
       ├─ dcmi_npu_msn_env_clean(card_id)             # 杀 msnpureport 日志传输进程
       └─ dcmi_call_dsmi_hot_reset(device_logic_id)
             └─ dsmi_hot_reset_atomic(id, DSMI_SUBCMD_HOTRESET_ASSEMBLE)   # DMP 报文往返设备
```

注意 `dcmi_call_dsmi_hot_reset` 是关键的「跨层」点——它把 DCMI 层的复位意图转交给 DSMI 层（ascend_hal 树），最终通过 DMP 报文下发到设备固件。错误码会经 `dcmi_convert_error_code` 从 DSMI 体系转换回 DCMI 体系。

#### 4.3.3 源码精读

`dcmi_set_npu_device_reset_inband` —— 带内复位主实现，含 HCCS 全片升级、进程清理与 DSMI 下发：

[dcmi_hot_reset_intf.c:1625-1678](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L1625-L1678) —— 第 1635 行判 HCCS；第 1650-1652 行 `hccs_status == HCCS_ON` 时走 `hccs_reset_all`；第 1672 行调用 `dcmi_call_dsmi_hot_reset`。

`dcmi_call_dsmi_hot_reset` —— 跨层桥接，调用 DSMI 原子热复位：

[dcmi_hot_reset_intf.c:56-73](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L56-L73) —— Linux 下走 `dsmi_hot_reset_atomic(device_logic_id, DSMI_SUBCMD_HOTRESET_ASSEMBLE)`，失败经 `dcmi_convert_error_code` 转码。

样例对照——带内复位（`0_internal_reset`）只用一次调用：

[0_internal_reset/main.c:23-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/0_internal_reset/main.c#L23-L44) —— 声明 `inband_channel = INBAND_CHANNEL`，第 37 行直接 `dcmi_set_device_reset(card_id_list[0], device_id, inband_channel)`，无 pre-reset、无 rescan。

#### 4.3.4 代码实践

**实践目标**：看清带内复位在样例里的「一击」形态。

**操作步骤**：

1. 阅读 [0_internal_reset/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/0_internal_reset/main.c) 全文（46 行）。
2. 确认它相对 `1_external_reset` 缺少了哪些步骤（答案：缺少「查带外通道 → pre_reset → rescan」）。
3. 在 `dcmi_hot_reset_intf.c` 里找到 `dcmi_call_dsmi_hot_reset`（[第 56-73 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L56-L73)），追踪到 `dsmi_hot_reset_atomic`，确认这是 DSMI 层接口（u2-l2 讲过的 DMP 通路）。

**需要观察的现象**：带内样例 `main` 函数体里只有「init → 取卡列表 → 一次 `dcmi_set_device_reset(INBAND)`」三步。

**预期结果**：理解带内复位的简洁性来自「设备自复位、链路不掉」，所以无需重建 PCIe 拓扑。

> 实际复位 NPU 需要 root + 物理机 + 真实硬件，本机一般无法运行，结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 HCCS 互联开启时，带内复位要走「全片一起复位」而不是单芯片？

**答案**：HCCS 把多颗芯片高速互联成一组，片间状态互相依赖。若只复位其中一片，会导致互联拓扑与缓存一致性状态错乱；因此 `dcmi_set_npu_device_reset_inband` 在 `hccs_status == HCCS_ON` 时改走 `hccs_reset_all` → `dcmi_set_all_npu_hot_reset`，用一条 `DSMI_SUBCMD_HOTRESET_ASSEMBLE` 命令把整组芯片一起复位，保持状态一致。

**练习 2**：`dcmi_call_dsmi_hot_reset` 里 `dsmi_hot_reset_atomic` 失败后为什么要调 `dcmi_convert_error_code`？

**答案**：DSMI 用 `DSMI_OK`（0）及一套负值错误码，DCMI 用另一套（如 `DCMI_ERR_CODE_*`）。两层错误码体系不同，必须经 `dcmi_convert_error_code` 把 DSMI 错误码翻译成 DCMI 错误码，才能统一向 DCMI 调用方返回。

---

### 4.4 带外热复位（OUTBAND_CHANNEL）

#### 4.4.1 概念说明

**带外复位**不依赖 NPU 自身的数据通路，而是经由 **BMC/IPMI** 这条独立的管理通路，由主板上的 BMC 对指定 PCIe 槽位上的芯片执行物理复位。它的价值在于：**当 NPU 彻底挂死、带内命令无响应时，带外仍能把芯片拉起来**——因为 BMC 是独立的硬件管理通道，不经过 NPU 的数据面。

代价是流程复杂得多。物理复位会让该槽位的 PCIe 链路彻底掉线再重建，因此必须配套：

- **查通道状态**：先确认 BMC 这条带外管理通路本身是通的（`dcmi_get_device_outband_channel_state`）。
- **预复位 pre_reset**：复位前关闭上游 PCIe 端口（`dcmi_set_device_pre_reset` → `dcmi_set_npu_device_close_pcie_upstream`），避免复位瞬间主机端 PCIe 树异常。
- **执行复位**：通过 IPMI 命令让 BMC 复位目标槽位芯片（`dcmi_ipmi_reset_npu`），并带重试与状态轮询。
- **重扫 rescan**：复位完成后重新打开上游端口、触发设备重扫（`dcmi_set_device_rescan`），让主机重新发现并加载该 NPU。

这就是为什么 `1_external_reset` 样例有四步，而 `0_internal_reset` 只有一步。

#### 4.4.2 核心流程

```text
带外复位推荐四步（见 1_external_reset 样例）：
  ① dcmi_get_device_outband_channel_state(card, dev, &state)   # 确认 BMC 通路可达
  ② dcmi_set_device_pre_reset(card, dev)                        # 关上游 PCIe 端口
  ③ dcmi_set_device_reset(card, dev, OUTBAND_CHANNEL)           # 实际复位
        └─ execute_npu_reset → dcmi_set_npu_device_reset_outband
              └─ (静态) dcmi_set_npu_device_reset(..)
                    └─ dcmi_ipmi_reset_npu(slot_id, outband_id) # IPMI 报文 → BMC
                          └─ dcmi_ipmi_cmd(...)                 # IPMI 收发，含重试+状态轮询
  ④ sleep(3) → dcmi_set_device_rescan(card, dev)                # 重开端口 + 重扫设备
```

其中第 ③ 步内部，静态函数 `dcmi_set_npu_device_reset` 会循环最多 `MAX_RETRY_CNT` 次：每次先 `dcmi_ipmi_reset_npu` 下发复位，再用 `dcmi_ipmi_get_npu_reset_state` 轮询 BMC 返回的复位状态是否为 `BMC_RESET_CHIP_SUCCESS`；频繁复位会丢芯片，故每次重试间 `sleep(DCMI_RESET_MIN_DELAY)`。注释明确指出：复位命令的返回值只表示「BMC 收到命令」，并不代表动作执行成功，必须靠状态轮询确认。

`dcmi_ipmi_reset_npu` 是带外复位的最终落点。它构造一条 IPMI 报文：借用 PICMG（PICMG 是 ATCA/计算架构标准组织）的「设置 FRU LED 状态」命令字，把槽位号、芯片编号、复位标志塞进请求数据，通过 `dcmi_ipmi_cmd` 发给 BMC。

#### 4.4.3 源码精读

`dcmi_set_npu_device_reset_outband` —— 带外复位入口，处理 910_93 兄弟卡、槽位号获取与分发：

[dcmi_hot_reset_intf.c:1838-1891](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L1838-L1891) —— 第 1849-1855 行拦截不支持带外的形态（如 A5 标卡）；第 1875 行 `dcmi_get_pcie_slot` 取槽位号；第 1882 行调静态 `dcmi_set_npu_device_reset`。

静态 `dcmi_set_npu_device_reset` —— IPMI 复位 + 状态轮询重试循环：

[dcmi_hot_reset_intf.c:1728-1782](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L1728-L1782) —— 第 1751 行起 `for` 循环；ascend950 走 `dcmi_ipmi_reset_npu_950`，其余走 `dcmi_ipmi_reset_npu`；非 950 还要 `dcmi_ipmi_get_npu_reset_state` 轮询是否 `BMC_RESET_CHIP_SUCCESS`；每次失败 `sleep(DCMI_RESET_MIN_DELAY)`。

`dcmi_ipmi_reset_npu` —— 带外复制的物理落点，构造并发送 IPMI 报文：

[dcmi_ipmi.c:287-319](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_ipmi.c#L287-L319) —— 填充槽位号、芯片号（`chip_id+1`）、复位标志 `0x01`，命令字 `PICMG_SET_FRU_LED_STATE_CMD`，经 `dcmi_ipmi_cmd` 发往 BMC（`IPMI_BMC_LUN`）；响应 `rsp_data[0] != 0x00` 视为失败。

`dcmi_set_device_pre_reset` —— 预复位（权限校验 + 关上游端口）：

[dcmi_hot_reset_intf.c:148-194](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L148-L194) —— 校验物理机/特权容器、查设备类型，最终调 `dcmi_set_npu_device_pre_reset`（内部会 `dcmi_set_npu_device_close_pcie_upstream` 关上游端口）。

`dcmi_set_device_rescan` —— 重扫（重开上游端口 + 触发设备重扫）：

[dcmi_hot_reset_intf.c:386-425](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L386-L425) —— 内部调 `dcmi_set_npu_device_rescan`，先 `dcmi_set_npu_device_open_pcie_upstream` 再 `dsmi_hot_reset_atomic(id, DSMI_SUBCMD_HOTRESET_RESCAN)`。

`dcmi_get_device_outband_channel_state` / `dcmi_get_device_npu_outband_channel_state` —— 通道可达性查询：

[dcmi_hot_reset_intf.c:475-521](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L475-L521) 与 [dcmi_inner_info_get_ext.c:1613-1644](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_inner_info_get_ext.c#L1613-L1644) —— 910_93/A5 Pod/Server 通过 `dcmi_get_npu_outband_channel_state`（拿 BMC 版本号探活）判断；其余通过 `dcmi_get_npu_outband_reset_state` 读 BMC 复位状态码是否合法来判断通路。

样例对照——带外复位（`1_external_reset`）的四步流程：

[1_external_reset/main.c:16-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/1_external_reset/main.c#L16-L60) —— 依次 `dcmi_get_device_outband_channel_state`（第 22 行）→ `dcmi_set_device_pre_reset`（第 34 行）→ `dcmi_set_device_reset(..., OUTBAND_CHANNEL)`（第 42 行）→ `sleep(3)` → `dcmi_set_device_rescan`（第 53 行）。注意第 26-28 行：返回 `-8255` 即 `DCMI_ERR_CODE_NOT_SUPPORT` 时打印「该设备不支持带外复位」。

#### 4.4.4 代码实践

**实践目标**：对比两个样例，说清带内/带外在「调用步骤」与「底层通信接口」上的差异。

**操作步骤**：

1. 并排打开 [0_internal_reset/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/0_internal_reset/main.c) 与 [1_external_reset/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/2_chip_reset/1_external_reset/main.c)。
2. 填写下面这张对照表（先自己填，再核对下方的参考答案）：

   | 维度 | 带内（0_internal_reset） | 带外（1_external_reset） |
   | --- | --- | --- |
   | 通道宏 | ? | ? |
   | 调用步骤数 | ? | ? |
   | 是否需要 pre_reset/rescan | ? | ? |
   | `dcmi_set_device_reset` 内部落点 | ?（函数名） | ?（函数名） |
   | 底层通信接口 | ? | ? |

3. 在 `dcmi_hot_reset_intf.c` 中验证：带内最终经 `dcmi_call_dsmi_hot_reset` → `dsmi_hot_reset_atomic`（DSMI/DMP）；带外最终经静态 `dcmi_set_npu_device_reset` → `dcmi_ipmi_reset_npu` → `dcmi_ipmi_cmd`（IPMI/BMC）。

**参考答案**：

| 维度 | 带内（0_internal_reset） | 带外（1_external_reset） |
| --- | --- | --- |
| 通道宏 | `INBAND_CHANNEL`(=1) | `OUTBAND_CHANNEL`(=0) |
| 调用步骤数 | 1（仅 `dcmi_set_device_reset`） | 4（查通道→pre_reset→reset→rescan） |
| 是否需要 pre_reset/rescan | 否 | 是 |
| `dcmi_set_device_reset` 内部落点 | `dcmi_set_npu_device_reset_inband` | `dcmi_set_npu_device_reset_outband` |
| 底层通信接口 | DSMI/DMP（`dsmi_hot_reset_atomic`，经设备数据通路） | IPMI/BMC（`dcmi_ipmi_reset_npu`，经独立管理通路） |

**需要观察的现象**：带外复位涉及 PCIe 链路掉线重建，故需要 pre_reset 关端口、rescan 重开重扫；带内复位链路不掉，故一步到位。

**预期结果**：能用「链路是否重建」一句话解释步骤数差异；能指出带内依赖 DSMI/DMP、带外依赖 IPMI/BMC 这两个不同的底层通信接口。

> 实际运行需 root + 物理机 + 真 NPU + 带 BMC 的服务器；普通开发机无法复现，结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：注释说「复位命令的返回值只表示 BMC 收到命令，并不代表动作执行成功」。代码是怎么确认复位真的成功的？

**答案**：见静态 [dcmi_set_npu_device_reset:1751-1780](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L1751-L1780)：下发 `dcmi_ipmi_reset_npu` 后，再调 `dcmi_ipmi_get_npu_reset_state` 轮询 BMC 返回的状态码，只有等于 `BMC_RESET_CHIP_SUCCESS` 才算成功；否则在 `MAX_RETRY_CNT` 次内重试，每次间隔 `sleep(DCMI_RESET_MIN_DELAY)`。全部失败则返回 `DCMI_ERR_CODE_INNER_ERR`。

**练习 2**：为什么带外复位在「`dcmi_set_device_reset` 之后」还需要 `dcmi_set_device_rescan`，而带内复位不需要？

**答案**：带外是 BMC 物理复位，会让该槽位 PCIe 链路彻底断开再重建——主机侧原本挂在该槽位下的设备节点会失效，必须重开上游端口并触发重扫（`dsmi_hot_reset_atomic(id, DSMI_SUBCMD_HOTRESET_RESCAN)`）才能重新发现并加载 NPU。带内是设备固件自复位，PCIe 链路本身不断，设备节点仍在，故无需 rescan。

**练习 3**：`dcmi_ipmi_reset_npu` 里芯片号为什么要写成 `chip_id + 1`？

**答案**：见 [dcmi_ipmi.c:297](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_ipmi.c#L297)。软件侧 `chip_id` 从 0 开始编号，而 BMC/IPMI 侧的「芯片编号」从 1 开始（0 留作特殊用途），故上报 BMC 时需 `+1` 偏移。这是两层编号约定不一致的典型适配点。

---

## 5. 综合实践

**任务**：绘制一张「DCMI 芯片复位决策与调用图」，把本讲四个模块串起来。

要求：

1. 以 `dcmi_set_device_reset(card_id, device_id, channel_type)` 为起点。
2. 画出两条分支：
   - `INBAND_CHANNEL`：经过哪些函数，最终落到哪个底层通信接口（DSMI/DMP）。标注出 HCCS 互联时的「全片复位」旁路。
   - `OUTBAND_CHANNEL`：画出完整的「查通道 → pre_reset → reset（含 IPMI 重试与状态轮询） → rescan」四步，并标出最终底层通信接口（IPMI/BMC）。
3. 在图上用一句话标注每一步的**为什么**（例如：「pre_reset：关上游端口，避免物理复位瞬间 PCIe 树异常」）。
4. 在图旁附一张「带内 vs 带外」对照表（步骤数、是否重建链路、底层接口、适用场景：设备存活 vs 设备挂死）。

**进阶（可选）**：阅读 `dcmi_check_device_reset_permission`（[第 219-265 行](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_hot_reset_intf.c#L219-L265)），在图的最前端补一个「权限/形态判断」前置节点，列出 root、物理机、VM/容器、芯片形态四个维度的通过条件。

> 提示：本实践为「源码阅读 + 画图」型，无需真实硬件；若你恰好有 root + 物理机 + NPU 环境，可先在测试卡上跑通 `0_internal_reset`（影响小、可逆性较好），切勿在生产环境贸然执行复位。

## 6. 本讲小结

- DCMI **基础信息查询**遵循「参数校验 → 芯片形态判断（ascend950 常被拦截）→ 读 `g_board_details` 缓存」三段式，**不往返设备**；运行信息（温度/健康/利用率）才往返设备，二者要区分。
- 所有芯片复位收敛到单一入口 `dcmi_set_device_reset(card_id, device_id, channel_type)`，权限校验（root/物理机/形态）是第一道重关。
- `execute_npu_reset` 按 `channel_type` 分发：`INBAND_CHANNEL` → `dcmi_set_npu_device_reset_inband`；`OUTBAND_CHANNEL` → `dcmi_set_npu_device_reset_outband`。
- **带内复位**经 DSMI/DMP（`dsmi_hot_reset_atomic`）走设备自身数据通路，一步到位，HCCS 互联时升级为全片复位；前提是设备数据通路存活。
- **带外复位**经 IPMI/BMC（`dcmi_ipmi_reset_npu`）走独立管理通路，物理复位会让 PCIe 链路掉线重建，故需「查通道 → pre_reset → reset（重试+轮询） → rescan」四步；能在设备挂死时仍复位成功。
- `dcmi_inner_info_get`（`dcmi_get_card_info` 等）和 `dcmi_inner_info_get_ext`（`dcmi_get_device_npu_outband_channel_state`）为查询与复位提供内部支撑，是复用 `g_board_details` 缓存的底层砖块。

## 7. 下一步学习建议

- **继续 DCMI 体系**：建议进入「DCMI 网络接口实现（HCCN/多芯片适配）」相关讲义，看看 DCMI 如何把网络管理与多芯片形态（ascend910B/ascend950）适配结合起来——你会再次看到 ascend950 走 `dcmiv2_*` 与动态加载的套路。
- **下沉 DSMI/DMP**：本讲带内复位最终落到 `dsmi_hot_reset_atomic`。若想彻底理解 DMP 报文如何构造与收发，建议复习 u2-l2（DSMI 设备系统管理接口实现），并阅读 `dsmi_dmp_command.c` 中 `DSMI_SUBCMD_HOTRESET_*` 系列子命令的定义。
- **对照 DSMI 复位接口**：阅读 `dsmi_common_interface.c` 中 `dsmi_hot_reset_atomic`（约第 1662 行起）的完整实现，理解 DCMI → DSMI 的跨层调用与错误码转换链路。
- **故障与恢复方向**：复位常与故障管理配套。后续可关注 `dcmi_fault_manage_intf.c` 与 SDK-driver 层的 FMS 故障管理系统，理解「检测到故障 → 软故障处理 → 复位恢复」的完整闭环。
