# 对外公共头文件与 API 总览

## 1. 本讲目标

本讲是入门导览单元（单元 1）的最后一讲。前面几讲我们已经建立了 driver 的三层架构认知（DCMI / HAL / SDK-driver，见 u1-l1）、看懂了仓库目录结构（见 u1-l3），也跑通了一个 DCMI 查询样例（见 u1-l4）。本讲要回答一个更基础、却贯穿后续所有学习的问题：

**「上层应用到底通过哪些接口跟 NPU 打交道？这些接口声明在哪里？」**

学完本讲，你应当能够：

1. 理解 `pkg_inc/` 目录作为「对外头文件门面」的整体组织方式。
2. 区分 `ascend_hal`、`dsmi_common_interface`、`dcmi_interface_api` 三套公共接口各自的定位、命名风格与服务对象。
3. 学会在头文件中查找某个接口的函数签名、参数含义与返回的错误码定义。
4. 看懂三套接口各自的错误码体系（`drvError_t` 枚举 vs `DCMI_ERROR_CODE_BASE` 偏移）。

本讲只讲「声明」（头文件），不讲「实现」。实现分散在三棵源码树里，会在进阶单元（单元 2、3）逐一展开。

---

## 2. 前置知识

阅读本讲前，请确保理解以下概念（前几讲已建立）：

- **三层架构**：DCMI 层（定制管理接口）、HAL 层（硬件抽象层，用户态动态库）、SDK-driver 层（内核态 `.ko`）。三棵源码树 `custom` / `ascend_hal` / `sdk_driver` 分别承载它们。
- **声明与实现分离**：头文件（`.h`）只声明「接口长什么样」，具体逻辑写在 `.c` 里。driver 的对外头文件集中在 `pkg_inc/`，而 DCMI 这类定制接口的头文件放在 `src/custom/include/`。
- **Host / Device**：Host 指主机（CPU 侧），Device 指 NPU 板卡。本讲的所有接口都运行在 Host 侧用户态，用来「从主机去操作/查询设备」。
- **C 接口的基本要素**：函数返回值（通常是错误码）、输入参数 `[in]`、输出参数 `[out]`。driver 头文件大量使用 Doxygen 风格注释（`@brief`、`@param`、`@return`）来标注这些。

一个关键直觉：**driver 对外暴露了不止一套接口，而是三套**。它们面向不同的调用者（管理工具 vs 计算运行时），命名风格、错误码、参数模型都不一样。本讲的核心就是把这三套接口「分门别类」看清楚。

---

## 3. 本讲源码地图

本讲涉及的关键文件全部是头文件：

| 文件 | 所在目录 | 作用 |
|------|---------|------|
| `ascend_hal.h` | `pkg_inc/` | HAL 层的聚合头文件，只有十几行，把 HAL 的几个子头文件串起来 |
| `ascend_hal_base.h` | `pkg_inc/` | HAL 层的主力接口头文件（6000+ 行），声明 `hal*` 系列 API |
| `ascend_hal_external.h` | `pkg_inc/` | HAL 依赖的公共类型定义（缓冲池、进程态等） |
| `ascend_hal_error.h` | `pkg_inc/` | 定义 `drvError_t` 错误码枚举，HAL 与 DSMI 共用 |
| `dsmi_common_interface.h` | `pkg_inc/` | DSMI 设备系统管理接口，声明 `dsmi_*` 系列 API |
| `dms_device_node_type.h` | `pkg_inc/` | 设备节点类型枚举，DSMI 依赖 |
| `dcmi_interface_api.h` | `src/custom/include/` | DCMI 设备管理接口，声明 `dcmi_*` 系列 API |
| `hal_error_code/drv_error_code.h` | `pkg_inc/hal_error_code/` | 错误码的 JSON 描述表，供工具解析 |

记忆要点：**`pkg_inc/` 是统一交付门面**，里面同时放着 HAL 和 DSMI 两套接口头；**DCMI 属于定制层**，所以它的头文件单独放在 `src/custom/include/`。

---

## 4. 核心概念与源码讲解

### 4.1 pkg_inc：对外头文件门面体系

#### 4.1.1 概念说明

一个大型 C 项目通常会把「对外公开的头文件」和「内部实现用的头文件」分开存放。driver 把所有对外头文件集中在一个目录里——`pkg_inc/`（package include）。它的名字本身就说明用途：**打包发布给上层（如 Runtime、管理工具、样例程序）使用的头文件集合**。

为什么需要这样一个门面目录？

- **版本稳定**：上层软件编译时只依赖 `pkg_inc/`，driver 内部重构 `.c` 文件位置时，只要 `pkg_inc/` 里的声明不变，上层就不用改。
- **职责清晰**：`src/` 下成千上万个内部头文件，调用者根本无从下手；`pkg_inc/` 只挑出「真正对外」的那部分。
- **多套接口共存**：HAL、DSMI 两套接口的头文件并排放在一起，调用者按需 `#include` 即可。

#### 4.1.2 核心流程

`pkg_inc/` 内部可以分成四类文件：

1. **HAL 接口头文件族**：`ascend_hal.h`（聚合入口）→ `ascend_hal_base.h` / `ascend_hal_external.h` / `ascend_hal_dc.h` / `ascend_hal_define.h` / `ascend_hal_type.h`。
2. **HAL 错误码**：`ascend_hal_error.h`（`drvError_t` 枚举）+ `hal_error_code/drv_error_code.h`（JSON 描述）。
3. **DSMI 接口**：`dsmi_common_interface.h` + `dsmi_common_interface_base.h` + `dms_device_node_type.h`。
4. **内部包接口**：`ascend_hal_pkg.h`、`ascend_inpackage_hal.h`（仓内其他模块互相调用，不对外部用户）。

一个上层程序使用 driver 接口的最小动作就是：`#include` 某个 `pkg_inc/` 头文件 → 链接对应的动态库（`libascend_hal.so` 或 `libdcmi.so`）。

#### 4.1.3 源码精读

先看 `pkg_inc/` 目录下都有哪些文件（这是真实目录列表）：

```
pkg_inc/
├── ascend_hal.h                 # HAL 聚合入口（极简）
├── ascend_hal_base.h            # HAL 主力接口（hal* 系列）
├── ascend_hal_dc.h
├── ascend_hal_define.h
├── ascend_hal_error.h           # drvError_t 错误码枚举
├── ascend_hal_external.h        # 公共类型（缓冲池等）
├── ascend_hal_pkg.h
├── ascend_hal_type.h
├── ascend_inpackage_hal.h
├── dms_device_node_type.h       # 设备节点类型枚举
├── dsmi_common_interface.h      # DSMI 接口（dsmi_* 系列）
├── dsmi_common_interface_base.h
└── hal_error_code/
    └── drv_error_code.h         # 错误码 JSON 描述
```

注意 `hal_error_code/` 是个子目录，说明错误码体系本身就比较复杂，单独成包。

这里有个值得记住的设计：DSMI 头文件直接 `#include` 了 HAL 的错误码头文件，说明两者共用同一套底层错误码。这一点在 [dsmi_common_interface.h:22-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L22-L23) 里能看到：

```c
#include "ascend_hal_error.h"
#include "dms_device_node_type.h"
```

这句「DSMI 包含 HAL 错误码」是后面理解「为什么 dsmi 接口的错误码是 `DRV_ERROR_*`」的关键线索。

#### 4.1.4 代码实践

这是一个源码阅读型实践。

1. **实践目标**：建立 `pkg_inc/` 目录的「文件 → 职责」心智地图。
2. **操作步骤**：
   - 用 `ls pkg_inc/` 列出全部文件。
   - 用 `wc -l pkg_inc/*.h` 查看每个头文件的行数。
3. **需要观察的现象**：哪个文件最短（聚合入口）？哪个文件最长（主力接口）？哪个文件放在子目录里（错误码）？
4. **预期结果**：`ascend_hal.h` 只有十几行（聚合入口）；`ascend_hal_base.h` 有 6000+ 行（HAL 主力接口）；错误码相关文件在 `hal_error_code/` 子目录下。
5. **待本地验证**：不同版本行数会变，但「聚合入口极短、主力接口极长」这个规律应当稳定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 driver 要把对外头文件集中放在 `pkg_inc/`，而不是和内部头文件混在 `src/` 下？

**参考答案**：为了接口稳定性与发布清晰。`pkg_inc/` 是「契约」，对外承诺不变；`src/` 是「实现」，可以自由重构。把两者物理隔离，让上层软件只依赖稳定契约，driver 内部演进时不会波及调用者。

**练习 2**：DSMI 头文件 `dsmi_common_interface.h` 里 `#include "ascend_hal_error.h"`，这说明两套接口在错误码上是什么关系？

**参考答案**：DSMI 复用了 HAL 定义的 `drvError_t` 错误码枚举，而不是自己另搞一套。即 HAL 和 DSMI 共用同一套 `DRV_ERROR_*` 底层错误码。

---

### 4.2 ascend_hal.h：HAL 聚合头与 hal\* 接口体系

#### 4.2.1 概念说明

`ascend_hal.h` 是 HAL 层对外的「总入口」。但它本身几乎不写代码，只做一件事：**把 HAL 的几个子头文件 `#include` 串起来**。这是一种常见的「聚合头（umbrella header）」模式——调用者只要 `#include "ascend_hal.h"` 一个文件，就能拿到 HAL 全部公开接口。

HAL 层是三层里体量最大的一层（见 u1-l1），编译为用户态动态库 `libascend_hal.so`，面向**计算运行时**（如 ACL Runtime）。它把硬件能力抽象成一组以 `hal` 开头的接口：内存分配、内存拷贝、流/事件管理、设备信息查询……

#### 4.2.2 核心流程

聚合头的包含关系：

```
ascend_hal.h
  ├── ascend_hal_define.h       # 基础宏定义
  ├── ascend_hal_base.h         # 主力接口（hal* 系列，6000+ 行）
  ├── ascend_hal_external.h     # 公共类型（缓冲池、进程态）
  └── ascend_hal_dc.h           # DC（设备计算）相关接口
```

而 `ascend_hal_base.h` 自己又会进一步 `#include` 它依赖的类型头：

```
ascend_hal_base.h
  ├── ascend_hal_define.h
  └── ascend_hal_external.h
```

`hal*` 接口的统一签名特征是：

```c
DLLEXPORT drvError_t halXxx(...);   // 返回 drvError_t 错误码
```

即：返回类型是 `drvError_t`（定义在 `ascend_hal_error.h`），函数名采用 **camelCase**（首字母 `hal` 后跟驼峰，如 `halMemAlloc`、`halGetDeviceInfo`）。

#### 4.2.3 源码精读

先看聚合头本体，[ascend_hal.h:11-18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal.h#L11-L18) 这段就是它的全部实质内容：

```c
#ifndef __ASCEND_HAL_H__
#define __ASCEND_HAL_H__

#include "ascend_hal_define.h"
#include "ascend_hal_base.h"
#include "ascend_hal_external.h"
#include "ascend_hal_dc.h"

#endif
```

四个 `#include`，仅此而已。这正是「聚合头」的典型形态。

再看 `ascend_hal_base.h` 顶部，[ascend_hal_base.h:30-31](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L30-L31) 引入依赖：

```c
#include "ascend_hal_define.h"
#include "ascend_hal_external.h"
```

随后定义了一批 HDC（主机-设备通信，见 u3-l2）相关类型，[ascend_hal_base.h:37-41](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L37-L41)：

```c
typedef drvError_t hdcError_t;
typedef void *HDC_CLIENT;
typedef void *HDC_SESSION;
typedef void *HDC_SERVER;
typedef void *HDC_EPOLL;
```

然后是大量 `hal*` 接口声明。挑三个代表性接口：

**设备信息查询**——[ascend_hal_base.h:1210](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1210)：

```c
DLLEXPORT drvError_t halGetDeviceInfo(uint32_t devId, int32_t moduleType, int32_t infoType, int64_t *value);
```

这是一个「万能查询」接口：用 `moduleType`（模块类型，如 AI Core、内存）+ `infoType`（信息子类型）组合，查出对应的设备信息。模块类型的取值见 [ascend_hal_base.h:343-357](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L343-L357) 的 `enum`（`MODULE_TYPE_SYSTEM`、`MODULE_TYPE_AICORE`、`MODULE_TYPE_MEMORY` 等）。

**内存分配**——[ascend_hal_base.h:2660](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2660)：

```c
DLLEXPORT drvError_t halMemAlloc(void **pp, unsigned long long size, unsigned long long flag);
```

**内存拷贝**——[ascend_hal_base.h:2142](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2142)：

```c
DLLEXPORT drvError_t halMemcpy(void *dst, size_t dst_size, void *src, size_t count, struct memcpy_info *info);
```

注意三个接口都返回 `drvError_t`，都带 `DLLEXPORT` 前缀，函数名都是 `hal` + 驼峰。这就是 `hal*` 家族的统一外貌。

`DLLEXPORT` 宏的作用是把这些符号标记为「动态库导出」，保证上层链接 `.so` 时能找到。它的定义可参考 DSMI 头里的同名宏（Linux 下为 `__attribute__((visibility("default")))`）。

#### 4.2.4 代码实践

1. **实践目标**：学会在 `ascend_hal_base.h` 里用接口名反查签名与注释。
2. **操作步骤**：
   - 打开 `pkg_inc/ascend_hal_base.h`。
   - 搜索 `halMemAlloc`，阅读它上方 `@brief`/`@param`/`@return` 的 Doxygen 注释。
   - 再搜索 `halGetDeviceInfo`，看它上方那张大表格（说明哪些 `moduleType`+`infoType` 组合合法）。
3. **需要观察的现象**：每个 `hal*` 接口上方是否都有一段规范的 Doxygen 注释？注释里是否标注了 `[in]`/`[out]` 参数方向？
4. **预期结果**：是。driver 头文件的注释相当规整，`@param [in]` 表示输入、`@param [out]` 表示输出，`@return` 说明返回值含义。这正是「通过头文件查接口」能成立的基础。
5. **待本地验证**：无（纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：`ascend_hal.h` 只有十几行，为什么还要单独存在一个文件？

**参考答案**：它是聚合头（umbrella header），给调用者提供一个「一劳永逸」的入口——`#include "ascend_hal.h"` 就等于同时引入了 base/external/dc 等全部 HAL 子头。即便内部子头拆分调整，只要聚合头的 `#include` 列表稳定，调用者代码就不用改。

**练习 2**：`halMemAlloc` 与 `halMemcpy` 的返回类型是什么？它定义在哪个头文件？

**参考答案**：返回类型是 `drvError_t`，定义在 `pkg_inc/ascend_hal_error.h`。所有 `hal*` 接口统一用这个错误码类型。

---

### 4.3 dsmi_common_interface.h：DSMI 设备系统管理接口

#### 4.3.1 概念说明

DSMI 全称 **Device System Management Interface**（设备系统管理接口）。如果说 HAL 的 `hal*` 面向「计算运行时」，那么 DSMI 的 `dsmi_*` 面向的是**设备系统管理**：查设备数量、查版本、查健康状态、查温度、查电源、升级固件……

DSMI 在三层架构中的位置很有意思：它的**声明**在 `pkg_inc/`（和 HAL 并列），但**实现**在 `src/ascend_hal/dmc/dsmi/`（属于 HAL 层内部）。换句话说，DSMI 是 HAL 动态库里对外暴露的另一组接口，只是服务对象不同（管理工具而非计算运行时）。

#### 4.3.2 核心流程

`dsmi_*` 接口的统一签名特征：

```c
DLLEXPORT int dsmi_xxx_yyy(...);   // 返回 int，函数名全小写下划线分隔
```

与 `hal*` 的三点关键区别：

| 维度 | HAL（`hal*`） | DSMI（`dsmi_*`） |
|------|---------------|-------------------|
| 命名风格 | camelCase（`halMemAlloc`） | snake_case（`dsmi_get_device_count`） |
| 返回类型 | `drvError_t` | `int` |
| 设备定位 | `devId`（逻辑/物理设备号） | `device_id`（设备号） |
| 服务对象 | 计算运行时（ACL/Runtime） | 管理工具（如 npu-smi） |

注意：DSMI 虽然返回 `int`，但其错误码语义仍来自 HAL 的 `drvError_t` 枚举——这一点会在 4.3.3 的源码里看到。

#### 4.3.3 源码精读

`dsmi_common_interface.h` 开头先定义导出宏和常量，[dsmi_common_interface.h:16-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L16-L23)：

```c
#ifdef __linux
#define DLLEXPORT __attribute__((visibility("default")))
#else
#define DLLEXPORT _declspec(dllexport)
#endif

#include "ascend_hal_error.h"
#include "dms_device_node_type.h"
```

这就是 `DLLEXPORT` 的真身——Linux 下用 GCC 的 `visibility("default")` 把符号导出动态库。

接着是一段**错误码别名映射**，这是 DSMI 与 HAL 错误码关系的铁证，[dsmi_common_interface.h:39-54](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L39-L54)：

```c
// 1980 dsmi return value
#define DM_DDMP_ERROR_CODE_EAGAIN DRV_ERROR_TRY_AGAIN               /**< same as EAGAIN */
#define DM_DDMP_ERROR_CODE_PERM_DENIED DRV_ERROR_OPER_NOT_PERMITTED /**< same as EPERM */
// all of follow must same as inc/base.h
#define DM_DDMP_ERROR_CODE_SUCCESS DRV_ERROR_NONE                          /**< success */
#define DM_DDMP_ERROR_CODE_PARAMETER_ERROR DRV_ERROR_PARA_ERROR            /**< param error */
#define DM_DDMP_ERROR_CODE_INVALID_HANDLE_ERROR DRV_ERROR_INVALID_HANDLE   /**< invalid fd handle */
...
```

可以看到，DSMI 自己历史遗留的 `DM_DDMP_ERROR_CODE_*` 命名，全部 `#define` 成了 HAL 的 `DRV_ERROR_*`。注释 `// all of follow must same as inc/base.h` 更是明确要求两者必须保持一致。因此判断 dsmi 接口成功与否，标准就是 `返回值 == 0`（即 `DRV_ERROR_NONE`）。

来看三个设备查询类接口（本讲综合实践的素材）。先看查设备数量，[dsmi_common_interface.h:2587-2595](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L2587-L2595)：

```c
/**
 * @ingroup driver
 * @brief Get the number of devices
 * @attention NULL
 * @param [out] device_count  The space requested by the user is used to store the number of returned devices
 * @return  0 for success, others for fail
 * @note Support:Ascend310,Ascend310B,Ascend910,Ascend310P,Ascend910B,Ascend910_93,Ascend950,Ascend910_55
 */
DLLEXPORT int dsmi_get_device_count(int *device_count);
```

注意每个接口注释末尾都有 `@note Support:...`，列出该接口支持哪些芯片型号。这是 driver 适配多芯片的重要信息（见 u1-l2 讲过的三种芯片）。

查版本，[dsmi_common_interface.h:2547-2560](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L2547-L2560)：

```c
DLLEXPORT int dsmi_get_version(int device_id, char *version_str, unsigned int version_len, unsigned int *ret_len);
```

查健康状态，[dsmi_common_interface.h:2660-2671](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h#L2660-L2671)：

```c
DLLEXPORT int dsmi_get_device_health(int device_id, unsigned int *phealth);
```

这三个接口的共同模式：`device_id` 作为输入定位设备，输出参数带回结果，返回 `int` 表示成败。这正是 `dsmi_*` 设备查询接口的通用套路。

#### 4.3.4 代码实践

1. **实践目标**：掌握「按命名规律在头文件里找接口」的技巧。
2. **操作步骤**：
   - 在 `pkg_inc/dsmi_common_interface.h` 中搜索 `dsmi_get_device_temperature`、`dsmi_get_device_power_info`、`dsmi_get_device_voltage`。
   - 阅读它们的签名和 `@brief`。
3. **需要观察的现象**：这些「查某项指标」的接口签名是不是高度雷同（都是 `device_id` + 一个 `out` 指针）？
4. **预期结果**：是。`dsmi_*` 的查询接口几乎是同一个模板，只是输出参数的类型不同（温度是 `int *`，电源是结构体指针）。
5. **待本地验证**：无。

#### 4.3.5 小练习与答案

**练习 1**：`dsmi_get_device_count` 返回成功时的值是多少？这个值对应 `drvError_t` 枚举里的哪个常量？

**参考答案**：返回 `0` 表示成功，对应 `DRV_ERROR_NONE`（`ascend_hal_error.h` 里 `DRV_ERROR_NONE = 0`）。DSMI 的 `DM_DDMP_ERROR_CODE_SUCCESS` 就是被 `#define` 成 `DRV_ERROR_NONE` 的。

**练习 2**：`dsmi_*` 接口注释里的 `@note Support:...` 有什么实际用处？

**参考答案**：它列出该接口支持哪些芯片型号（Ascend310/910B/910_93/950 等）。调用方在适配多种芯片时，可以据此判断某个接口在当前硬件上是否可用，避免调用到不支持的接口。

---

### 4.4 dcmi_interface_api.h：DCMI 设备管理接口

#### 4.4.1 概念说明

DCMI 全称 **Da Vinci Card Management Interface**（达芬奇卡管理接口）。它是三层架构里最上层、面向**管理工具**的接口（见 u1-l1、u1-l4）。和 DSMI 的最大区别在于：DCMI 引入了**「卡（card）」的概念**——它按「卡号 + 设备号」两级定位，而 DSMI/HAL 只用一个 `device_id`。

为什么有这个区别？因为管理工具（如 npu-smi、u1-l4 里的样例）需要面向物理板卡运维：一台机器插多张卡，每张卡上可能有多个芯片，运维人员天然以「卡」为单位思考。

DCMI 属于定制层，所以它的头文件不在 `pkg_inc/`，而在 `src/custom/include/dcmi_interface_api.h`（见 u1-l3）。

#### 4.4.2 核心流程

`dcmi_*` 接口的统一签名特征：

```c
DCMIDLLEXPORT int dcmi_xxx(int card_id, int device_id, ...);   // 卡+设备两级定位
```

关键点：

- 导出宏叫 `DCMIDLLEXPORT`（注意前缀 DCMI），且在 Linux 下它是**空宏**（见下方源码），与 HAL/DSMI 的 `DLLEXPORT` 不同。
- 返回 `int`，但错误码体系**独立**于 HAL/DSMI：DCMI 有自己的 `DCMI_ERROR_CODE_BASE`。
- 多数接口前两个参数固定是 `(int card_id, int device_id, ...)`。

DCMI 错误码的计算方式：

\[
\text{errcode} = \text{DCMI\_ERROR\_CODE\_BASE} - n = -8000 - n
\`

即所有 DCMI 错误码都是 `-8000` 再减去一个偏移，这样和 HAL 的 `DRV_ERROR_*`（小正整数）完全错开，不会冲突。

#### 4.4.3 源码精读

DCMI 头文件开头先定义导出宏和关键常量，[dcmi_interface_api.h:14-31](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L14-L31)：

```c
#include <stdbool.h>
#include "dsmi_common_interface_custom.h"
...
#ifdef __linux
#define DCMIDLLEXPORT
#else
#define DCMIDLLEXPORT _declspec(dllexport)
#endif

#define MAX_VER_LEN 255  // Maximum length of version string
#define MAX_CARD_NUM 64  // The system supports up to 64 cards
```

两个细节值得注意：第一，`DCMIDLLEXPORT` 在 Linux 下是空的（DCMI 接口可能通过别的方式导出，或由定制层构建处理）；第二，`MAX_CARD_NUM 64` 正好印证了 u1-l4 讲过的「卡列表上限 64」。

DCMI 的错误码体系独立于 HAL，定义在 [dcmi_interface_api.h:1972-1994](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L1972-L1994)：

```c
#define DCMI_OK 0
#define DCMI_ERROR_CODE_BASE (-8000)
#define DCMI_ERR_CODE_INVALID_PARAMETER             (DCMI_ERROR_CODE_BASE - 1)
#define DCMI_ERR_CODE_OPER_NOT_PERMITTED            (DCMI_ERROR_CODE_BASE - 2)
#define DCMI_ERR_CODE_MEM_OPERATE_FAIL              (DCMI_ERROR_CODE_BASE - 3)
...
#define DCMI_ERR_CODE_NOT_SUPPORT                   (DCMI_ERROR_CODE_BASE - 255)
```

注意 `DCMI_OK 0`（成功）和 `DCMI_ERROR_CODE_BASE (-8000)`（错误码基准）。DCMI 用「负数偏移」、HAL/DSMI 用「`drvError_t` 小正数」，两套体系泾渭分明。

来看 DCMI 的核心接口。初始化，[dcmi_interface_api.h:2001](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2001)：

```c
DCMIDLLEXPORT int dcmi_init(void);
```

这就是 u1-l4 样例里一切查询的起点。查 PCIe 信息，[dcmi_interface_api.h:2019](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2019)：

```c
DCMIDLLEXPORT int dcmi_get_device_pcie_info(int card_id, int device_id, struct dcmi_pcie_info *pcie_info);
```

它的输出结构体 `dcmi_pcie_info` 定义在 [dcmi_interface_api.h:89-97](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L89-L97)：

```c
struct dcmi_pcie_info {
    unsigned int deviceid;
    unsigned int venderid;
    unsigned int subvenderid;
    unsigned int subdeviceid;
    unsigned int bdf_deviceid;
    unsigned int bdf_busid;
    unsigned int bdf_funcid;
};
```

这正是 u1-l4 样例里打印的厂商/设备 ID 与 BDF 三元组的数据来源。

枚举卡的接口在文件靠后位置，[dcmi_interface_api.h:3613](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L3613)：

```c
DCMIDLLEXPORT int dcmi_get_card_num_list(int *card_num, int *card_list, int list_len);
```

注意 DCMI 还有一套 `dcmiv2_` 前缀的接口（如 `dcmiv2_get_device_pcie_info`，[dcmi_interface_api.h:2451](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2451)），它是 DCMI 的「v2 版本」，参数模型简化为单 `dev_id`（不再用 card+device 两级），用于新一代调用方式。这体现了 driver 接口会随芯片演进而「版本化」迭代。

#### 4.4.4 代码实践

1. **实践目标**：理解 DCMI 的「card + device」两级定位模型，并对比 `dcmi_` 与 `dcmiv2_` 两版接口。
2. **操作步骤**：
   - 在 `src/custom/include/dcmi_interface_api.h` 中对比 `dcmi_get_device_pcie_info`（2019 行）与 `dcmiv2_get_device_pcie_info`（2451 行）的签名差异。
   - 思考：v2 版本去掉了哪个参数？为什么？
3. **需要观察的现象**：v1 用 `card_id + device_id` 两个参数定位，v2 只用一个 `dev_id`。
4. **预期结果**：`dcmiv2_*` 系列把「卡+设备」两级编号合并成单一的 `dev_id`，调用更简洁，适合不需要区分物理卡结构的场景。
5. **待本地验证**：无。

#### 4.4.5 小练习与答案

**练习 1**：DCMI 的错误码 `-8001` 代表什么含义？它是怎么算出来的？

**参考答案**：`-8001 = DCMI_ERROR_CODE_BASE - 1 = -8000 - 1`，对应 `DCMI_ERR_CODE_INVALID_PARAMETER`（参数非法）。DCMI 错误码统一是 `-8000` 减去一个偏移量。

**练习 2**：为什么 DCMI 头文件放在 `src/custom/include/` 而 DSMI 放在 `pkg_inc/`？

**参考答案**：DCMI 属于「定制层」（custom），是按产品形态可选的特性库，所以头文件随定制源码放在 `src/custom/include/`；DSMI 是 HAL 动态库对外暴露的通用接口，和 HAL 一起作为标准交付物放在 `pkg_inc/` 门面目录。

---

## 5. 综合实践

本讲的综合实践就是规格里要求的那张对照表，它把四个最小模块串起来。

**任务**：在 `dsmi_common_interface.h` 中找出 3 个设备查询类接口，在 `ascend_hal_base.h` 中找出 3 个以 `hal` 开头的接口，整理成一张「接口名 — 所属层 — 用途」对照表。

**操作步骤**：

1. 打开 `pkg_inc/dsmi_common_interface.h`，挑选 3 个 `dsmi_*` 查询接口（建议：`dsmi_get_device_count`、`dsmi_get_version`、`dsmi_get_device_health`），抄下签名与 `@brief`。
2. 打开 `pkg_inc/ascend_hal_base.h`，挑选 3 个 `hal*` 接口（建议：`halMemAlloc`、`halMemcpy`、`halGetDeviceInfo`），抄下签名与 `@brief`。
3. 为了对比，再从 `src/custom/include/dcmi_interface_api.h` 里挑 1 个 `dcmi_*` 接口（如 `dcmi_get_device_pcie_info`）。
4. 整理成下表（参考答案）：

| 接口名 | 所属层 | 命名风格 | 返回类型 | 定位方式 | 用途 |
|--------|--------|----------|----------|----------|------|
| `dsmi_get_device_count` | DSMI（HAL 库内） | snake_case | `int` | `device_id` | 查询系统设备总数 |
| `dsmi_get_version` | DSMI（HAL 库内） | snake_case | `int` | `device_id` | 查询某设备的系统版本号 |
| `dsmi_get_device_health` | DSMI（HAL 库内） | snake_case | `int` | `device_id` | 查询设备整体健康状态 |
| `halMemAlloc` | HAL | camelCase | `drvError_t` | 当前 device | 分配设备虚拟内存 |
| `halMemcpy` | HAL | camelCase | `drvError_t` | 指针 + 拷贝描述 | 主机与设备间同步内存拷贝 |
| `halGetDeviceInfo` | HAL | camelCase | `drvError_t` | `devId` + 模块/信息类型 | 万能设备信息查询 |
| `dcmi_get_device_pcie_info` | DCMI（custom） | snake_case | `int` | `card_id + device_id` | 查询卡上某设备的 PCIe 厂商/设备/BDF 信息 |

**需要观察的现象**：

- DSMI 与 HAL 都用单一设备号定位，但 DCMI 用「卡+设备」两级。
- HAL 用 camelCase + `drvError_t`；DSMI/DCMI 用 snake_case + `int`。
- DCMI 错误码是 `-8000` 偏移，DSMI 复用 HAL 的 `DRV_ERROR_*`，两者错误码体系不同。

**预期结果**：通过这张表，你能一眼看出某个接口「属于哪一层、给谁用、怎么定位设备、怎么判断成败」。这就是本讲要建立的核心能力。

---

## 6. 本讲小结

- `pkg_inc/` 是 driver 的**对外头文件门面**，集中存放 HAL、DSMI 的公开头文件；DCMI 属于定制层，头文件单独在 `src/custom/include/`。
- `ascend_hal.h` 是 HAL 的**聚合头**，只负责把 base/external/dc 等子头串起来；真正的 `hal*` 接口主力在 `ascend_hal_base.h`（6000+ 行）。
- driver 对外有**三套接口**：`hal*`（camelCase，返回 `drvError_t`，面向计算运行时）、`dsmi_*`（snake_case，返回 `int`，面向设备系统管理）、`dcmi_*`（snake_case，card+device 两级定位，面向板卡管理工具）。
- **错误码分两套**：HAL 与 DSMI 共用 `drvError_t` 枚举（`DRV_ERROR_NONE=0` 为成功，定义在 `ascend_hal_error.h`）；DCMI 用独立的 `DCMI_ERROR_CODE_BASE (-8000)` 偏移体系（`DCMI_OK=0` 为成功）。
- 头文件普遍使用规范的 **Doxygen 注释**（`@brief`/`@param [in]/[out]`/`@return`/`@note Support`），「通过头文件查接口签名与芯片支持情况」是后续学习的通用技能。
- DCMI 还有 `dcmiv2_*` 版本接口，把「卡+设备」两级合并为单一 `dev_id`，体现了接口的版本化演进。

---

## 7. 下一步学习建议

本讲讲清了三套接口的「声明」与组织方式。接下来：

- **想深入 DCMI 的实现**：进入单元 2，从「u2-l1 DCMI 接口总览与初始化流程」开始，看 `dcmi_init` 在 `src/custom/dev_prod/user/dcmi/` 里是怎么实现的。
- **想深入 DSMI 的实现**：看「u2-l2 DSMI 设备系统管理接口实现」，了解 `dsmi_*` 如何通过 dmp command 下发到设备。
- **想深入 HAL 层**：进入单元 3，从「u3-l1 HAL 层总览与 ascend_hal 公共接口」开始，理解 `hal*` 接口背后的 HDC 通信模型。
- **建议同步阅读**：直接打开 `pkg_inc/ascend_hal_error.h`，把 `drvError_t` 枚举从头到尾浏览一遍——它会是后续所有 HAL/DSMI 调试时最常查的文件。
