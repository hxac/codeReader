# HAL 层总览与 ascend_hal 公共接口

## 1. 本讲目标

本讲是单元 3（HAL 层与主机-设备通信）的第一篇。前面几个单元我们已经建立了 driver 的三层架构（DCMI / HAL / SDK-driver）心智模型，也梳理了对外公共头文件（`pkg_inc/`）。本讲要把目光聚焦到三层中**体量最大、最核心**的 HAL 层，目标是让读者：

1. 理解 HAL（Hardware Abstraction Layer，硬件抽象层）在 Host 侧的定位——它编译成一个**用户态动态库** `libascend_hal.so`，面向计算运行时（acl/Runtime）而不是管理工具。
2. 看懂 `ascend_hal.h` 这个「聚合头」是如何把 `base / external / dc / define / error` 等子头文件串起来的，以及各子头的分工。
3. 能够按「内存管理 / 流与任务 / 队列事件 / 通信」给 `hal*` 接口分类，并能说出 HAL 与 DCMI 面向的调用者有何本质不同。

学完本讲，你应该能在源码中快速判断「某个能力属于 HAL 还是 DCMI」「某个 `hal*` 接口属于哪一类」，并为后续讲义（HDC 通信、PBL 基础库、SVM 内存）打好接口层基础。

## 2. 前置知识

本讲默认你已经掌握前置讲义（u1-l1 ～ u2-l4）建立的概念，重点回顾三条：

- **三层架构**：DCMI 层（定制层，面向管理工具）→ HAL 层（用户态库，面向运行时）→ SDK-driver 层（内核态 `.ko`，操作硬件）。本讲的主角是中间的 HAL 层。
- **Host / Device 与用户态 / 内核态**：Host 指主机（CPU 侧），Device 指 NPU 芯片；用户态进程调用 `hal*` 接口后，很多操作会通过 `ioctl` 陷入内核态的 SDK-driver，再抵达 Device。
- **公共头门面 `pkg_inc/`**：driver 对外暴露的 HAL/DSMI 接口声明集中在 `pkg_inc/`；DCMI 的头文件则在 `src/custom/include/`。本讲只看 `pkg_inc/` 下的 HAL 头。

补充一个本讲会用到的术语：

- **导出符号（exported symbol）**：动态库（`.so`）里默认不是所有函数都能被外部程序调用，只有被「标记导出」的符号才出现在动态符号表里，供其他程序链接。HAL 用 `DLLEXPORT` 宏来做这件事。
- **聚合头（aggregate header）**：一个只做 `#include`、本身几乎不含代码的头文件，作用是让使用方「include 一个就够」。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `pkg_inc/` 下，全部是对外公共头（声明，不包含实现）：

| 文件 | 行数级别 | 作用 |
| --- | --- | --- |
| `pkg_inc/ascend_hal.h` | 极小（约 20 行） | **聚合头**，把 HAL 各子头串成一个入口，使用方只需 `#include "ascend_hal.h"`。 |
| `pkg_inc/ascend_hal_external.h` | 中 | 最底层子头：定义 `DLLEXPORT` 导出宏、基础通知、buff/mbuf 内存池、分组（grp）、事件调度（esched）、队列（queue）等接口。 |
| `pkg_inc/ascend_hal_define.h` | 很大 | 公共「字典」：错误码别名、事件 ID、队列/buff/内存/TRS/HDC 等所有结构体与枚举、特性查询枚举 `drvFeature_t`。 |
| `pkg_inc/ascend_hal_error.h` | 中 | 统一错误码枚举 `drvError_t`（`DRV_ERROR_NONE = 0` 表示成功）。 |
| `pkg_inc/ascend_hal_base.h` | 极大（6000+ 行） | HAL 的**主力头**：HDC 通信类型、设备开关与信息查询、内存管理（`halMem*`）、P2P、共享 id、故障/传感器事件、DMA 等。 |
| `pkg_inc/ascend_hal_dc.h` | 极小（约 4 行） | 当前为**空占位头**，仅保留 include guard，预留给未来「dc」相关声明。 |

> 说明：本讲只读头文件（接口契约），不涉及实现。`hal*` 接口的实现分散在 `src/ascend_hal/` 各子模块（SVM、HDC、TRS、DMC 等），后续讲义会逐个深入。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① HAL 层的定位与 `ascend_hal` 动态库；② `ascend_hal.h` 聚合头的分层组织；③ `hal` 系列接口的分类。

### 4.1 HAL 层的定位与 ascend_hal 动态库

#### 4.1.1 概念说明

HAL 是「Hardware Abstraction Layer」的缩写，中文叫**硬件抽象层**。它的职责是在 Host 侧把「NPU 芯片能做什么」抽象成一组稳定的 C 函数接口，让上层不必关心 PCIe 寄存器、页表、中断这些硬件细节。

理解 HAL 定位，关键在于和 DCMI 做对比（这正是本讲实践任务的核心）：

| 维度 | HAL 层（`hal*` / `drv*`） | DCMI 层（`dcmi_*` / `dsmi_*`） |
| --- | --- | --- |
| 面向的调用者 | **计算运行时**：acl Runtime、GeRuntime、集合通信（HCCP）等「要跑算子、要分配显存、要拷数据」的模块 | **管理工具**：`npu-smi`、`npucli`、运维脚本等「要看板卡状态、要复位、要升级」的工具 |
| 典型操作 | 分配/释放显存、H2D/D2H 拷贝、流与队列、事件等待、P2P | 查询温度/健康/利用率、枚举卡与设备、芯片复位、日志导出 |
| 编译产物 | 用户态动态库 `libascend_hal.so` | 用户态动态库 `libdcmi.so`（链接 `libascend_hal.so`） |
| 命名风格 | `camelCase`，如 `halMemAlloc`、`halGetDeviceInfo` | `snake_case`，如 `dcmi_get_card_num_list` |
| 返回值 | `drvError_t`（成功 = `DRV_ERROR_NONE = 0`） | `int`（成功 = 0，错误码用独立的 `-8000` 偏移体系） |
| 定位单位 | 多以「逻辑设备 id（devid）」为单位 | 多以「卡 card_id + 设备 device_id」两级定位 |

一句话总结：**DCMI 偏「管」，HAL 偏「用」**。一个上层 AI 应用从「打开设备 → 分配显存 → 拷贝权重 → 下发算子 → 等待完成 → 释放」整条链路，几乎全程都在调用 HAL 接口；而 `npu-smi info` 这种运维命令走的是 DCMI。

#### 4.1.2 核心流程

一个典型的「上层应用经 HAL 使用 NPU」的流程如下（伪代码）：

```text
应用 / Runtime 进程（用户态）
   │  #include "ascend_hal.h"
   │  链接 -lascend_hal
   ▼
halDeviceOpen(devid)            // 打开设备，建立进程与设备的会话
   │
halMemAlloc(&ptr, size, flag)   // 申请显存（HBM/DDR）
   │   └─> 内部经 ioctl 陷入内核态 SDK-driver，申请物理页、建页表
   ▼
halMemcpy(dst, ..., src, ...)   // 拷贝数据（H2D / D2H / D2D）
   ▼
halQueueInit / halEschedWaitEvent  // 用队列/事件驱动数据流水线
   ▼
halMemFree(ptr); halDeviceClose(devid)
```

这条链路里有两个要点：

1. HAL 是**用户态库**，函数调用本身在用户态完成；但凡要真正动硬件（申请物理内存、下发 DMA），都会通过 `ioctl` 陷入内核态的 SDK-driver（`.ko`），再抵达 Device。这就是「HAL（用户态）↔ SDK-driver（内核态）」的跨态关系。
2. 因为是动态库，所以「哪些函数能被外部调用」是被 `DLLEXPORT` 宏严格控制的。

#### 4.1.3 源码精读

**导出宏 `DLLEXPORT` 与弱符号 `ASCEND_HAL_WEAK`** 定义在最底层的 `ascend_hal_external.h`：

[ascend_hal_external.h:L49-L56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_external.h#L49-L56) —— Linux 下 `DLLEXPORT` 展开为 `__attribute__((visibility("default")))`，把函数标记为「对外可见」，这样它才会进入 `libascend_hal.so` 的动态符号表，外部程序才能链接到它。`ASCEND_HAL_WEAK` 是弱符号，允许某些接口在库里没有实现时链接也不报错（后面会看到很多 `halQueue*`、`halEsched*` 带这个标记）。

**统一返回类型 `drvError_t`** 定义在 `ascend_hal_error.h`：

[ascend_hal_error.h:L33-L51](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_error.h#L33-L51) —— 这是一个枚举 `tagDrvError`，约定 `DRV_ERROR_NONE = 0` 表示成功，其余正值表示各类错误（无设备、非法参数、内存不足、忙、超时……）。HAL 几乎所有接口都返回 `drvError_t`，判断成功就是 `ret == 0`（或 `== DRV_ERROR_NONE`）。这与前置讲义里 DCMI「成功即 0」的约定一致，只是 HAL 用的是这套统一枚举。

> 注意一个细节：`ascend_hal_external.h` 里**较老的** buff/mbuf 接口（如 `halBuffFree`、`halMbufAlloc`）返回的是裸 `int`，而**较新的** queue/esched/device/memory 接口返回 `drvError_t`。这是历史演进留下的差异，但「0 表示成功」的总规则不变。

**设备打开 `halDeviceOpen`** 是 HAL 的入口接口之一：

[ascend_hal_base.h:L772-L784](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L772-L784) —— `halDeviceOpen` 调用各驱动组件的「统一设备打开」入口，注释明确写了「不可重复调用」「不能与独立模块的 open/close 混用」，返回 `drvError_t`。这体现了 HAL 把底层多模块的初始化收敛成对上层的一个统一接口。

**统一设备信息查询 `halGetDeviceInfo`** 是 HAL 里最「万能」的查询接口：

[ascend_hal_base.h:L1210](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1210) —— 通过 `(devId, moduleType, infoType)` 三元组查询几乎一切信息：芯片核数、主频、利用率、物理 chip id、主机-设备连接类型等。它上方那一大段 Doxygen 表格（base.h 约 1112-1208 行）就是一张「moduleType × infoType → 含义」对照表。这是一个典型的「**表驱动 + 二维参数**」设计：用一个函数 + 两维枚举取代几十个专门函数。

#### 4.1.4 代码实践

**实践目标**：验证 HAL 是一个可被外部链接的用户态动态库，并理解「成功返回 0」的约定。

**操作步骤**：

1. 在已安装 driver 的环境里，找到 HAL 库与头文件（通常随 driver run 包部署到系统目录）。
2. 阅读下面这段**示例代码**（非项目原有代码），它调用一个无需打开设备即可使用的 HAL 接口 `halGetAPIVersion`：

```c
/* 示例代码：查询 HAL API 版本号 */
#include <stdio.h>
#include "ascend_hal.h"   /* 聚合头，一个 include 拉全所有 hal 接口 */

int main(void)
{
    int version = 0;
    drvError_t ret = halGetAPIVersion(&version);  /* 返回 drvError_t，0 为成功 */
    if (ret != 0) {
        printf("halGetAPIVersion failed, ret = %d\n", (int)ret);
        return (int)ret;
    }
    /* version 高 8 位是 major，中 8 位是 minor，低 8 位是 patch */
    printf("HAL API version: 0x%06x (major=%d, minor=%d, patch=%d)\n",
           version, (version >> 16) & 0xff, (version >> 8) & 0xff, version & 0xff);
    return 0;
}
```

3. 编译（示例命令，路径以实际部署为准）：`gcc demo.c -I<头文件目录> -lascend_hal -o demo`。
4. 运行 `./demo`。

**需要观察的现象**：链接成功说明 `halGetAPIVersion` 确实是 `libascend_hal.so` 的导出符号（被 `DLLEXPORT` 标记）；返回值应为 0，打印出版本号。

**预期结果**：`ret == 0`，且打印的 major/minor/patch 与头文件里 `__HAL_API_VER_MAJOR/MINOR/PATCH`（见 4.2.3）一致。若环境未安装 driver 或库路径不对，链接/运行会失败——**运行结果待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 HAL 用 `DLLEXPORT` 标记函数？如果不加会怎样？
**答案**：`DLLEXPORT`（`visibility("default")`）让函数进入动态库的导出符号表，外部程序才能在运行时动态链接到它。如果不加，编译器默认会隐藏符号（`visibility("hidden")`），外部程序链接时会报「undefined symbol」。

**练习 2**：`halGetDeviceInfo` 用 `(moduleType, infoType)` 两个枚举来表达几十种查询，相比为每种查询写一个独立函数，这种设计的好处与代价各是什么？
**答案**：好处是接口收敛、易于扩展（加新查询只需加枚举值，不改函数签名），也便于上层做通用封装；代价是参数语义不透明，必须查表才知道每个组合的含义，且编译期无法对参数组合做类型检查，容易传错。

---

### 4.2 ascend_hal.h 聚合头的分层组织

#### 4.2.1 概念说明

`pkg_inc/ascend_hal.h` 本身只有十几行，几乎全是 `#include`。它的作用是**门面（facade）**：使用方只要 `#include "ascend_hal.h"`，就能拿到 HAL 的全部公共接口，而不必逐个去 include 子头。

理解 HAL 头文件的分层，关键是看懂这张**包含依赖图**（谁 include 谁）：

```text
              ascend_hal.h  (聚合头 / 使用方入口)
                 │ include 了 4 个子头
       ┌─────────┼─────────┬──────────┐
   base.h   external.h   dc.h      define.h
     │          │                     │  include
     │          │                     ▼
     └─include──┴──────────────►  define.h ──► error.h
                                          └──► ascend_hal_pkg.h
```

各层的职责一句话：

- `ascend_hal_external.h`：**最底层叶子**。定义 `DLLEXPORT`、`ASCEND_HAL_WEAK`，声明最早期的 buff/mbuf/grp/esched/queue 接口。
- `ascend_hal_error.h`：统一错误码 `drvError_t`。
- `ascend_hal_define.h`：**公共字典**，几乎所有的 `struct`/`enum`（内存、队列、事件、TRS、HDC、特性查询）都集中在这里。
- `ascend_hal_base.h`：**主力头**，依赖 `define.h`（拿到各种类型定义）和 `external.h`（拿到导出宏），声明绝大多数 `hal*` / `drv*` 接口。
- `ascend_hal_dc.h`：**空占位头**，预留给未来。

#### 4.2.2 核心流程

当上层代码写 `#include "ascend_hal.h"` 时，预处理器的展开顺序大致是：

1. 展开 `ascend_hal.h` → 依次 include `define.h`、`base.h`、`external.h`、`dc.h`。
2. 每个 `#include` 都有 `#ifndef ... #define ... #endif` 这套 **include guard**（含糊保护），保证重复 include 不会造成重复定义。
3. `extern "C"` 块（见 external.h 顶部）确保 C++ 调用方也能正确链接这些 C 函数（名字不会被 C++ 改写）。
4. 最终，所有 `hal*` 接口声明、所有公共结构体/枚举都进入当前编译单元可见。

这套分层带来的好处是**编译解耦**：比如只关心错误码的工具可以只 include `ascend_hal_error.h`，不必拖进 6000 行的 `base.h`；而常规使用方 include 一个 `ascend_hal.h` 就够了。

#### 4.2.3 源码精读

**聚合头本体**——`ascend_hal.h` 全部有效内容就是四个 include：

[ascend_hal.h:L14-L18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal.h#L14-L18) —— 依次引入 `ascend_hal_define.h`（字典）、`ascend_hal_base.h`（主力接口）、`ascend_hal_external.h`（底层接口 + 导出宏）、`ascend_hal_dc.h`（占位）。注意它**不直接** include `ascend_hal_error.h`，而是经由 `define.h` 间接引入。

**API 版本号**——聚合头体系里维护了一套语义化版本，用于接口兼容性管理：

[ascend_hal_base.h:L235-L257](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L235-L257) —— 三个宏 `__HAL_API_VER_MAJOR/MINOR/PATCH`（当前 `0x07 / 0x24 / 0x18`）按规则递增：删接口/改名才升 major，加新接口升 minor，改枚举/结构体成员且保持兼容升 patch。三者拼成 `__HAL_API_VERSION`。配合 `halGetAPIVersion`（取库的版本）和 `halSetRuntimeApiVer`（上层声明自己编译时用的版本），可以做运行时兼容性校验。这是大型公共库常见的「接口版本握手」机制。

**新式错误码基址**——除了 `drvError_t` 枚举，base.h 还定义了一套「带模块号」的新式错误码：

[ascend_hal_base.h:L264-L272](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L264-L272) —— `HAL_ERROR_CODE_BASE = 0x90020000`，宏 `HAI_ERROR_CODE(MODULE, ERROR_CODE)` 把「模块号 << 12 + 错误号」叠加到基址上，形成形如 `0x9002XYYY` 的唯一错误码。这样不同模块的错误码不会撞车，便于定位。前置讲义里 DCMI 用的是另一套 `-8000` 偏移（`DCMI_ERROR_CODE_BASE`），两套体系相互独立。

**空占位头**——`ascend_hal_dc.h` 的全部内容：

[ascend_hal_dc.h:L11-L14](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_dc.h#L11-L14) —— 目前只有 include guard 和一个空行，没有任何声明。这是一个真实事实：**`dc` 子头当前是预留的空壳**，聚合头仍然 include 它，是为了将来扩展「dc」（某种 device-compute 相关）声明时不破坏使用方的 include 习惯。阅读源码时不要期待在这里找到 dc 接口，更不要为它编造内容。

#### 4.2.4 代码实践

**实践目标**：亲手验证 HAL 头文件的包含依赖关系。

**操作步骤**：

1. 用文本编辑器或 `Read` 工具打开 `pkg_inc/ascend_hal.h`，确认它只有 4 个 `#include`。
2. 打开 `pkg_inc/ascend_hal_base.h` 顶部，确认它 include 了 `ascend_hal_define.h` 与 `ascend_hal_external.h`。
3. 打开 `pkg_inc/ascend_hal_external.h` 顶部，确认它 include 了 `ascend_hal_define.h`，并定义了 `DLLEXPORT`。
4. 打开 `pkg_inc/ascend_hal_define.h` 顶部，确认它 include 了 `ascend_hal_error.h`。
5. 画一张与 4.2.1 一致的依赖图。

**需要观察的现象**：`define.h` 是几乎所有子头的公共依赖；`external.h` 是叶子（不再向下依赖 base.h）；`dc.h` 为空。

**预期结果**：你画出的依赖图应当与 4.2.1 完全吻合，且能解释「为什么 include `ascend_hal.h` 一个头就能拿到全部接口」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ascend_hal.h` 不直接 include `ascend_hal_error.h`，却能用到 `drvError_t`？
**答案**：因为 `ascend_hal.h` include 了 `ascend_hal_define.h`，而 `define.h` 又 include 了 `error.h`，传递依赖使得 `drvError_t` 在聚合头里可见。

**练习 2**：`__HAL_API_VERSION` 的 major/minor/patch 分别在什么场景下递增？
**答案**：删接口或改接口名（破坏性）升 major；新增接口（向后兼容）升 minor；修改枚举/结构体成员但保持兼容升 patch。

---

### 4.3 hal 系列接口的分类

#### 4.3.1 概念说明

`ascend_hal_base.h` + `ascend_hal_external.h` 里有上百个 `hal*` / `drv*` 接口，初看眼花缭乱。但只要按**功能领域**归类，就会发现它们集中在几个清晰的大类里。本讲按实践任务要求的四大类来整理：**内存管理 / 流与任务 / 队列事件 / 通信**，外加一个横切的**设备生命周期与信息**类。

先建立直觉：

- **内存管理**：NPU 上的显存（HBM/DDR）申请、释放、拷贝、共享。这是 HAL 最核心、接口最多的领域（对应后续整个 SVM 单元）。
- **流与任务**：把算子组织成「流（stream）」、用「提交队列 SQ / 完成队列 CQ」下发与回收任务。HAL 头里主要出现的是流的快照备份与共享资源 id；真正的 SQ/CQ 任务调度在 TRS 子模块（见 u6-l3）。
- **队列事件**：进程间/设备间的数据队列（queue）与事件调度（esched），用于数据流水线（如 TDT、DVPP）。
- **通信**：Host 与 Device 之间的消息通道 HDC、设备间点对点 P2P、资源地址映射。

#### 4.3.2 核心流程

四类接口在一个真实 AI 任务里的大致出场顺序：

```text
[设备生命周期]  halDeviceOpen → 建立会话
       │
[设备信息]      halGetDeviceInfo(核数/主频/连接类型...) → 探测能力
       │
[内存管理]      halMemAlloc(HBM) → halMemcpy(H2D 拷权重)
       │
[流与任务]      准备 stream / SQ（经 TRS）→ 下发算子
       │
[队列事件]      halQueueInit + halEschedWaitEvent → 驱动数据流水线
       │
[通信]          halDeviceEnableP2P（多卡协同）/ HDC 传消息
       │
[内存管理]      halMemFree
[设备生命周期]  halDeviceClose
```

需要强调：**类别之间并非互斥**。比如 `halMemcpy` 既是「内存管理」也涉及「通信」（数据要在 Host↔Device 间搬）。分类只是帮助记忆与定位，不是绝对边界。

#### 4.3.3 源码精读

下面按四类各给出代表性接口（每类 2～3 个），并附永久链接。

**(A) 内存管理（`halMem*` / `halBuff*`）**

最基础的申请/释放：

[ascend_hal_base.h:L2660-L2670](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2660-L2670) —— `halMemAlloc(&ptr, size, flag)` 申请一段虚拟地址并映射，`flag` 编码了 devid、虚拟/物理内存类型（SVM/DEV/HOST/DVPP）、页大小（normal/huge）、对齐等海量信息（flag 的位域定义在 `ascend_hal_define.h` 的 `MEM_*` 宏区段）；`halMemFree(ptr)` 逆序释放。

同步拷贝（H2H / H2D / D2H / D2D）：

[ascend_hal_base.h:L2142](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2142) —— `halMemcpy(dst, dst_size, src, count, &info)`，其中 `info.dir` 取自 `drvMemcpyKind_t` 枚举（`DRV_MEMCPY_HOST_TO_DEVICE` 等，见 base.h:293-298），是阻塞式拷贝。

「整块申请」之外的 VMM（虚拟/物理分离）高级接口：

[ascend_hal_base.h:L2784-L2859](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2784-L2859) —— `halMemAddressReserve/Free`（预留/归还虚拟地址段）、`halMemCreate/Release`（申请/释放物理内存句柄）、`halMemMap/Unmap`（动态建立/解除虚拟↔物理映射）。这套接口把「要地址」和「要物理内存」解耦，是 SVM v3 的核心能力（后续 u4-l3 专讲）。

此外 `ascend_hal_external.h` 里还有一套**面向数据流水的 buff/mbuf 内存池**接口（`halBuffCreatePool` / `halMbufAlloc` 等，external.h:157/175），属于另一套更早期的内存管理风格。

**(B) 流与任务（stream / 共享资源 id）**

HAL 头里直接出现的「流」接口不多，主要服务于进程快照/恢复：

`halStreamBackup` / `halStreamRestore`（base.h:5925/5935）—— 备份/恢复流及其虚拟地址资源，用于进程检查点（snapshot）场景。

跨进程共享流/队列资源的「共享 id」：

[ascend_hal_base.h:L1854-L1873](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1854-L1873) —— `halShrIdCreate/Open/Destroy/Close` 创建、打开、销毁、关闭一个「共享资源 id」（`drvShrIdInfo`），用于在进程间共享 stream / notify / event 等资源 id。

> 重要说明：HAL 真正的「任务下发与回收」——SQ（Submission Queue，提交队列）/ CQ（Completion Queue，完成队列）的分配与通信——实现在 **TRS（Task Resource Schedule）子模块**，接口集中在 `src/ascend_hal/trs/`，本讲只点到为止，详见 u6-l3。`ascend_hal_define.h` 里那一大段 `TSDRV_FLAG_*`、`halAsyncDma*` 结构体就是为 TRS 的异步 DMA 任务准备的。

**(C) 队列事件（`halQueue*` / `halEsched*`）**

队列（用于生产者-消费者数据流水线）声明在 `external.h`：

[ascend_hal_external.h:L747-L792](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_external.h#L747-L792) —— `halQueueInit`（初始化队列子系统）、`halQueueDestroy`、`halQueueGetStatus`（按 `QUEUE_QUERY_ITEM` 查深度/状态/丢包统计）、`halQueueQueryInfo`。注意它们多带 `ASCEND_HAL_WEAK`，意味着库里可选实现。

事件调度（esched，进程内/跨进程的事件分发）：

[ascend_hal_external.h:L439-L487](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_external.h#L439-L487) —— `halEschedAttachDevice`（把进程附着到设备）、`halEschedCreateGrp`（创建事件分组，每进程最多 32 个）、`halEschedSubscribeEvent`（线程订阅事件位图）、`halEschedWaitEvent`（阻塞等待事件被调度）。

**(D) 通信（HDC / P2P / 资源映射）**

HDC（Host-Device Communication）是 HAL 各模块共用的主机-设备消息底座，其类型在 base.h 开头定义：

[ascend_hal_base.h:L38-L41](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L38-L41) —— HDC 的四个核心句柄类型 `HDC_CLIENT / HDC_SESSION / HDC_SERVER / HDC_EPOLL`（都是 `void *` 不透明指针），对应「客户端 / 会话 / 服务端 / 事件循环」模型；HDC 的完整通信机制详见下一讲 u3-l2。

HDC 承载的「业务类型」枚举：

[ascend_hal_base.h:L76-L101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L76-L101) —— `drvHdcServiceType` 列举了 HDC 通道能跑哪些业务：`HDC_SERVICE_TYPE_DMP`（设备管理协议）、`PROFILING`、`LOG`、`RDMA`、`TSD`、`TDT`、`BBOX`…… 由此可见 HDC 是一个被 DMC（日志/性能/维测）、TRS、TDT 等众多模块复用的公共通信底座。

设备间点对点访问（P2P）：

[ascend_hal_base.h:L1720](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1720) —— `halDeviceEnableP2P(dev, peer_dev, flag)`（以及对应的 `Disable`/`CanAccessPeer`）开启两块 NPU 之间互相访问对方显存的能力，是多卡集合通信的前提。

资源地址映射 `halResAddrMap`（base.h:1581）—— 把设备侧 SoC 资源地址映射到从进程的虚拟地址空间，便于跨进程访问寄存器/L2 等资源。

> 速记表：

| 类别 | 代表接口 | 所在头 |
| --- | --- | --- |
| 内存管理 | `halMemAlloc` / `halMemFree` / `halMemcpy` / `halMemAddressReserve` / `halMemMap` | base.h |
| 内存管理（流水池） | `halBuffCreatePool` / `halMbufAlloc` | external.h |
| 流与任务 | `halStreamBackup` / `halShrIdCreate` / `halShrIdOpen`（SQ/CQ 在 TRS） | base.h |
| 队列事件 | `halQueueInit` / `halQueueGetStatus` / `halEschedCreateGrp` / `halEschedWaitEvent` | external.h |
| 通信 | HDC(`HDC_CLIENT` 等) / `halDeviceEnableP2P` / `halResAddrMap` | base.h |
| 设备生命周期/信息 | `halDeviceOpen` / `halGetDeviceInfo` / `halGetAPIVersion` | base.h |

#### 4.3.4 代码实践

**实践目标**：动手把 `hal*` 接口按四类归类，并据此说明 HAL 与 DCMI 调用者的差异。

**操作步骤**：

1. 打开 `pkg_inc/ascend_hal_base.h` 与 `pkg_inc/ascend_hal_external.h`。
2. 用搜索（`DLLEXPORT`）列出所有导出函数。
3. 按下表把每个接口归入「内存管理 / 流与任务 / 队列事件 / 通信 / 设备信息」之一，每类至少填 2～3 个：

| 类别 | 你找到的接口（2～3 个） | 一句话用途 |
| --- | --- | --- |
| 内存管理 | … | … |
| 流与任务 | … | … |
| 队列事件 | … | … |
| 通信 | … | … |

4. 对照 4.3.3 的速记表自查。

**需要观察的现象**：`halMem*` 家族（base.h 中部，约 2142～3221 行）数量最多；`halQueue*` / `halEsched*` 集中在 external.h 后半段；HDC 相关多为「类型定义」而非直接 `hal*` 函数（真正的 hdc client/server 接口在 `src/ascend_hal/hdc/`）。

**预期结果**：你能为四类各举出至少 2 个真实接口名，并能说出 HAL 接口「面向运行时、以逻辑 devid 为单位、返回 drvError_t」，与 DCMI「面向管理工具、以 card+device 定位」形成对照。

#### 4.3.5 小练习与答案

**练习 1**：`halMemcpy` 的拷贝方向由哪个参数决定？有哪些取值？
**答案**：由 `struct memcpy_info *info` 中的 `dir` 字段决定，类型为 `drvMemcpyKind_t`，取值有 `DRV_MEMCPY_HOST_TO_HOST / HOST_TO_DEVICE / DEVICE_TO_HOST / DEVICE_TO_DEVICE` 四种（base.h:293-298）。

**练习 2**：`halQueueInit` 等接口后面常带 `ASCEND_HAL_WEAK`，这说明什么？
**答案**：`ASCEND_HAL_WEAK`（`__attribute__((weak))`）表示弱符号，即该接口在 `libascend_hal.so` 里可以没有强实现；若库里未提供，链接不报错，但运行时调用会失败/返回错误。这通常意味着该能力是可选的，或由其他库/模块按场景注入实现。

**练习 3**：HDC 的 `drvHdcServiceType` 枚举能说明 HAL 的什么架构特点？
**答案**：它表明 HDC 是一个**公共通信底座**——DMP、Profiling、Log、RDMA、TSD、TDT、Bbox 等互不相关的业务都复用同一套主机-设备消息通道，体现了 HAL「统一抽象、复用基础设施」的设计思路。

---

## 5. 综合实践

**任务**：写一份《HAL 公共接口速查与调用者分析》小报告，把本讲三个模块串起来。

要求完成以下三件事：

1. **画两张图**：
   - 图 A：`ascend_hal.h` 的头文件包含依赖图（对应 4.2.1），标注每个子头的职责。
   - 图 B：四类 `hal*` 接口在「打开设备 → 跑一次推理 → 关闭设备」流程中的出场顺序（对应 4.3.2）。
2. **填一张分类表**：从 `ascend_hal_base.h` / `ascend_hal_external.h` 中，按「内存管理 / 流与任务 / 队列事件 / 通信」各挑 2～3 个接口，写成「接口名 — 所在头:行号 — 类别 — 一句话用途」四列表。所有行号必须是你亲自在源码中核对过的。
3. **回答一个论述题**（不少于 150 字）：假设你要为一个新的上层模块做技术选型，请说明什么情况下应该调用 HAL（`hal*`）接口、什么情况下应该调用 DCMI（`dcmi_*`/`dsmi_*`）接口，并各举一个真实场景。要点提示：HAL 面向「要用 NPU 算力/显存」的运行时（如分配显存、拷贝数据），DCMI 面向「要管 NPU 设备」的工具（如查温度、复位芯片）。

**进阶（可选）**：尝试在装好 driver 的环境里编译运行 4.1.4 的 `halGetAPIVersion` 示例，把打印出的版本号与头文件里 `__HAL_API_VER_MAJOR/MINOR/PATCH` 对照，验证「接口版本握手」机制；运行结果若无法获取，记为「待本地验证」。

## 6. 本讲小结

- HAL 是三层架构中**体量最大**的一层，编译为用户态动态库 `libascend_hal.so`，面向**计算运行时**（acl/Runtime），与面向管理工具的 DCMI 形成「用」与「管」的分工。
- `ascend_hal.h` 是一个**聚合头**，经由 `define.h / base.h / external.h / dc.h` 把全部公共接口串成一个入口；`define.h` 是公共字典，`base.h` 是主力接口头，`external.h` 是底层叶子（定义 `DLLEXPORT`），`error.h` 提供统一错误码 `drvError_t`（`DRV_ERROR_NONE = 0`），`dc.h` 当前为空占位。
- HAL 用 `DLLEXPORT`（`visibility("default")`）控制导出符号，用 `__HAL_API_VERSION`（major/minor/patch）做接口版本握手，用 `HAI_ERROR_CODE` 生成带模块号的新式错误码。
- `hal*` 接口可归为五大类：**内存管理**（`halMem*`，最多）、**流与任务**（`halStream*`/`halShrId*`，SQ/CQ 在 TRS）、**队列事件**（`halQueue*`/`halEsched*`）、**通信**（HDC/P2P）、**设备生命周期与信息**（`halDeviceOpen`/`halGetDeviceInfo`）。
- 判断一个能力属于 HAL 还是 DCMI 的核心标准：看调用者是「跑算子、要显存、要拷数据」的运行时（HAL），还是「看状态、做复位、做升级」的管理工具（DCMI）。

## 7. 下一步学习建议

本讲只看了 HAL 的**接口契约**（头文件），还没有进入实现。建议按以下顺序继续：

1. **u3-l2 主机-设备通信：HDC client/server/core 模型** —— 本讲多次提到的 HDC 是 HAL 各模块的通信底座，下一讲深入 `src/ascend_hal/hdc/` 看 client/server/core/epoll 如何协作，是把「通信」这一类接口落到实现的第一步。
2. **u3-l3 / u3-l4 PBL 基础库（UDA / URD / commlib）** —— HAL 不是铁板一块，它的公共能力被抽到 PBL（Public Base Lib），这两讲带你认识 HAL 的内部地基。
3. **单元 4 SVM 共享虚拟内存** —— 本讲「内存管理」类的 `halMem*` 接口的真正实现就在 SVM，那是 HAL 最庞大的一块，值得用整个单元深入。
4. **u6-l3 TRS 任务资源调度** —— 本讲「流与任务」类点到即止的 SQ/CQ 通信，在内核态 `trsdrv` 与用户态 `trs` 子模块里有完整实现。

阅读源码时，建议随时回到本讲的分类表与依赖图作为「地图」，避免在 6000 行的 `ascend_hal_base.h` 里迷路。
