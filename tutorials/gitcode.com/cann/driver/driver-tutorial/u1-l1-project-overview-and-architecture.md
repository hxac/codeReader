# driver 项目定位与三层架构总览

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是帮你建立一个「全局心智模型」。学完本讲你应该能够：

- 说清楚 `driver` 仓在昇腾 **CANN** 软件栈中扮演什么角色、负责什么。
- 区分 **DCMI 层、HAL 层、SDK-driver 层** 三层各自的职责，并知道它们分别对应仓库里的哪个源码目录。
- 画出一次「上层应用调用 → 自上而下穿过三层 → 到达 NPU 设备」的整体路径。

本篇不要求你懂内核或驱动细节，所有概念都会从零讲起。后续每一篇讲义都会落在这个心智模型的某一个局部上。

## 2. 前置知识

在进入源码之前，先用大白话对齐几个术语。

### 2.1 什么是「驱动」

一块 NPU（神经网络处理器）芯片插在主板上，操作系统并不能直接「认识」它。**驱动（driver）** 就是操作系统和硬件之间的一层「翻译官」：

- 往上，它给应用程序提供一套**接口**（比如「申请显存」「查询芯片温度」）。
- 往下，它知道**怎么指挥硬件**（往哪个寄存器写什么值、怎么走 PCIe 总线把命令送过去）。

本仓库 `driver` 就是昇腾 AI 处理器的驱动模块。

### 2.2 Host 与 Device

昇腾场景里有两个物理角色：

- **Host（主机）**：你运行程序的通用服务器（CPU + 内存 + 操作系统，通常是 x86 或 ARM 的 Linux）。
- **Device（设备）**：插在 Host 上的 NPU 加速卡，有自己的算力、显存（HBM）和一个小系统。

驱动的一大半工作，就是让 Host 上的程序能「指挥」Device 干活，并把 Device 的状态/日志/数据「取」回 Host。

### 2.3 用户态与内核态

Linux 把内存和权限分成两块：

- **用户态（user space）**：普通程序运行的地方，权限受限，不能直接碰硬件。运行时库、管理工具都在这里。
- **内核态（kernel space）**：操作系统内核运行的地方，权限最高，能直接操作硬件、中断、DMA。驱动程序（以 `.ko` 内核模块形式加载）运行在这里。

一次调用通常要「从用户态进内核态，再到设备」。理解这条「降权限」的路径，是理解本仓三层架构的关键。

### 2.4 CANN 是什么

**CANN**（Compute Architecture for Neural Networks，神经网络计算架构）是昇腾的完整软件栈，从底层的驱动/固件，到中间的运行时、算子库，一直到上层的训练/推理框架适配。`driver` 仓位于这套软件栈的**最底层**，是「使能芯片」的地基。本仓 README 开篇一句话就点明了定位：

> Driver 仓的代码是 CANN 的驱动模块，提供基础驱动和资源管理及调度等功能，使能昇腾芯片。

## 3. 本讲源码地图

本讲涉及的关键文件（先有个印象即可，后面会逐个精读）：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目概述、三层架构定义、完整目录结构说明。 |
| `docs/zh/QUICKSTART.md` | 快速上手指南，含编译部署、调试、新增接口的开发示例。 |
| `docs/zh/figures/Driver_architecture.png` | 官方三层架构分层图。 |
| `src/CMakeLists.txt` | 源码编译入口，决定了三大源码树如何被组织进构建。 |
| `src/ascend_hal/` | **HAL 层**源码（用户态驱动库）。 |
| `src/sdk_driver/` | **SDK-driver 层**源码（内核态驱动模块）。 |
| `src/custom/` | **定制化特性源码库**，DCMI 接口的具体实现就在这里。 |
| `pkg_inc/` | 对外公共头文件（`ascend_hal.h`、`dsmi_common_interface.h` 等）。 |
| `src/custom/include/dcmi_interface_api.h` | DCMI 接口的公共头文件。 |
| `examples/dcmi/.../main.c` | DCMI 查询 PCIe 信息的可运行样例。 |

## 4. 核心概念与源码讲解

我们先在 [README.md:L10-L14](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L10-L14) 里看到三层架构的官方定义：当前开源仓主要包含 **DCMI 层、HAL 层、SDK-driver 层** 三部分（架构图见 [README.md:L16-L18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L16-L18)）。

下面把这三层当作三个最小模块，逐一拆解。

### 4.1 DCMI 层：达芬奇卡管理接口

#### 4.1.1 概念说明

**DCMI**（DaVinci Card Management Interface，达芬奇卡管理接口）是面向「**管理**」的对外接口层。

「管理」是什么意思？打个比方：你买了一台服务器插了 8 张 NPU，运维人员要查询每张卡的型号、温度、健康状态、PCIe 信息，要给卡做热复位、升级固件——这些「管卡」的动作，都通过 DCMI 暴露的 `dcmi_*` 系列接口完成。它的典型调用方是管理工具（如本仓的 `npucli`、或社区的 `npu-smi`）。

需要注意一个容易混淆的点：DCMI 在**概念上**是最上层的管理接口，但它的**代码实现**却放在 `src/custom/dev_prod/user/dcmi/` 这个定制化目录里，公共头文件是 [src/custom/include/dcmi_interface_api.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h)。也就是说，「DCMI 层」对应的是 `custom` 源码树下的 `dcmi` 子模块。

#### 4.1.2 核心流程

一个典型的 DCMI 管理调用流程：

```text
管理工具 (npucli / npu-smi)
   │  调用 dcmi_* 接口
   ▼
DCMI 接口层 (custom/dev_prod/user/dcmi)
   │  参数校验 → 环境判断 → 调用下层 dsmi_* 原语
   ▼
（进入 HAL 层，见 4.2）
```

DCMI 的标准用法是「先初始化、再枚举卡、再逐卡查询」。以查询 PCIe 信息为例，三步固定动作是：`dcmi_init` → `dcmi_get_card_num_list` → `dcmi_get_device_pcie_info`。

#### 4.1.3 源码精读

先看接口声明。这三个核心函数都在 DCMI 的公共头文件里：

- [src/custom/include/dcmi_interface_api.h:L2001](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2001) 声明 `dcmi_init(void)`，DCMI 的初始化入口。
- [src/custom/include/dcmi_interface_api.h:L3613](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L3613) 声明 `dcmi_get_card_num_list`，枚举系统里有多少张卡、卡的 ID 列表。
- [src/custom/include/dcmi_interface_api.h:L2019](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2019) 声明 `dcmi_get_device_pcie_info`，查询单张卡的 PCIe 信息。

再看可运行样例 [examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c:L22-L40](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c#L22-L40)，它把上面三个接口串成了完整调用链：

```c
ret = dcmi_init();                                   // 1. 初始化
ret = dcmi_get_card_num_list(&card_count, card_id_list, MAX_CARD_NUM);  // 2. 枚举卡
for (int i = 0; i < card_count; i++) {
    ret = dcmi_get_device_pcie_info(card_id_list[i], device_id, &pcie_info);  // 3. 逐卡查询
}
```

最后看 DCMI 接口的**实现**（注意实现不在头文件，而在 `custom` 树的 `dcmi_interface/src` 下）。以 [src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c:L793-L824](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L793-L824) 中的 `dcmi_get_device_pcie_info` 为例，可以看到 DCMI 层典型套路：先做空指针校验、再做芯片型号/板型兼容判断（如 `dcmi_board_chip_type_is_ascend_950()` 不支持就直接返回 `DCMI_ERR_CODE_NOT_SUPPORT`），最后才把真正干活的事交给 `dcmi_get_npu_pcie_info`（后者会进一步下沉到 HAL 层）。

这正是 DCMI 层的职责定位：**参数校验 + 产品形态适配 + 转调下层原语**，它本身不直接碰硬件。

#### 4.1.4 代码实践

**实践目标**：从源码读出 DCMI 层「只做校验和适配、不直接碰硬件」这个定位。

**操作步骤**：

1. 打开 [src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c:L793](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_interface/src/dcmi_basic_info_intf.c#L793) 的 `dcmi_get_device_pcie_info`。
2. 数一数函数体里有几处 `return DCMI_ERR_CODE_*`（参数非法、不支持），以及最终把工作交给哪个内部函数。
3. 列出该函数里出现的「芯片/板型判断」调用（如 `dcmi_board_chip_type_is_ascend_950`、`dcmi_mainboard_is_a900_a5_ub`）。

**需要观察的现象 / 预期结果**：你会看到函数前半段几乎都是 `if (...) return 错误码;` 的守卫语句，真正的取值动作在最后一行转调里。这说明 DCMI 是「门面 + 路由」，而非「干活的工人」。结果待本地结合源码确认。

#### 4.1.5 小练习与答案

**练习 1**：DCMI 的公共头文件为什么放在 `src/custom/include/` 而不是 `pkg_inc/`，这说明 DCMI 层和 `custom` 源码树是什么关系？

> **参考答案**：说明 DCMI 接口的**实现与契约**都属于 `custom` 定制化特性库。`custom` 承载了面向设备厂商/产品的定制能力，DCMI 作为「卡管理」对外接口，其具体实现随产品形态而变，因此归在 `custom` 树下，由 `src/custom/include/` 导出公共头。

**练习 2**：在 [dcmi_interface_api.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h) 中，`dcmi_get_device_pcie_info` 旁边还有一个 `dcmi_get_device_pcie_info_v2`。从命名规律推测，DCMI 接口演进的一般方式是什么？

> **参考答案**：DCMI 倾向于用「同名 + `_v2`/`_v3` 后缀」来做接口演进，而不是直接改老接口签名。这样老调用方继续用 v1 不受影响，需要扩展字段（如返回更多信息）的新调用方用 v2，保证向后兼容。

### 4.2 HAL 层：硬件抽象层

#### 4.2.1 概念说明

**HAL**（Hardware Abstraction Layer，硬件抽象层）是本仓**体量最大、最核心**的一层，对应源码树 `src/ascend_hal/`，最终编译成一个**用户态动态库**（`libascend_hal.so`）。

如果说 DCMI 是面向「管理工具」的门面，那么 HAL 就是面向「**计算运行时**」（如昇腾 Runtime / acl）的主力接口层。它把「不同型号芯片、不同通信方式」的硬件差异抽象掉，对上提供统一的 `hal_*` 接口（内存、流、任务、设备信息、DMA 等）。

HAL 层还「兼任」了几个重要基础设施，它们对 DCMI 同样可用：

- **DSMI**（Device System Management Interface，设备系统管理接口）：`dsmi_*` 系列原语，是 DCMI 调用的下一层。
- **HDC**（Host-Device Communication）：主机↔设备的通信底座，所有跨进程/跨设备的消息都走它。
- **SVM**（Shared Virtual Memory）：共享虚拟内存管理。
- **PBL**（Public Base Lib）：UDA/URD/commlib 等基础公共库。

#### 4.2.2 核心流程

HAL 是「承上启下」的中间层：

```text
                  ┌─────────────────────────── 上层调用者 ───────────────────────────┐
管理工具 ──DCMI──► │                                                                 │ ◄──hal_*── 计算运行时(acl/Runtime)
                  └────►  HAL 层 (ascend_hal)  ◄──DSMI(dsmi_*) / HDC / SVM / PBL ────┘
                                      │  通过 ioctl / HDC 把请求送入内核
                                      ▼
                              SDK-driver 内核层 (见 4.3)
```

一句话：DCMI 和 acl 这两类上层调用者，最终都汇入 HAL，再由 HAL 进内核、到设备。

#### 4.2.3 源码精读

HAL 的对外接口通过聚合头文件 [pkg_inc/ascend_hal.h:L11-L18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal.h#L11-L18) 导出，它把 `ascend_hal_base.h`、`ascend_hal_external.h`、`ascend_hal_dc.h` 等几个分头文件打包在一起，是 HAL 的「总入口」。

`hal_*` 接口示例（设备打开 / 能力查询 / 设备信息获取）：

- [pkg_inc/ascend_hal_base.h:L772](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L772) `halDeviceOpen`：打开一个 NPU 设备，返回后续操作所需的句柄信息。这是 Runtime 接管设备的起点。
- [pkg_inc/ascend_hal_base.h:L850](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L850) `halGetChipCapability`：查询芯片能力（算力/核数等），用于上层决定如何调度任务。
- [pkg_inc/ascend_hal_base.h:L1210](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1210) `halGetDeviceInfo`：按模块类型读取设备运行信息（温度、利用率、健康度等）。

再看 DCMI 的下一层——DSMI。它的实现位于 `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c`，文件头部就能看到它依赖 HDC 通信（`dm_hdc.h`、`dsmi_dmp_command.h`），见 [src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c:L22-L33](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c#L22-L33)。这说明 DSMI 通过 HDC 通路与设备交互，而 DSMI 的返回码会被统一映射成对外的 `DRV_ERROR_*`，定义在 [pkg_inc/dsmi_common_interface.h:L40-L54](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L40-L54)（例如 `DM_DDMP_ERROR_CODE_SUCCESS → DRV_ERROR_NONE`）。

> 「DCMI 包 DSMI」的最佳证据来自 QUICKSTART 的开发示例：它演示新增一个 `dcmi_get_host_device_connect_type` 时，函数体内直接转调 `dsmi_get_host_device_connect_type`（见 [docs/zh/QUICKSTART.md:L220-L245](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L220-L245)），而那个 `dsmi_*` 又进一步调用 `drvGetDevInfo` 取设备信息（见 [docs/zh/QUICKSTART.md:L183-L204](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L183-L204)）。这条 `dcmi → dsmi → drvGetDevInfo` 正是「自上而下穿过 DCMI、HAL 两层」的真实代码路径。

#### 4.2.4 代码实践

**实践目标**：亲手在源码里确认「DCMI（custom 树）调用 HAL 树里的 DSMI」这条跨树调用。

**操作步骤**：

1. 在本仓根目录执行 `grep -rn "dsmi_get_host_device_connect_type" src/`（这是 QUICKSTART 指导新增的示例函数；若你已按 QUICKSTART 动手加过，会命中；若未加过，则为空——这本身也说明它是「待新增」的示例）。
2. 再执行 `grep -n "halDeviceOpen\|halGetDeviceInfo" pkg_inc/ascend_hal_base.h`，确认这些 `hal_*` 是 HAL 对外暴露的统一入口。
3. 对照本节时序图，把 `dcmi_*`(custom) → `dsmi_*`(ascend_hal) → HDC 通信 这条链路在脑中走一遍。

**需要观察的现象 / 预期结果**：你会清楚看到 custom 树的 DCMI 实现 `#include` 并调用了 ascend_hal 树里 DSMI 的函数——这就是「DCMI 层架在 HAL 层之上」的代码级证据。`grep` 命令的具体命中行以本地仓库实际状态为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DCMI 和 acl 运行时这两类完全不同的调用者，最终都要经过 HAL 层？

> **参考答案**：因为「如何访问设备、如何与设备通信、如何屏蔽芯片差异」这些复杂度都收敛在 HAL。DCMI 偏管理、acl 偏计算，但落到「真正操作硬件」这一步，都需要 HAL 提供的 DSMI/HDC/内存等原语。HAL 是它们共享的地基。

**练习 2**：HAL 编译出来是用户态库还是内核模块？依据是什么？

> **参考答案**：用户态动态库（`libascend_hal.so`）。依据：源码在 `src/ascend_hal/`，README 标注其为「HAL 层源码」，且它与 `src/sdk_driver/`（内核层）是并列的两个源码树；HAL 通过 `hal_*`/`dsmi_*` 接口供用户态进程直接链接调用。

### 4.3 SDK-driver 层：内核态驱动

#### 4.3.1 概念说明

**SDK-driver 层** 对应源码树 `src/sdk_driver/`，它编译成一个**Linux 内核模块**（`.ko`），运行在**内核态**，是 Host 侧「离硬件最近」的一层。

如果说 HAL 是「用户态翻译官」，那么 SDK-driver 就是「内核态执行者」。它负责那些用户态做不了的事：注册 PCIe 驱动、申请中断、管理预留内存、做任务硬件调度（TRS）、处理故障（FMS）、支持算力切分虚拟化（vascend）等。用户态的 HAL 通过 `ioctl` 等系统调用「陷入」内核，把请求交给 SDK-driver 真正落地。

注意许可：内核模块以 GPL 发布（仓库根有 `LICENSES/GPL-V2.0`），而用户态库走 CANN 商业许可（`LICENSES/CANN-V2.0`）。

#### 4.3.2 核心流程

一次「用户态 → 内核态 → 设备」的完整降层：

```text
用户态进程 (HAL / libascend_hal.so)
   │  ioctl(...) 系统调用，陷入内核
   ▼
SDK-driver 内核模块 (.ko, src/sdk_driver)
   │  解析命令 → 调度硬件资源（中断/DMA/寄存器）
   ▼
NPU 设备 (Device)
```

SDK-driver 内部又分了很多子模块（见 [README.md:L91-L115](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L91-L115) 的目录树），例如：

- `kernel_adapt`：内核源码适配层，屏蔽不同内核版本差异。
- `platform`：芯片资源（中断、预留内存）存储库。
- `trsdrv`：任务资源调度（SQ/CQ 通信、mailbox）。
- `fms`：故障管理系统。
- `vascend`：昇腾算力切分（虚拟化）。

这些子模块会在单元 6 逐篇展开，本讲只需知道「它们都活在内核态」即可。

#### 4.3.3 源码精读

最直接的证据是 `kernel_adapt` 的模块入口 [src/sdk_driver/kernel_adapt/ka_module_init.c:L40-L43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c#L40-L43)：

```c
module_init(ka_module_init);
module_exit(ka_module_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("kernel open adapt module");
```

这几行是 Linux 内核模块的「标准开场」：`module_init/module_exit` 注册加载与卸载回调，`MODULE_LICENSE("GPL")` 声明 GPL 许可，`MODULE_DESCRIPTION` 说明这是「内核开放适配模块」。能出现 `module_init`、`#include <linux/module.h>`，就足以证明它是内核态代码，而非用户态库。

再看构建如何把三层组织到一起。[src/CMakeLists.txt:L15-L21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/CMakeLists.txt#L15-L21) 在构建完整 `DRIVER` 时，依次 `add_subdirectory(ascend_hal)`、`add_subdirectory(sdk_driver)`、并在开启产品构建（`ENABLE_BUILD_PRODUCT`）时 `add_subdirectory(custom)`——这正好对应 HAL、SDK-driver、custom(DCMI) 三棵源码树，是三层架构在构建系统里的落地。

#### 4.3.4 代码实践

**实践目标**：用源码证据区分「用户态 HAL」与「内核态 SDK-driver」。

**操作步骤**：

1. 打开 [src/sdk_driver/kernel_adapt/ka_module_init.c:L16-L43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c#L16-L43)，确认 `#include <linux/module.h>`、`module_init`、`MODULE_LICENSE("GPL")` 这些**只有内核代码才会出现**的标志。
2. 对照 `src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common_interface.c` 头部，你会发现那里 `#include` 的是 `<stdio.h>`、`<fcntl.h>`、`<sys/socket.h>` 等**用户态**标准头——两者形成鲜明对比。
3. 浏览 [README.md:L91-L115](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L91-L115) 的 `sdk_driver` 目录树，挑出 3 个你觉得「必须在内核态做」的子模块（提示：中断、预留内存、虚拟化）。

**需要观察的现象 / 预期结果**：你会清楚看到 `sdk_driver` 用内核头、`ascend_hal` 用用户态头，从而直观区分两层。预期结果待本地确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么申请中断、管理预留物理内存这类工作必须放在 SDK-driver（内核态），而不是放在 HAL（用户态）？

> **参考答案**：因为这些操作需要直接操控硬件与内核资源：中断注册要和内核中断子系统打交道，预留内存是内核在启动阶段保留的物理区，DMA 需要建立物理地址映射。用户态没有这些权限，必须通过系统调用陷入内核，由内核态驱动代为完成。

**练习 2**：从 [src/CMakeLists.txt:L15-L21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/CMakeLists.txt#L15-L21) 看，`custom` 子目录在什么条件下才会被构建？这暗示了 DCMI 层的什么性质？

> **参考答案**：只有 `ENABLE_BUILD_PRODUCT` 为真（构建完整产品）时才编译 `custom`。这暗示 DCMI 等定制化能力是「产品形态相关」的可选件，而非内核/运行时基础能力的必需部分，因此用开关单独控制。

## 5. 综合实践

本讲的核心综合任务，是把三层架构在脑中「立」起来，并亲手追一条贯穿三层的调用。

### 任务：绘制三层架构图 + 追踪一次跨层调用

**步骤 1：读官方材料**。先读 [README.md:L10-L18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L10-L18)（三层定义 + 架构图引用），如有条件打开图片 `docs/zh/figures/Driver_architecture.png` 对照。

**步骤 2：画图**。用纸笔或任意画图工具，画一张「四列」结构图（从左到右）：

```text
[上层调用者]  →  [DCMI 层]      →  [HAL 层]           →  [SDK-driver 层]  →  [NPU 设备]
                 custom/dev_prod    ascend_hal            sdk_driver           Device
                 /user/dcmi         (libascend_hal.so)    (.ko, 内核态)
                                     内含 DSMI/HDC/SVM
```

在每一层下标注它对应的**仓库源码目录**（`ascend_hal` / `sdk_driver` / `custom`），并标明 HAL=用户态、SDK-driver=内核态。

**步骤 3：追一次调用**。以样例 [examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c:L22-L40](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c#L22-L40) 里的 `dcmi_get_device_pcie_info` 为起点，写一段话（5–8 句）描述它如何自上而下穿过三层，要点包括：

1. 管理工具在**用户态**调用 `dcmi_get_device_pcie_info`（DCMI 层，`custom` 树）。
2. DCMI 做参数校验和产品形态判断后，转调下层 `dsmi_*` 原语（进入 HAL 层，`ascend_hal` 树）。
3. DSMI 经 **HDC** 通信通路，通过 `ioctl` 陷入**内核态**（进入 SDK-driver 层，`sdk_driver` 树）。
4. 内核驱动操作 PCIe/寄存器，从 **NPU 设备**读到 PCIe 信息。
5. 结果原路返回：设备 → 内核 → HAL → DCMI → 管理工具。

**预期结果**：一张清晰的三层架构图 + 一段能讲清「DCMI → HAL(DSMI/HDC) → SDK-driver → Device」的说明文字。其中步骤 2、3 中的具体内部函数（如 `dcmi_get_npu_pcie_info`、HDC 收发细节）可在后续讲义（u3-l2 HDC、u2-l2 DSMI）深入，本讲能讲到「层与层之间靠谁衔接」即可。本实践为源码阅读型，无需真实硬件，结果以你写出的图和文字为准。

## 6. 本讲小结

- `driver` 仓是昇腾 **CANN** 软件栈最底层的驱动模块，负责「使能芯片」，做基础驱动、资源管理与调度。
- 开源仓主体由三层构成：**DCMI 层**（达芬奇卡管理接口）、**HAL 层**（硬件抽象层）、**SDK-driver 层**（驱动开发套件/内核驱动）。
- 三层对应三棵源码树：DCMI 实现在 **`custom`** 树、HAL 在 **`ascend_hal`** 树、SDK-driver 在 **`sdk_driver`** 树；构建入口 [src/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/CMakeLists.txt#L15-L21) 把它们组织在一起。
- HAL（`libascend_hal.so`）是**用户态**库，SDK-driver（`.ko`）是**内核态**模块；前者用 `<stdio.h>` 等用户态头，后者用 `<linux/module.h>` 等内核头。
- DCMI 面向**管理工具**（管卡、查状态、复位），HAL 面向**计算运行时**（acl/Runtime），二者最终都汇入 HAL 再进内核到设备。
- 经典跨层路径：`dcmi_*`（custom）→ `dsmi_*`（ascend_hal）→ HDC/ioctl → SDK-driver 内核 → NPU 设备。

## 7. 下一步学习建议

建立心智模型后，建议按以下顺序继续：

1. **u1-l2（环境准备与编译部署）**：亲手把仓库编译成 run 包并安装，让架构「跑起来」。
2. **u1-l3（目录结构与三大源码组织）**：深入三棵源码树的子模块划分，建立「功能→目录」的快速定位能力。
3. **u1-l5（公共头文件与 API 总览）**：系统浏览 `pkg_inc/` 和 `custom/include/`，把本讲提到的 `dcmi_*` / `dsmi_*` / `hal_*` 三套接口梳理成对照表。
4. 之后进入单元 2（DCMI 接口层）和单元 3（HAL 与 HDC 通信），把本讲画的每一条「层间衔接线」逐个用源码坐实。
