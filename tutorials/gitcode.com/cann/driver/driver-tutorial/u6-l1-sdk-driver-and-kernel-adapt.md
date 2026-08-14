# SDK-driver 层总览与 kernel_adapt 内核适配

## 1. 本讲目标

本讲是单元 6（SDK-driver 内核层）的第一篇，目标是从前面几单元一直停留在的「用户态」视角，第一次真正下沉到**内核态**。

学完后你应该能够：

- 说清 `sdk_driver` 层与 `ascend_hal` 层的「用户态 / 内核态」关系，以及 `ioctl` 是怎么把两边连起来的。
- 理解 `kernel_adapt`（内核适配）模块在整个内核驱动里的**底座**地位：它是唯一一处直接调用 Linux 内核 API 的代码，其它内核子模块都只调它导出的 `ka_*` 符号。
- 掌握 kernel_adapt 屏蔽不同内核版本差异的**三种机制**：`LINUX_VERSION_CODE` 宏分支、`IS_ENABLED(CONFIG_*)` 配置门控、`conftest` 运行期探测。
- 看懂 `ka_module_init.c` 的模块加载流程，以及 `ka_base` / `ka_mem` / `ka_driver` 三个最小模块各自的适配套路。
- 能够对照 `ka_*_pub.h` 公共头文件，列举出 kernel_adapt 提供的「内存 / PCI / 设备模型 / 文件系统 / 调度」等几大类能力。

---

## 2. 前置知识

### 2.1 用户态与内核态

回顾 u3-l1、u3-l2 已经建立的心智模型：`ascend_hal` 编译为用户态动态库 `libascend_hal.so`，运行在普通进程里；而 `sdk_driver` 编译为 Linux 内核模块（`.ko`），运行在内核态，拥有直接操作硬件、申请物理页、注册中断的全部特权。

二者之间的边界是 **`ioctl` 系统调用**：用户态库把请求封装好，通过打开的字符设备（如 `/dev/davinci_manager`）执行 `ioctl` 陷入内核，内核态驱动接收并处理后再原路返回。

```
应用进程 (用户态)
   │  libascend_hal.so
   │  ioctl(/dev/davinci_manager, CMD, arg)
   ▼  ═══ 系统调用边界 ═══
sdk_driver .ko (内核态)
   │  ┌─────────────────────────────┐
   │  │ trsdrv / fms / vascend / …  │  ← 业务子模块（调用 ka_*）
   │  │          ▼                   │
   │  │      kernel_adapt            │  ← 本讲：唯一触碰内核 API 的底座
   │  │          ▼                   │
   │  │  Linux 内核 (mm / pci / fs)  │
   │  └─────────────────────────────┘
   ▼
NPU 硬件
```

### 2.2 内核模块（.ko）与 EXPORT_SYMBOL

Linux 内核模块用 `module_init` / `module_exit` 宏注册加载与卸载入口，编译产物是 `.ko`（kernel object）。模块内部想让**别的内核模块**调用的函数，必须用 `EXPORT_SYMBOL` 或 `EXPORT_SYMBOL_GPL` 显式导出，否则符号不会出现在内核符号表中、外部无法链接。kernel_adapt 正是把封装好的 `ka_*` 函数大量 `EXPORT_SYMBOL_GPL` 出去，供同一套 `.ko` 体系内的其它驱动子模块调用。

### 2.3 LINUX_VERSION_CODE：内核版本的「时间戳」

Linux 内核对每个发布版赋予一个 32 位整数 `LINUX_VERSION_CODE`，由主、次、修订号拼成：

\[
\text{LINUX\_VERSION\_CODE} = (MAJOR \ll 16) \,|\, (MINOR \ll 8) \,|\, PATCH
\]

`KERNEL_VERSION(a, b, c)` 宏用于把 `(a, b, c)` 拼成同样格式以便比较。于是 `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 8, 0)` 这种写法，就能在**预处理期**根据当前编译的内核头文件版本，挑选不同的实现。这是 kernel_adapt 最核心的适配手段。

### 2.4 为什么必须做「内核适配」？

昇腾驱动要适配 openEuler、CentOS、Ubuntu 等众多发行版，它们的内核版本从 4.19 一路跨到 6.8+。Linux 内核内部 API 变化极快——同名函数改签名、改语义，甚至整个子系统（如 VFIO/mdev）被重写是常态。如果业务代码直接调 `get_user_pages_remote(...)`，换一个内核版本就编译不过或行为错误。kernel_adapt 的存在意义就是：**把所有跟内核版本相关的脏活脏分支，集中到一处**，对上暴露一套稳定的 `ka_*` 接口。

> 关键术语速查：用户态 / 内核态、`.ko`、`ioctl`、`module_init`、`EXPORT_SYMBOL_GPL`、`LINUX_VERSION_CODE`、`IS_ENABLED(CONFIG_*)`、`conftest`、openEuler。

---

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `src/sdk_driver/kernel_adapt/` 下）：

| 文件 | 作用 |
|------|------|
| [`ka_module_init.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c) | 模块加载/卸载入口（`module_init`/`module_exit`），加载时建立系统 RAM 区间红黑树 |
| [`Makefile`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/Makefile) | 把各子目录的 `ka_*.o` 链接成单一模块 `ascend_kernel_open_adapt.ko` |
| [`kernel_adapt_init.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/kernel_adapt_init.h) | 公共日志宏（`ka_err`/`ka_info`/…）与 `STATIC` 控制 |
| [`base/ka_base.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c) | 基础设施薄封装：随机数、proc、cdev owner 等，逐符号导出 |
| [`memory/ka_mem.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c) | 内存子系统适配：GUP/pin、页表遍历、vm_flags、RAM 记录 |
| [`driver/ka_driver.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c) | 设备模型 / VFIO / mdev 适配（体量最大，跨版本差异最剧烈） |
| [`include/ka_*_pub.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/include/ka_base_pub.h) | 对外公共头：把 `ka_*` 宏/类型/函数声明统一暴露给上层 |
| [`conftest/conftest.sh`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/conftest/conftest.sh) | 编译期探测目标内核是否拥有某函数/宏/头文件 |

---

## 4. 核心概念与源码讲解

### 4.1 kernel_adapt 模块定位与内核模块加载

#### 4.1.1 概念说明

`kernel_adapt` 是 `sdk_driver` 树最底层的子模块，全称「kernel open adapt」（见 `MODULE_DESCRIPTION("kernel open adapt module")`）。它的定位可以用一句话概括：**昇腾内核驱动里唯一一处直接 `#include <linux/...>`、直接调用 Linux 内核 API 的代码**。它上面的所有内核业务子模块（trsdrv 任务调度、fms 故障管理、vascend 算力切分等）都只调用 kernel_adapt 导出的 `ka_*` 接口，从而与具体的 Linux 内核版本彻底解耦。

这种「适配层 / 业务层」分离的好处是：当 Linux 内核升级、API 漂移时，**只有 kernel_adapt 需要改**，业务代码保持不变——这正是它叫「adapt」的原因。

#### 4.1.2 核心流程

kernel_adapt 本身被编译为一个独立的内核模块，加载流程遵循标准 Linux 模块规范：

1. `insmod ascend_kernel_open_adapt.ko` 触发 `module_init` 注册的 `ka_module_init`。
2. `ka_module_init` 调用 `ka_mm_ram_record_init()`，遍历主机所有「系统 RAM」物理区间，建一棵红黑树（供后续 O(log n) 判断某物理地址是否为内存）。
3. 模块常驻内核，通过 `EXPORT_SYMBOL_GPL` 把数百个 `ka_*` 符号注入内核符号表。
4. 其它昇腾内核子模块加载后，按需调用这些 `ka_*` 符号。
5. `rmmod` 时触发 `module_exit` 注册的 `ka_module_exit`，销毁红黑树。

#### 4.1.3 源码精读

**模块入口**（注意它本身就在做版本适配——`__init`/`__exit` 注解是条件编译的）：

[src/sdk_driver/kernel_adapt/ka_module_init.c:21-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c#L21-L43) — `ka_module_init` 仅做 `ka_mm_ram_record_init()`，并按内核版本决定函数是否带 `__init`/`__exit` 修饰（>4.19.25 才加）；最后用 `module_init`/`module_exit`/`MODULE_LICENSE("GPL")` 完成标准模块注册。

```c
#if LINUX_VERSION_CODE > KERNEL_VERSION(4, 19, 25)
STATIC int __init ka_module_init(void)
#else
STATIC int ka_module_init(void)
#endif
{
    (void)ka_mm_ram_record_init();
    return 0;
}
...
module_init(ka_module_init);
module_exit(ka_module_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("kernel open adapt module");
```

> 注意外层 `#ifndef EMU_ST`（[ka_module_init.c:14](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c#L14)）：`EMU_ST` 是软件模拟（仿真）构建标志，模拟环境下不编成真模块，而是提供空壳 `ka_module_init`/`ka_module_exit`。这是除了内核版本外的另一类「形态」适配。

**编译成单一模块**：[src/sdk_driver/kernel_adapt/Makefile:83-96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/Makefile#L83-L96) 用 `obj-m += ascend_kernel_open_adapt.o` 把所有子目录的 `ka_*.o` 聚合链接成一个名为 `ascend_kernel_open_adapt` 的内核模块（产物 `.ko`）。

```makefile
obj-m += ascend_kernel_open_adapt.o
ascend_kernel_open_adapt-objs := ka_module_init.o  \
    task/ka_task.o fs/ka_fs.o system/ka_system.o base/ka_base.o \
    driver/ka_driver.o memory/ka_mem.o memory/ka_pgwalk.o \
    pci/ka_pci.o net/ka_net.o dfx/ka_dfx.o sched/ka_sched.o
```

**层级佐证**：[README.md:91](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L91) 把 `sdk_driver` 标注为「SDK 层源码文件夹」，与 `ascend_hal`（用户态）、`custom`（定制层）并列——再次印证三层架构里 sdk_driver 是内核侧。

#### 4.1.4 代码实践

**实践目标**：确认 kernel_adapt 编译产物形态与模块入口。

**操作步骤**（源码阅读型，需在装有昇腾驱动的目标机器上验证）：

1. 阅读 [Makefile:83-96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/Makefile#L83-L96)，记下模块名 `ascend_kernel_open_adapt` 与全部 `-objs`。
2. 若机器已安装昇腾驱动，执行 `lsmod | grep ascend_kernel_open_adapt` 查看模块是否已加载。
3. 执行 `modinfo <驱动安装路径>/ascend_kernel_open_adapt.ko`，对照 `MODULE_DESCRIPTION` / `MODULE_LICENSE`。

**需要观察的现象**：`modinfo` 输出的 `description` 应为 `kernel open adapt module`，`license` 为 `GPL`。

**预期结果**：模块以独立 `.ko` 形态存在；若机器未安装驱动或无 NPU，则无法看到，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`ka_module_init` 函数体只调用了一个函数，为什么这个初始化如此「轻」？  
**答案**：kernel_adapt 是**被动提供能力**的适配库，没有自己的后台线程或设备要管理；它唯一需要在加载期做的准备工作，是建立供后续查询使用的系统 RAM 红黑树（`ka_mm_ram_record_init`），所以入口很轻。

**练习 2**：为什么 `MODULE_LICENSE` 必须是 `"GPL"`？  
**答案**：内核里大量 API（包括 kernel_adapt 重度依赖的内存、VFIO 等接口）只对声明 GPL 许可的模块导出；非 GPL 模块无法 `EXPORT_SYMBOL_GPL` 也能用，但会触发「内核被污染（tainted）」警告，且部分接口不可见。

---

### 4.2 版本适配的三种机制

#### 4.2.1 概念说明

kernel_adapt 适配内核差异，靠的是三套互补的机制，按「确定性」从高到低排列：

| 机制 | 触发时机 | 适用场景 | 代表写法 |
|------|----------|----------|----------|
| ① `LINUX_VERSION_CODE` 宏分支 | 预处理期 | 差异与内核版本号一一对应 | `#if LINUX_VERSION_CODE >= KERNEL_VERSION(5,8,0)` |
| ② `IS_ENABLED(CONFIG_*)` 配置门控 | 预处理期 | 某特性由内核编译配置决定（如 VFIO、KVM） | `#if IS_ENABLED(CONFIG_VFIO)` |
| ③ `conftest` 编译期探测 | 编译前脚本探测 | 版本号不可靠（发行版回移植、fork 内核） | 脚本试编译，生成 `CONFTEST_*` 宏 |

机制①最常用——全仓库 `LINUX_VERSION_CODE` 出现 **264 次**（覆盖 23 个文件），是绝对主力。机制②用于可选子系统。机制③是「兜底」：当 openEuler 这类发行版把高版本特性**回移植**到低版本号内核时，光看版本号会误判，必须实打实地试编译一段代码看符号是否存在。

#### 4.2.2 核心流程

- **机制①②**：C 预处理器在读到 `#if` 时，根据内核头里的 `LINUX_VERSION_CODE` 宏与 `CONFIG_*` 宏求值，保留符合条件的分支、丢弃其余分支——**编译产物里只会有一个版本的代码**，零运行期开销。
- **机制③**：`conftest.sh` 在正式编译前跑一遍，针对 `check_tables.csv` 列出的每一项（函数/宏/头文件），用目标内核头试编译一个小 C 文件；若成功则定义对应宏（如 `CONFTEST_func_pci_aer_clear_nonfatal_status_present`），否则置 0。源码里再据此选择实现。

#### 4.2.3 源码精读

**机制①的密度**（用搜索验证）：在 `src/sdk_driver/kernel_adapt/` 下，`memory/ka_mem.c` 一文就有 28 处 `LINUX_VERSION_CODE`，`driver/ka_driver.c` 多达 60 处——后者因为 VFIO/mdev 子系统几乎每个大版本都改 API。

**机制②的样例**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:226-229](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L226-L229) 中 `ka_vfio_pin_pages` 用 `#if IS_ENABLED(CONFIG_VFIO)` 门控整个 VFIO 调用——目标内核若未编入 VFIO 支持，这段代码退化为直接返回错误，不会链接失败。

```c
int ka_vfio_pin_pages(ka_vfio_device_t *vfio_device, ka_pin_info *pin_info)
{
    int ret = -1;
#if IS_ENABLED(CONFIG_VFIO)
#if KA_IS_ASCEND_HOST_KERNEL
    ...  // 真正的 vfio_pin_pages 调用
```

**机制③的样例**：[src/sdk_driver/kernel_adapt/conftest/check_functions/check_tables.csv:1-10](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/conftest/check_functions/check_tables.csv#L1-L10) 列出待探测项，分三类：`functions`（函数是否存在）、`macros`（宏是否存在）、`headers`（头文件是否存在）。例如 `func_pci_aer_clear_nonfatal_status_present` 探测的就是一个在不同内核里时有时无的 PCIe AER 接口。

驱动脚本 [conftest.sh:32-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/conftest/conftest.sh#L32-L44) 用 `uname -r` 取目标内核版本，定位 `/usr/src/kernels/<版本>` 头文件目录，后续逐项试编译。

#### 4.2.4 代码实践

**实践目标**：用搜索工具量化三种机制的使用密度。

**操作步骤**：

1. 在仓库根目录执行（只读）：  
   `grep -rc "LINUX_VERSION_CODE" src/sdk_driver/kernel_adapt | sort -t: -k2 -nr`  
   找出「最依赖版本分支」的文件。
2. 执行 `grep -rc "IS_ENABLED" src/sdk_driver/kernel_adapt/include` 看配置门控分布。
3. 打开 `check_tables.csv` 统计三类探测项各有多少条。

**需要观察的现象**：`ka_driver.c`、`ka_mem.c` 的 `LINUX_VERSION_CODE` 计数远高于 `ka_base.c`——说明设备模型与内存子系统的 API 漂移最严重。

**预期结果**：与本章给出的统计一致（`ka_driver.c` ≈ 60，`ka_mem.c` ≈ 28，`ka_base.c` ≈ 5）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能全靠机制①（版本号分支），还需要机制③（conftest 探测）？  
**答案**：发行版（尤其 openEuler）常把高版本特性回移植到低版本号内核，版本号与实际能力不一致；conftest 用「试编译」实测符号是否存在，比版本号更可靠，是兜底手段。

**练习 2**：机制①②在运行期有没有开销？  
**答案**：没有。它们都是预处理期 `#if`，编译后产物只含一个分支的代码，零运行期判断开销。

---

### 4.3 ka_base：稳定符号与薄封装

#### 4.3.1 概念说明

`ka_base` 是 kernel_adapt 里最「纯」的适配模块：它的每个函数都极短，只做一件事——**把一个随内核版本变化的小 API，包装成一个稳定的 `ka_*` 符号导出**。这是「薄封装（thin wrapper）」模式的典范：函数体里只有一个 `#if LINUX_VERSION_CODE` 分支，挑出当前内核对应的原生调用，再用 `EXPORT_SYMBOL_GPL` 把包装后的符号送出。

它的搭档 [`ka_base_pub.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/include/ka_base_pub.h) 则用 `typedef`/`#define` 把内核类型与宏整体改名（如 `typedef struct rb_node ka_rb_node_t;`、`#define ka_base_min(x,y) min(x,y)`），让上层代码写 `ka_*` 而永不触碰原生名。

#### 4.3.2 核心流程

封装一个原生 API 的标准三步：

1. 在 `ka_base_pub.h` 声明稳定的 `ka_*` 原型（参数与返回值尽量用 `ka_*` 类型）。
2. 在 `ka_base.c` 实现函数体，内含按版本选择原生调用的 `#if` 分支。
3. 用 `EXPORT_SYMBOL_GPL(ka_xxx)` 导出，供其它内核模块调用。

#### 4.3.3 源码精读

**样例一·随机数改名**：[src/sdk_driver/kernel_adapt/base/ka_base.c:41-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c#L41-L49)。内核在 4.12 把 `get_random_int()` 换成更安全的 `get_random_u32()`，ka_base 用版本分支把它包装成永远可用的 `ka_base_get_random_u32`：

```c
u32 ka_base_get_random_u32(void)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 12, 0)
    return get_random_u32();
#else
    return get_random_int();
#endif
}
EXPORT_SYMBOL_GPL(ka_base_get_random_u32);
```

**样例二·proc 数据接口改名**：[src/sdk_driver/kernel_adapt/base/ka_base.c:31-39](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c#L31-L39)。5.17 起 `PDE_DATA(inode)` 被重命名为 `pde_data(inode)`，ka_base 同样包装：

```c
void *ka_base_pde_data(const ka_inode_t *inode)
{
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(5, 17, 0))
    return pde_data(inode);
#else
    return PDE_DATA(inode);
#endif
}
```

**样例三·用版本号做「能力存在性」判断**：[src/sdk_driver/kernel_adapt/base/ka_base.c:57-65](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c#L57-L65)。4.0 之后 `find_module()` 不再导出，于是 `ka_base_find_module` 在高版本直接返回 `NULL`，把「能力消失」也封装起来。

#### 4.3.4 代码实践

**实践目标**：把一个 `ka_*` 符号的「适配全链路」走一遍。

**操作步骤**：

1. 在 `ka_base.c` 选一个符号，如 `ka_base_get_random_u32`。
2. 在 `ka_base_pub.h` 中找到它的声明（搜索同名）。
3. 全仓库搜索 `ka_base_get_random_u32` 的调用点，观察上层模块如何使用。

**需要观察的现象**：上层调用方代码里**只出现 `ka_base_get_random_u32`，绝不出现** `get_random_u32` 或 `get_random_int`——这正是适配层存在的证据。

**预期结果**：原生内核符号被完全隔离在 `ka_base.c` 内部；其余文件对它「无感」。

#### 4.3.5 小练习与答案

**练习 1**：`ka_base_register_func_by_pdev`（[ka_base.c:78-86](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c#L78-L86)）返回的是一个**函数指针**，这种「按版本选回调」的封装与普通包装有何不同？  
**答案**：它不是在调用点分支，而是在**注册期**就根据内核版本把正确的回调函数指针交给上层；之后调用方直接通过指针调用，运行期无分支开销。适合差异点在「该用哪个函数」而非「函数怎么调」的场景。

**练习 2**：为什么导出宏用 `EXPORT_SYMBOL_GPL` 而不是 `EXPORT_SYMBOL`？  
**答案**：GPL 导出表示该符号只能被 GPL 许可的模块使用（kernel_adapt 自身就是 GPL），语义上更严格，也便于合规审查；与昇腾开源策略一致。

---

### 4.4 ka_mem：内存子系统 API 漂移适配

#### 4.4.1 概念说明

如果说 `ka_base` 封装的是零散小 API，那么 `ka_mem` 封装的是**整个内存管理子系统**——而 mm 子系统恰恰是 Linux 内核里 API 漂移最严重的区域之一。昇腾驱动需要在主机内存里为 NPU 钉住用户页、建立页表映射、判断物理地址是否为 RAM，这些都依赖 mm 核心 API，而这些 API 在 4.x → 6.x 演进里反复改签名、改语义。`ka_mem` 的价值就是把这种「多级漂移」收敛成稳定接口。

它依赖的头 [`ka_memory_pub.h`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/include/ka_memory_pub.h) 是整个 kernel_adapt 里最大的公共头，大量使用宏别名（`#define ka_mm_kmalloc kmalloc`）与类型别名（`typedef struct mm_struct ka_mm_struct_t;`），把 mm 子系统的「词汇表」整体 `ka_` 化。

#### 4.4.2 核心流程

mm 适配有三种典型形态：

- **改名型**：同一个东西换了名字（`mmap_sem` → `mmap_lock`），用版本分支取正确字段。
- **签名演进型**：函数参数个数随版本递增（如 `get_user_pages_remote` 从 5 参变到 7 参再到 `pin_user_pages_remote`），用**多级级联 `#elif`** 选对的重载。
- **语义变化型**：字段从普通 `unsigned long` 变成需专用 helper 操作的原子量（如 6.3 的 `vm_flags`），封装成 setter/getter。

#### 4.4.3 源码精读

**改名型——mmap 锁**：[src/sdk_driver/kernel_adapt/memory/ka_mem.c:48-56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L48-L56)。5.8 起读写字段 `mm->mmap_sem` 改名为 `mm->mmap_lock`：

```c
ka_rw_semaphore_t *ka_mm_get_mmap_sem(ka_mm_struct_t *mm)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 8, 0)
    return &mm->mmap_lock;
#else
    return &mm->mmap_sem;
#endif
}
```

**签名演进型——pin/get user pages（最经典）**：[src/sdk_driver/kernel_adapt/memory/ka_mem.c:58-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L58-L80)。这一个函数用 **7 段级联** 覆盖 6.5 / 5.10 / 5.9 / 4.10 / 4.9 / 4.6 / 更早，展现了「参数从少到多、函数从 `get_*` 演进到 `pin_*`」的完整变迁：

```c
long ka_mm_pin_user_pages_remote(ka_task_struct_t *tsk, ka_mm_struct_t *mm,
                                 unsigned long start, unsigned long nr_pages,
                                 unsigned long gup_flags, ka_page_t **pages, int *locked)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 5, 0)
    got_num = pin_user_pages_remote(mm, start, nr_pages, gup_flags, pages, locked);
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
    got_num = pin_user_pages_remote(mm, start, nr_pages, gup_flags, pages, NULL, locked);
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(5, 9, 0)
    got_num = get_user_pages_remote(mm, start, nr_pages, gup_flags, pages, NULL, locked);
    ...  // 一路到 4.6 之前用 get_user_pages_locked
#endif
}
```

> 直觉解释：把用户态虚拟地址「钉住」成物理页，是 NPU 做 DMA、共享内存的基础。Linux 为修正长期 pin 的引用计数 bug，先在 5.10 引入 `pin_user_pages_*`（与 `get_user_pages_*` 区分 FOLL_PIN 语义），又陆续调整参数；ka_mem 把这条混乱演进线抹平为一个 `ka_mm_pin_user_pages_remote`。

**语义变化型——vm_flags 原子化**：[src/sdk_driver/kernel_adapt/memory/ka_mem.c:105-113](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L105-L113)。6.3 起 `vma->vm_flags` 改为只能用 `vm_flags_set()/vm_flags_clear()` 修改（禁止直接 `|=`），ka_mem 封装成 setter：

```c
void ka_mm_set_vm_flags(ka_vm_area_struct_t *vma, unsigned long flags)
{
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 3, 0))
    vm_flags_set(vma, (vm_flags_t)flags);
#else
    vma->vm_flags |= flags;
#endif
}
```

**页表层级数变化——4 级 → 5 级**：[src/sdk_driver/kernel_adapt/memory/ka_mem.c:304-365](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L304-L365)。`ka_mm_get_pte` 做页表遍历，4.11 起内核引入 `p4d` 层（5 级页表），遍历路径从 `pgd→pud→pmd→pte` 变成 `pgd→p4d→pud→pmd→pte`；该函数用 `#if LINUX_VERSION_CODE >= KERNEL_VERSION(4,11,0)` 决定是否多走一层 p4d。

**模块加载期建表——RAM 记录**：[src/sdk_driver/kernel_adapt/memory/ka_mem.c:600-648](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L600-L648)。这就是 `ka_module_init` 调用的目标：`ka_mm_ram_record_init` 用 `walk_iomem_res_desc` 遍历主机所有「系统 RAM」区间插入红黑树，`ka_mm_mem_is_ram(pa)` 据此在 O(log n) 内判断某物理地址是不是真内存（驱动在做 DMA 映射前需要区分内存与 MMIO）。

#### 4.4.4 代码实践

**实践目标**：体会「签名演进型」分支的层级深度。

**操作步骤**：

1. 打开 [ka_mem.c:58-80](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L58-L80)。
2. 数一数 `ka_mm_pin_user_pages_remote` 有几段 `#elif`，每段对应哪个版本、参数差在第几个。
3. 再看 [ka_mem.c:206-217](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/memory/ka_mem.c#L206-L217) 的 `ka_mm_pin_user_pages_fast`，对比它的级联段数。

**需要观察的现象**：同一个语义「钉住用户页」，fast 与 remote 两个变体各自的版本分支数量与断点版本都不同，说明适配是**逐函数、逐签名**的细活。

**预期结果**：能口头说出 `pin_user_pages_remote` 在 6.5、5.10 两处关键参数变化（6.5 去掉了 `tsk` 与一个 `NULL` 槽位）。

#### 4.4.5 小练习与答案

**练习 1**：`ka_mm_get_mmap_sem` 返回的是 `ka_rw_semaphore_t *`（读写信号量指针）。为什么上层拿指针而非直接锁？  
**答案**：返回指针后，上层可灵活选用 `ka_mm_mmap_read_lock`/`ka_mm_mmap_write_lock`（也在 `ka_memory_pub.h` 用宏适配）做读或写锁定，比固化一种锁方式更通用。

**练习 2**：`ka_mm_set_vm_flags` 在 6.3 前后分别用「直接 `|=`」和「`vm_flags_set()`」。如果业务代码无视适配层、在新内核上直接 `vma->vm_flags |= flags` 会怎样？  
**答案**：6.3 起 `vm_flags` 改为受锁保护的特殊类型，直接 `|=` 会触发编译错误或运行期竞争告警；这正是必须经适配层的原因。

---

### 4.5 ka_driver：设备模型 / VFIO / mdev 适配与 ka_pub 能力地图

#### 4.5.1 概念说明

`ka_driver` 是 kernel_adapt 里**体量最大、版本差异最剧烈**的模块（`LINUX_VERSION_CODE` 计数 ≈ 60，居全模块之首）。它适配两块高变动区域：

1. **Linux 设备驱动模型**：`class_create`、`cdev`、`device` 等基础接口的小幅签名变化。
2. **VFIO / mdev 虚拟化框架**：从 4.x 到 6.8 几乎每个大版本都在重构——mdev 的 `mdev_parent_ops` 注册方式、`vfio_device_ops` 的回调名（`open`→`open_device`）、`vfio_pin_pages` 的参数与返回、甚至 `eventfd_signal` 的参数个数都在变。这块适配直接支撑了单元 7 将讲的 **vascend 算力切分**（u7-l5）与 **vmng 虚拟化管理**：把一颗物理 NPU 切成多个虚拟实例暴露给虚拟机，全靠 VFIO/mdev。

#### 4.5.2 核心流程

设备模型与虚拟化适配的套路仍是版本分支，但因差异大，常出现**整段函数体被不同版本各自实现**（而非一行分支）的情况。典型如 `ka_vfio_rw_gpa` 用四个 `#elif` 给出四套完全不同的实现。

#### 4.5.3 源码精读

**设备模型·class_create 签名变化**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:58-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L58-L66)。6.4 起 `class_create` 不再需要传 `owner`：

```c
ka_class_t *ka_driver_class_create(ka_module_t *owner, const char *name)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 4, 0)
    return class_create(name);
#else
    return class_create(owner, name);
#endif
}
```

**eventfd_signal 参数变化**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:1384-1397](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L1384-L1397)。6.8 起 `eventfd_signal(ctx, n)` 简化为 `eventfd_signal(ctx)`（计数参数被移除）。

**iommu_map 新增 GFP 参数**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:139-154](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L139-L154)。6.3 起 `iommu_map` 多了 `gfp` 分配标志参数，低版本则忽略之。

**VFIO 页钉住——多版本实现**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:226-266](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L226-L266)。`ka_vfio_pin_pages` 在 6.0 / 5.19 / 更早 三段里调用签名完全不同的 `vfio_pin_pages`，且低版本还需手工把 `pfn` 转成 `page`：

```c
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 0, 0))
    ret = vfio_pin_pages(vfio_device, pin_info->gfn << PAGE_SHIFT, ...);
#elif (LINUX_VERSION_CODE >= KERNEL_VERSION(5, 19, 0))
    ret = vfio_pin_pages(vfio_device, gfns, pin_info->npage, ...);
#else
    mdev = vfio_device_data(vfio_device);
    ret = vfio_pin_pages(mdev_dev(mdev), gfns, ...);
#endif
```

**mdev 驱动注册——API 三度重写**：[src/sdk_driver/kernel_adapt/driver/ka_driver.c:1344-1368](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/driver/ka_driver.c#L1344-L1368)。`ka_vdev_register_mdev_device` 在 6.1 用 `mdev_register_parent`、5.19 用 `mdev_register_device(dev, drv)`、更早用 `mdev_register_device(dev, ops)`——同一个意图三种完全不同的注册 API。

> 这些虚拟化适配的存在，说明 kernel_adapt 不只是「给昇腾业务代码兜底」，还要把 Linux 整个虚拟化栈的版本碎片化替上层（vascend/vmng）扛下来。

#### 4.5.4 代码实践（对应本讲核心实践任务）

**实践目标**：阅读 `ka_module_init.c` 与 `ka_base.c`，说明 kernel_adapt 如何封装内核 API；并写出 `ka_pub` 系列公共头文件提供的几大类能力。

**操作步骤**：

1. 重读 [ka_module_init.c:21-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c#L21-L43) 与 [ka_base.c:41-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/base/ka_base.c#L41-L49)，用一句话概括封装套路。
2. 进入 `src/sdk_driver/kernel_adapt/include/`，逐个打开 `ka_*_pub.h`，按下表归纳能力类别。

**封装套路小结**：kernel_adapt 把「随内核版本变化的原生 API」用 `#if LINUX_VERSION_CODE`/`IS_ENABLED`/conftest 三种机制包进 `ka_*` 函数或宏里，再 `EXPORT_SYMBOL_GPL` 导出；上层只调 `ka_*`，从而与具体内核版本解耦。

**`ka_pub` 能力类别对照表**（请你在阅读后自行补全实例）：

| 头文件 / 实现 | 能力类别 | 典型用途 |
|---------------|----------|----------|
| `ka_memory_pub.h` / `ka_mem.c` | **内存管理** | kmalloc、页表遍历、pin/get user pages、ioremap、DMA 映射、RAM 判定 |
| `ka_pci_pub.h` / `ka_pci.c` | **PCIe** | PCI 配置空间、MSI/MSI-X、电源状态（D0/D3）、AER、IOMMU |
| `ka_driver_pub.h` / `ka_driver.c` | **设备模型 / 虚拟化** | class/cdev 创建、VFIO 页钉住、mdev 注册（算力切分/虚拟机直通） |
| `ka_fs_pub.h` / `ka_fs.c` | **文件系统** | procfs / sysfs / debugfs、字符设备文件操作、copy_from/to_user |
| `ka_sched_pub.h` / `ka_sched.c` | **调度** | `cond_resched` 条件让 CPU（长循环里防占用过久） |
| `ka_task_pub.h` / `ka_task.c` | **任务 / 同步** | mutex / spinlock / rwlock、current 进程信息 |
| `ka_system_pub.h` / `ka_system.c` | **系统** | 系统信息、时间、errno |
| `ka_net_pub.h` / `ka_net.c` | **网络** | 网络相关内核接口适配 |
| `ka_dfx_pub.h` / `ka_dfx.c` | **可维可测** | 调试与诊断辅助 |
| `ka_kvm_pub.h` | **KVM** | KVM 总线/客机内存读写（配合 VFIO 路径） |

**需要观察的现象**：每个 `ka_*_pub.h` 顶部都 `#include <linux/version.h>` 与若干 `<linux/*>`，然后大量 `#define ka_xxx xxx` / `typedef ... ka_xxx_t;`——即「整本词汇表改名」。

**预期结果**：能不看本表，独立说出至少 5 类能力及其对应的头文件名。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ka_driver.c` 的 `LINUX_VERSION_CODE` 计数（≈60）远高于 `ka_base.c`（≈5）？  
**答案**：ka_driver 适配的 VFIO/mdev 子系统几乎每个大版本都重写 API，差异是「整段实现不同」级别；而 ka_base 封装的多是单行小 API（改名/加参数），一个函数一段分支就够，故计数低。

**练习 2**：`ka_vdev_register_mdev_device` 三段分支对应 6.1 / 5.19 / 更早三种注册 API。这种「同一意图、三种 API」的适配，最终给上层（vascend）带来了什么好处？  
**答案**：vascend 算力切分代码只需调用一个稳定的 `ka_vdev_register_mdev_device`，无需关心目标内核是哪个版本、mdev 框架用的是哪套注册接口；切分特性的可移植性因此大幅提升。

---

## 5. 综合实践

**任务**：以一名新内核模块作者的身份，把本讲四条主线串起来——「定位 → 加载 → 适配机制 → 能力地图」。

1. **定位**：画一张从用户态应用、经 `libascend_hal.so`、`ioctl` 陷入内核、到 `sdk_driver` 各业务子模块、再到 `kernel_adapt`、最后到 Linux 内核 mm/pci/fs 的完整调用栈草图，标注 `kernel_adapt` 是「唯一触碰内核 API 的层」。
2. **加载**：阅读 [ka_module_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/ka_module_init.c) 与 [Makefile:83-96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/kernel_adapt/Makefile#L83-L96)，说明模块名、加载时做了什么、为何入口这么轻。
3. **机制**：各举一例说明三种适配机制（`LINUX_VERSION_CODE`、`IS_ENABLED`、`conftest`），并解释为什么不能只靠版本号。
4. **能力地图**：完成 4.5.4 的 `ka_pub` 能力对照表，至少填出「内存 / PCIe / 设备模型 / 文件系统 / 调度」五类对应的头文件与一个典型函数。
5. **进阶思考**：假设你要为昇腾新增一个依赖内核 `foo_bar()` 的适配（`foo_bar` 在 5.10 加入、6.0 改了签名），仿照 `ka_base_get_random_u32` 写出 `ka_base_foo_bar` 的骨架代码（示例代码，标注清楚）。

**示例代码**（仅供思路参考，非仓库既有代码）：

```c
/* 示例代码：新增一个 ka_base 适配函数的骨架 */
int ka_base_foo_bar(unsigned long arg)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 0, 0)
    return foo_bar(arg, 0);          /* 6.0 起多了一个 flags 参数 */
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
    return foo_bar(arg);             /* 5.10 引入，单参数 */
#else
    return -EOPNOTSUPP;              /* 低版本无此能力，返回「不支持」 */
#endif
}
EXPORT_SYMBOL_GPL(ka_base_foo_bar);
```

---

## 6. 本讲小结

- `sdk_driver` 是昇腾驱动的**内核态**层（`.ko`），与用户态 `ascend_hal`（`.so`）经 `ioctl` 跨态通信；`kernel_adapt` 是其中最底层的适配底座。
- kernel_adapt 是**唯一**直接调用 Linux 内核 API 的内核子模块，其它业务子模块只调它导出的 `ka_*` 符号——这让业务代码与内核版本彻底解耦。
- 它靠**三种机制**屏蔽内核差异：`LINUX_VERSION_CODE` 宏分支（主力，264 处）、`IS_ENABLED(CONFIG_*)` 配置门控、`conftest` 编译期探测（兜底回移植场景）。
- `ka_base` 示范「薄封装」模式：一个函数一段版本分支，逐符号 `EXPORT_SYMBOL_GPL` 导出稳定接口。
- `ka_mem` 适配高漂移的 mm 子系统：mmap 锁改名、pin/get_user_pages 签名七级演进、vm_flags 原子化、4 级→5 级页表；并在模块加载期建 RAM 红黑树。
- `ka_driver` 适配设备模型与 VFIO/mdev 虚拟化（全模块差异最剧烈），为后续 vascend 算力切分、vmng 虚拟化管理打下底座；`ka_*_pub.h` 系列头文件提供内存 / PCIe / 设备模型 / 文件系统 / 调度等十大类能力。

---

## 7. 下一步学习建议

- **本单元续读**：本讲只讲了 kernel_adapt 这个「底座」。下一篇 **u6-l2（platform 平台资源管理）** 将进入 sdk_driver 业务层，看芯片中断、预留内存如何被解析存储；建议接着读 `src/sdk_driver/platform/`。
- **横向串联**：kernel_adapt 的虚拟化适配（VFIO/mdev）是单元 7 **u7-l5（vascend 算力切分 / vmng）** 的直接前提，学完本讲后再去看 vascend 会非常顺畅。
- **深入内存**：本讲提到的 pin/get user pages、页表遍历，与单元 4 **SVM 共享虚拟内存**（u4-l2、u4-l3）在内核侧的实现遥相呼应，可对照阅读 `src/sdk_driver/` 下 SVM 相关内核代码。
- **验证型延伸**：若手头有目标机器，尝试用 `modinfo`、`lsmod`、`dmesg` 观察本讲描述的模块加载与 `ka_info` 日志，把「源码认知」落到「运行现象」。
