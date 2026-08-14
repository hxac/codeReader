# DMS 设备管理系统（虚拟设备/flash/板级）

## 1. 本讲目标

本讲是单元 5（设备维护与管理系统）的第二讲。上一讲（u5-l1）我们读了 **DMC（Device Maintenance Components，设备维护组件）**——它提供的是 `device_monitor` 这一**通用消息收发框架**，以及 logdrv、prof 等**维测工具**，回答的是「怎么和设备通信、怎么采集日志与性能」。

本讲把视角从「维护组件」转向「**管理系统**」本身：**DMS（Device Management System，设备管理系统）**。它是一组面向「**管理什么**」的用户态能力集合——虚拟设备切分、flash 存储、板级信息、机密计算、低功耗与温度、时间同步、拓扑与 P2P……这些功能散落在 `src/ascend_hal/dms/` 下二十多个子目录里，但它们共享同一套 ioctl 基础设施。

读完本讲，你应当能够：

- 说清 **DMS 与 DMC 的分工差异**（管理系统 vs 维护组件），不再把二者混淆。
- 掌握 DMS 的**公共 ioctl 底座** `dms_user_common.c`：懒打开 `/dev/davinci_manager`、单 ioctl 按 `main_cmd/sub_cmd` 分发、errno 翻译、虚拟化环境标志。
- 掌握 **vdev 虚拟设备（SR-IOV）** 的查询与切换接口。
- 理解 **flash 存储** 与 **board 板级（机密计算 CC）** 两类接口如何复用统一的「get/set device info」分发原语。
- 能够浏览 `dms/` 任意子目录，快速判断它的管理职责与通信路径。

---

## 2. 前置知识

阅读本讲前，请确认你已建立以下认知（来自前置讲义）：

- **三层架构与 HAL 定位（u3-l1）**：driver 分 DCMI / HAL / SDK-driver 三层；HAL 编译为用户态动态库 `libascend_hal.so`，对外暴露 `hal*`/`dsmi*` 接口，返回值统一为 `drvError_t`，`DRV_ERROR_NONE = 0` 表示成功。
- **ioctl 陷入内核（u3-l1、u3-l2）**：用户态进程通过 `ioctl(fd, cmd, arg)` 系统调用陷入内核态驱动（`.ko`）；HDC 通信底座最终也经 `ioctl` 进内核。
- **URD 请求分发（u3-l4）**：PBL 的 URD 用「一个设备 fd（`/dev/davinci_manager`）+ 一个 ioctl 号」靠 `main_cmd/sub_cmd` 二维编号把命令分发到内核对应处理者；fd 在库加载时懒打开，带双检锁与 fork 安全（pid 检测）。**本讲的 `dms_user_common.c` 与 URD 是同一套设计思想**。
- **device_monitor 消息框架（u5-l1）**：DMC 用 `DM_CB_S`（调度中枢）+ `DM_INTF_S`（可插拔管道，HDC/UDP/selfloop）做异步消息收发，DSMI 命令可经它往返设备固件。

本讲会反复用到几个概念，先做通俗解释：

- **PF / VF（Physical Function / Virtual Function）**：PCIe SR-IOV 规范里的术语。一张物理 NPU 是一个 PF，开启 SR-IOV 后可切分出多个 VF（虚拟实例），每个 VF 拥有独立的设备号与资源配额，可分给不同虚拟机/容器。DMS 的 `vdev` 子模块就负责这类虚拟设备的信息查询与开关。
- **机密计算（CC，Confidential Computing）**：一种在「计算过程中」保护数据机密性的技术，数据在芯片内以密文运算。DMS 的 `board/dms_cc.c` 负责查询/设置芯片的 CC 模式与加密模式。
- **filter 字符串**：DMS 的一种「把 main_cmd+sub_cmd 拼进一个字符串、再用一个统一 ioctl 下发」的技巧，本质是把多维命令编号序列化进一个可被内核解析的过滤条件里。

> 一句话区分：**DMC 给你「通信管道 + 维测工具」，DMS 给你「具体的管理动作」**。DMS 的管理动作要落到设备上，往往就要借用 DMC/HDC 提供的管道，或直接走 URD/ioctl 同步通道。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/ascend_hal/dms/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/CMakeLists.txt) | DMS 模块的构建装配文件，列出全部子目录与按产品（`ascend910B`/`ascend950`）的特性开关 |
| [src/ascend_hal/dms/common/dms_user_common.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c) | **DMS 公共 ioctl 底座**：懒打开 `/dev/davinci_manager`、`DmsIoctl`/`DmsIoctlConvertErrno`、errno 翻译、虚拟化标志 |
| [src/ascend_hal/dms/common/dms_user_common.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.h) | 上述底座的头文件，含 `DMS_MAKE_UP_FILTER_*` 过滤宏、`DMS_VIRT_ADAPT_FUNC` 虚实适配宏 |
| [src/ascend_hal/dms/vdev/dms_vdev.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/vdev/dms_vdev.c) | **虚拟设备**模块：查询虚拟设备规格、SR-IOV 开关 |
| [src/ascend_hal/dms/flash/dms_flash.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/flash/dms_flash.c) | **flash 存储**模块：读/写设备 flash 信息（极薄包装） |
| [src/ascend_hal/dms/board/dms_cc.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_cc.c) | **板级/机密计算**模块：查询/设置 CC 模式（特性开关门控） |
| [src/ascend_hal/dms/board/dms_device_info.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c) | **通用 get/set device info 分发层**：`DmsGetDeviceInfo`/`DmsSetDeviceInfo`，flash/lpm 等模块复用它 |
| [src/ascend_hal/dms/lpm/dms_lpm.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/lpm/dms_lpm.c) | **低功耗管理**模块：频率、电压/电流、温度、TOPS、MCU 透传（综合实践参考） |
| [src/ascend_hal/dms/time/dms_time_zone.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/time/dms_time_zone.c) | **时间同步**模块：后台线程周期性把本地-UTC 时差同步给设备（综合实践参考） |
| [src/sdk_driver/dms/command/ioctl/dms_cmd_def.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/dms/command/ioctl/dms_cmd_def.h) | DMS ioctl 命令字定义（`DMS_IOCTL_CMD` 等），用户态/内核态共享 |
| [src/sdk_driver/dms/drv_devmng/inc/devmng_cmd_def.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/dms/drv_devmng/inc/devmng_cmd_def.h) | 内核侧 `DEVDRV_MANAGER_*` ioctl 命令字全集（DMS 的内核对应面） |

---

## 4. 核心概念与源码讲解

DMS 体量大、子目录多，但万变不离其宗：**所有 DMS 管理函数最终都要走「打开 `/dev/davinci_manager` → `ioctl(DMS_IOCTL_CMD, ...)` → 内核分发」这条同步通道**。因此本讲先讲透这条公共底座，再看四个典型管理模块如何复用它，最后在综合实践里横向浏览全部子目录。

### 4.1 dms_user_common：DMS 的公共 ioctl 底座

#### 4.1.1 概念说明

`dms/` 下有二十多个子目录，每个子目录都是一类「管理动作」（查虚拟设备、读写 flash、设置 CC 模式……）。如果每个模块都自己 open 设备、自己处理 errno、自己管 fd，代码会高度重复。于是 DMS 把这些公共活儿抽到 `common/dms_user_common.c`，提供两个核心入口：

- `DmsIoctl(cmd, ioarg)`：把「main_cmd + sub_cmd + 入参 + 出参」打包成一个 `urd_ioctl_arg`，用单一 ioctl 号 `DMS_IOCTL_CMD` 送到内核，内核再按 `main_cmd/sub_cmd` 分发。
- `DmsIoctlConvertErrno(cmd, ioarg)`：与上面类似，但额外把内核返回值/errno 自动翻译成对外的 `drvError_t`。

这正是 u3-l4 讲过的 **URD「一个 fd + 一个 ioctl 号 + main_cmd/sub_cmd 二维分发」** 设计在 DMS 里的再现。

#### 4.1.2 核心流程

DMS 底座的生命周期与一次调用的流程如下：

```text
库加载（dlopen/进程启动）
   │  __attribute__((constructor)) DmsInit()
   ├─ access(/dev/davinci_manager) 可读写？ 否 → 静默返回（环境无设备）
   └─ 是 → dms_open_intf()
            ├─ 双检锁 + getpid() 校验（fork 安全）
            ├─ mmOpen2(/dev/davinci_manager, M_RDWR|O_CLOEXEC)
            └─ ioctl(DAVINCI_INTF_IOCTL_CLOSE 之前的 open 握手)  → 缓存到 g_dms_fd

某管理接口被调用（如 dms_get_vdevice_info）
   │
   ├─ 填 ioarg：main_cmd / sub_cmd / input / output
   ├─ DmsIoctl(DMS_IOCTL_CMD, &ioarg)
   │     ├─ dms_open_intf()（懒打开，已开则直接复用 g_dms_fd）
   │     ├─ 虚拟化环境？ g_env_virt==true 且非 SRIOV → 直接返回 NOT_SUPPORT
   │     ├─ 把 dms_ioctl_arg 翻译成 urd_ioctl_arg
   │     └─ mmIoctl(fd, DMS_IOCTL_CMD, ...) → 陷入内核 → 内核分发
   └─ 处理返回值（errno_to_user_errno 翻译）

库卸载（dlclose/进程退出）
   └─ __attribute__((destructor)) dms_un_init() → dms_close_intf()
```

关键点有三：**懒打开**（constructor 只试探、真正 open 推迟到首次调用）、**fork 安全**（`getpid()` 不匹配则丢弃旧 fd 重开）、**虚拟化门控**（`g_env_virt` 标志在虚拟环境下屏蔽部分命令）。

#### 4.1.3 源码精读

**库自动初始化（constructor）**：进程加载 `libascend_hal.so` 时自动执行，先试探设备节点是否存在，再尝试打开：

```c
static void __attribute__((constructor)) DmsInit(void)
{
    mmProcess fd = DMS_INVALID_PID_OR_FD;
    if (access(DMS_DEVICE_FILE_NAME, R_OK | W_OK) != 0) {
        return;            // 无设备节点则静默返回，不报错
    }
    fd = dms_open_intf();
    ...
}
```

见 [src/ascend_hal/dms/common/dms_user_common.c:156-168](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L156-L168)：`access` 试探 + 懒打开，保证在「装了 HAL 库但当前没有 NPU」的环境里也不会启动失败。

**懒打开 + fork 安全**：

```c
/* to improve performance */
if (FdIsValid(g_dms_fd) && (g_dms_tgid == getpid())) {
        return g_dms_fd;          // 命中缓存：同进程且 fd 有效，直接复用
}
...
if (FdIsValid(g_dms_fd)) {
    if (g_dms_tgid != getpid()) {
        g_dms_fd = (mmProcess)DMS_INVALID_PID_OR_FD;   // fork 后子进程：fd 失效，重开
    } ...
}
fd = mmOpen2(DMS_DEVICE_FILE_NAME, M_RDWR|O_CLOEXEC, M_IRUSR);
```

见 [src/ascend_hal/dms/common/dms_user_common.c:88-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L88-L138)。`O_CLOEXEC` 保证 `exec` 时 fd 自动关闭，避免泄漏给子进程；`g_dms_tgid == getpid()` 防止 fork 出来的子进程误用父进程的 fd（fd 跨 fork 后语义已变）。

**核心入口 `DmsIoctl`**：

```c
int DmsIoctl(int cmd, struct dms_ioctl_arg *ioarg)
{
    ...
    fd = dms_open_intf();                       // 1. 懒打开
    if (!FdIsValid(fd)) { return DRV_ERROR_OPEN_FAILED; }
#ifndef CFG_FEATURE_SRIOV
    if (g_env_virt == true) { return DRV_ERROR_NOT_SUPPORT; }   // 2. 虚拟化门控
#endif
    /* ioarg translate urd_ioctl_arg */         // 3. 翻译成 URD 参数
    urd_ioarg.cmd.main_cmd = ioarg->main_cmd;
    urd_ioarg.cmd.sub_cmd  = ioarg->sub_cmd;
    urd_ioarg.cmd_para.input = ioarg->input; ...
    ret = mmIoctl(fd, cmd, &ioctlBuf);          // 4. 陷入内核
    if (ret < 0) {
        ret = (__errno_location() != NULL ? errno : EIO);
        share_log_read_err(HAL_MODULE_TYPE_DEV_MANAGER);   // 顺便采集错误日志
    }
    return ret;
}
```

见 [src/ascend_hal/dms/common/dms_user_common.c:175-220](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L175-L220)。注意第 2 步：`#ifndef CFG_FEATURE_SRIOV` 包起来的虚拟化门控——**只有编译时开启了 SR-IOV 特性，虚拟环境下才允许下发命令**；否则在容器/虚拟机里直接返回 `DRV_ERROR_NOT_SUPPORT`。这与 4.2 节的 vdev 模块直接相关。

**errno 翻译**：内核返回值与用户态 errno 的取值区间不同，需要统一翻译：

```c
/*
    |kernel return   | ioctl return | errno |
    |ret=[, -4096]   | ret          |       |
    |ret=[-4095, -1] | -1           | |ret| |
    |ret=[0, ]       | ret          |       |
*/
drvError_t ioctl_errno_convert(int ret, int errno_param)
{
    if (ret == -1) { return errno_to_user_errno(errno_param); }  // [-4095,-1] 走 errno
    if (is_valid_user_errno(ret)) { return ret; }                 // 已是合法用户码，直接用
    return DRV_ERROR_IOCRL_FAIL;                                  // 其余归为 ioctl 失败
}
```

见 [src/ascend_hal/dms/common/dms_user_common.c:227-247](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L227-L247)。`DmsIoctlConvertErrno` 就是「`ioctl` + `ioctl_errno_convert`」的组合包，见 [src/ascend_hal/dms/common/dms_user_common.c:249-275](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L249-L275)。

#### 4.1.4 代码实践

**实践目标**：理解 DMS 底座的「懒打开 + fork 安全」机制，并验证它在「无设备环境」下的容错行为。

**操作步骤（源码阅读型）**：

1. 打开 [src/ascend_hal/dms/common/dms_user_common.c:88-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.c#L88-L138)，找到 `dms_open_intf` 里的两处 `getpid()` 比较，思考：为什么不能只靠 `g_dms_fd` 是否有效来判断能否复用？
2. 对照 [u3-l4 讲过的 URD](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/urd/urd_user.c)，对比两者的懒打开写法（双检锁 + pid 校验），确认它们是同一套手法。
3. 在头文件 [src/ascend_hal/dms/common/dms_user_common.h:64](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.h#L64) 找到宏 `DMS_VIRT_ADAPT_FUNC(virt_func, phy_func)`，阅读其定义。

**需要观察的现象**：

- `DmsInit` 用 `access(... R_OK | W_OK)` 试探设备节点；若当前用户对 `/dev/davinci_manager` 无权限（普通用户），constructor 会**静默返回**而不报错。
- `DMS_VIRT_ADAPT_FUNC` 的作用是「按 `g_env_virt` 标志在两个函数指针间二选一」，让同一接口在物理机与虚拟环境下走不同实现。

**预期结果**：你能用自己的话回答——「DMS 底座为什么不会因为环境里没有 NPU 就让进程启动失败？」（答：constructor 的 `access` 试探 + 懒打开策略，把真正的失败推迟到首次实际调用时才以 `DRV_ERROR_OPEN_FAILED` 返回。）

#### 4.1.5 小练习与答案

**练习 1**：`DmsIoctl` 中 `#ifndef CFG_FEATURE_SRIOV` 这段虚拟化门控，为什么用编译宏 `CFG_FEATURE_SRIOV` 而不是只在运行时判断 `g_env_virt`？

**参考答案**：`CFG_FEATURE_SRIOV` 是**编译期**特性开关（见 4.6 节的 feature cmake）。若产品形态根本不支持 SR-IOV，则编译出的二进制里这段「虚拟化放行」逻辑就不该存在——既减小体积，也避免在不支持虚拟化的芯片上误放行。只有编译期开启 SR-IOV，运行期才进一步用 `g_env_virt` 判断当前是否真处于虚拟环境。这是 u3-l5 讲过的「编译期宏冻结能力 + 运行期判断可用」双层适配。

**练习 2**：`ioctl_errno_convert` 把内核返回值分了三段处理（`<= -4096`、`[-4095,-1]`、`>= 0`），为什么需要区分 `-1`？

**参考答案**：Linux 的 `ioctl` 系统调用约定：当内核返回值落在 `[-4095, -1]` 时，glibc 会把它转换成 `ioctl` 返回 `-1` 并把负值的绝对值写进 `errno`；而 `<= -4096` 或 `>= 0` 的返回值会被原样透传（不设 errno）。所以只有 `ret == -1` 时才需要去读 `errno`，其余情况直接用 `ret` 本身。这是 Linux 用户态/内核态错误码传递的经典约定。

---

### 4.2 dms_vdev：虚拟设备（SR-IOV）管理

#### 4.2.1 概念说明

`vdev`（Virtual Device）子模块管理 **SR-IOV 虚拟设备**。一张物理 NPU（PF）开启 SR-IOV 后可切分出多个 VF，每个 VF 拥有独立设备号与资源配额（算力核数、显存）。DMS 的 vdev 模块目前提供两类能力：

- **查询虚拟设备规格** `dms_get_vdevice_info`：给定 `dev_id` + `vf_id`，返回该虚拟实例的「总核数 / 已分配核数 / 显存大小」。
- **SR-IOV 开关** `dms_set_sriov_switch`：开启或关闭虚拟化能力（仅在编译开启 `CFG_FEATURE_SRIOV` 时存在）。

#### 4.2.2 核心流程

```text
dms_get_vdevice_info(dev_id, vf_id, &total_core, &core_count, &mem_size)
   ├─ 参数校验（三个出参非空）
   ├─ 填 ioarg：
   │     main_cmd = DMS_MAIN_CMD_BASIC
   │     sub_cmd  = DMS_GET_VDEVICE_INFO (=11)
   │     input    = { dev_id, vfid }
   │     output   = { total_core, core_num, mem_size }
   ├─ DmsIoctl(DMS_IOCTL_CMD, &ioarg)   → 内核 DEVDRV_MANAGER_GET_VDEVINFO
   └─ 把 out 字段写回调用者的出参，返回 DRV_ERROR_NONE
```

注意：vdev 走的是 **4.1 节的直接 `DmsIoctl`**（main_cmd/sub_cmd 放进 `ioarg`），而不是 4.4 节的「filter 字符串」式分发——因为它有自己专属的 sub_cmd（`DMS_GET_VDEVICE_INFO`）。

#### 4.2.3 源码精读

**查询虚拟设备信息**：

```c
int dms_get_vdevice_info(unsigned int dev_id, unsigned int vf_id,
    unsigned int *total_core, unsigned int *core_count, unsigned long *mem_size)
{
    struct dms_ioctl_arg ioarg = {0};
    struct dms_get_vdevice_info_in in = {0};
    struct dms_get_vdevice_info_out out = {0};
    ...
    in.dev_id = dev_id;
    in.vfid = vf_id;
    ioarg.main_cmd = DMS_MAIN_CMD_BASIC;
    ioarg.sub_cmd = DMS_GET_VDEVICE_INFO;     // = 11
    ioarg.input = (void *)&in;
    ioarg.output = (void *)&out;
    ret = DmsIoctl(DMS_IOCTL_CMD, &ioarg);
    ...
    *total_core = out.total_core;
    *core_count = out.core_num;
    *mem_size = out.mem_size;
    return DRV_ERROR_NONE;
}
```

见 [src/ascend_hal/dms/vdev/dms_vdev.c:20-52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/vdev/dms_vdev.c#L20-L52)。这是 DMS 模块最典型的「三段式」写法：**参数校验 → 填 ioarg 调 DmsIoctl → 回填出参**。

**SR-IOV 开关（特性门控）**：

```c
#ifdef CFG_FEATURE_SRIOV
drvError_t dms_set_sriov_switch(unsigned int dev_id, unsigned int sub_cmd, const void *buf, unsigned int buf_size)
{
    ...
    in.dev_id = dev_id;
    in.sriov_switch = *(const int *)buf;
    ioarg.main_cmd = DMS_MAIN_CMD_BASIC;
    ioarg.sub_cmd = DMS_SUBCMD_SRIOV_SWITCH;   // = 16
    ...
    ret = DmsIoctl(DMS_IOCTL_CMD, &ioarg);
    if (ret == EOPNOTSUPP) { return DRV_ERROR_NOT_SUPPORT; }
    ...
}
#endif
```

见 [src/ascend_hal/dms/vdev/dms_vdev.c:54-87](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/vdev/dms_vdev.c#L54-L87)。整个函数被 `#ifdef CFG_FEATURE_SRIOV` 包起来——在 `ascend910B` 的 feature cmake 里该宏是打开的（见 [src/ascend_hal/dms/feature/host/ascend910B.cmake:13](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/feature/host/ascend910B.cmake#L13)）。内核侧对应的命令字是 `DEVDRV_MANAGER_GET_VDEVINFO`（160），以及创建/销毁虚拟设备的 `DEVDRV_MANAGER_CREATE_VDEV`（158）/`DESTROY_VDEV`（159），见 [src/sdk_driver/dms/drv_devmng/inc/devmng_cmd_def.h:166-168](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/dms/drv_devmng/inc/devmng_cmd_def.h#L166-L168)。

> 子命令编号定义在用户态/内核态共享的 [src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h:25](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h#L25)（`DMS_GET_VDEVICE_INFO = 11`）与 [:30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h#L30)（`DMS_SUBCMD_SRIOV_SWITCH = 16`）。

#### 4.2.4 代码实践

**实践目标**：跟踪 vdev 查询接口从用户态到内核命令字的完整映射。

**操作步骤（源码阅读型）**：

1. 阅读 [src/ascend_hal/dms/vdev/dms_vdev.c:20-52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/vdev/dms_vdev.c#L20-L52)，确认 `main_cmd = DMS_MAIN_CMD_BASIC`、`sub_cmd = DMS_GET_VDEVICE_INFO`。
2. 在 [pbl_urd_sub_cmd_common.h:25](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h#L25) 找到 `DMS_GET_VDEVICE_INFO = 11`。
3. 在内核侧 [devmng_cmd_def.h:168](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/dms/drv_devmng/inc/devmng_cmd_def.h#L168) 找到 `DEVDRV_MANAGER_GET_VDEVINFO`（这是内核侧 vdev 查询的 ioctl 号）。

**需要观察的现象**：用户态的「main_cmd + sub_cmd」组合，与内核侧「DEVDRV_MANAGER_* 命令字」是**两套编号体系**——用户态用 URD 式的 `main_cmd/sub_cmd` 二维编号，内核侧 `drv_devmng` 模块再用一组独立的 `_IO` 命令字。DMS 底座负责把前者翻译送达后者。

**预期结果**：你能画出 `dms_get_vdevice_info` → `DmsIoctl(DMS_IOCTL_CMD)` → 内核 `DEVDRV_MANAGER_GET_VDEVINFO` 的映射关系。（运行结果待本地验证：需要真实 NPU + SR-IOV 环境才能实际调用成功。）

#### 4.2.5 小练习与答案

**练习 1**：`dms_set_sriov_switch` 里为什么要把内核返回的 `EOPNOTSUPP` 单独翻译成 `DRV_ERROR_NOT_SUPPORT`？

**参考答案**：`EOPNOTSUPP`（操作不支持）是 Linux errno，含义是「当前设备/固件不支持此操作」（比如某型号未启用虚拟化）。把它翻译成对外的 `DRV_ERROR_NOT_SUPPORT`，让上层调用者能用统一的 `drvError_t` 体系判断「是参数错还是能力不支持」，而不是去猜一个裸 errno。这也呼应了头文件里 `DMS_EX_NOTSUPPORT_ERR` 宏的设计——对「不支持」类错误降级日志级别，避免刷屏。

**练习 2**：vdev 模块为什么用「直接 `DmsIoctl` + 专属 sub_cmd」，而不用 4.4 节的「filter 字符串」式 `DmsGetDeviceInfo`？

**参考答案**：vdev 的输入输出是结构化的（`dms_get_vdevice_info_in/out`，含 dev_id/vfid/核数/显存等多字段），更适合用专门的 in/out 结构体直接传递；而 filter 字符串式分发是为「同形 get/set（一段 buffer + size）」的遗留接口设计的。专属 sub_cmd 让接口语义更清晰、类型更安全，是较新接口的推荐写法。

---

### 4.3 dms_flash：flash 存储信息读写

#### 4.3.1 概念说明

`flash` 子模块负责读写 NPU 板上的 **flash 存储信息**（如擦写次数、固件写保护状态等）。它是 DMS 里**最薄**的模块之一——总共只有两个函数、各几行代码，因为它把所有脏活累活都委托给了 4.4 节的通用分发层 `DmsGetDeviceInfo`/`DmsSetDeviceInfo`。

#### 4.3.2 核心流程

```text
dms_get_flash_info(dev_id, vfid, sub_cmd, buf, &size)
   └─ DmsGetDeviceInfo(dev_id, DMS_MAIN_CMD_FLASH, sub_cmd, buf, &size)
         （内部把 main_cmd+sub_cmd 拼成 filter 字符串，下发统一 ioctl）

dms_set_flash_info(dev_id, sub_cmd, buf, size)
   └─ DmsSetDeviceInfo(dev_id, DMS_MAIN_CMD_FLASH, sub_cmd, buf, size)
```

`DMS_MAIN_CMD_FLASH` 是一个「主命令类别」编号，与 [dsmi_common_interface.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/dsmi_common_interface.h) 里的 `DSMI_MAIN_CMD_FLASH` 取值一致（它被强制转换成 `DSMI_MAIN_CMD` 传入）。具体的 flash 子命令（擦写次数、写保护等）由调用者通过 `sub_cmd` 指定，定义在 [pbl_urd_sub_cmd_common.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h) 中（如 `DMS_SUBCMD_FW_WRITE_PROTECTION = 0x10`）。

#### 4.3.3 源码精读

```c
int dms_get_flash_info(unsigned int dev_id, unsigned int vfid, unsigned int sub_cmd, void *buf, unsigned int *size)
{
    (void)vfid;
    return DmsGetDeviceInfo(dev_id, (DSMI_MAIN_CMD)DMS_MAIN_CMD_FLASH, sub_cmd, buf, size);
}

int dms_set_flash_info(unsigned int dev_id, unsigned int sub_cmd, void *buf, unsigned int size)
{
    return DmsSetDeviceInfo(dev_id, (DSMI_MAIN_CMD)DMS_MAIN_CMD_FLASH, sub_cmd, buf, size);
}
```

见 [src/ascend_hal/dms/flash/dms_flash.c:15-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/flash/dms_flash.c#L15-L24)。注意两个细节：

1. `dms_get_flash_info` 接收 `vfid` 参数但**直接忽略**（`(void)vfid`）——说明当前 flash 信息是 PF 级别的，不区分虚拟设备。
2. `(DSMI_MAIN_CMD)DMS_MAIN_CMD_FLASH` 这个强制转换表明：`DMS_MAIN_CMD_FLASH` 与对外枚举 `DSMI_MAIN_CMD_FLASH` 共用同一套数值（参见 [pbl_urd_sub_cmd_common.h:236](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h#L236) 的注释 `/* DSMI_MAIN_CMD_LP */` 印证了这一对应关系）。

> 还有一个并行的 `src/ascend_hal/dms/chip/flash/dms_flash.c`，属于 `chip/` 子树（芯片子系统视角的 flash），与本讲的 `flash/`（板级存储视角）分工不同，不要混淆。

#### 4.3.4 代码实践

**实践目标**：理解 flash 模块作为「极薄包装」如何复用通用分发层。

**操作步骤（源码阅读型）**：

1. 阅读 [src/ascend_hal/dms/flash/dms_flash.c:15-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/flash/dms_flash.c#L15-L24)，确认它只是转调 `DmsGetDeviceInfo`/`DmsSetDeviceInfo`。
2. 跳到 4.4 节的 [dms_device_info.c:278-296](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L278-L296)，看 `DmsGetDeviceInfo` 如何把 `DMS_MAIN_CMD_FLASH` 拼进 filter 字符串。
3. 在 [pbl_urd_sub_cmd_common.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h) 中搜索 `DMS_SUBCMD_.*FLASH` 或 `WRITE_PROTECTION`，列举 flash 的几个子命令。

**需要观察的现象**：flash 模块自身不包含任何 ioctl 调用，全部委托给 `board/dms_device_info.c`。这是 DMS 里大量「按管理对象拆分、但共享分发原语」的模块的典型样态。

**预期结果**：你能解释「为什么 flash 模块只有两行实现却不该被删掉」——它承担的是**接口归类与命名语义**（让上层知道有专门的 flash 接口），并把 `DMS_MAIN_CMD_FLASH` 这一类别编号固化在调用点。（运行结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`dms_get_flash_info` 为什么忽略 `vfid`？这传递了什么设计信息？

**参考答案**：忽略 `vfid` 说明 flash 信息属于**板/物理设备级**资源（如整块 flash 的擦写次数、写保护状态），与虚拟设备切分无关——每个 VF 共享同一块物理 flash，不存在「每 VF 一份 flash 信息」。这是一种「接口签名保留 vfid 以保持与其他 get 接口一致，但语义上声明该资源不分虚拟设备」的妥协写法。

**练习 2**：如果要新增一个 flash 子命令（比如查询某个分区的剩余寿命），需要改哪些地方？

**参考答案**：(1) 在 [pbl_urd_sub_cmd_common.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/sdk_driver/pbl/dev_urd/command/ioctl/pbl_urd_sub_cmd_common.h) 新增一个 `DMS_SUBCMD_*` 子命令号；(2) 内核侧 `drv_devmng` 在 `DMS_MAIN_CMD_FLASH` 分支下增加对该 sub_cmd 的处理；(3) 上层（DSMI/DCMI）暴露一个新接口，转调 `dms_get_flash_info` 传入新 sub_cmd。flash 模块自身**无需改动**——这正是它做成分发层薄包装的好处。

---

### 4.4 通用 get/set device info 分发层（flash/lpm/board 的公共砖块）

#### 4.4.1 概念说明

flash、lpm、board 等模块有大量「读一段设备信息」「写一段设备信息」的同形操作。为避免每个模块各自拼 ioctl，DMS 在 `board/dms_device_info.c` 提供了一对**通用分发原语**：

- `DmsGetDeviceInfo(dev_id, main_cmd, sub_cmd, buf, &size)`
- `DmsSetDeviceInfo(dev_id, main_cmd, sub_cmd, buf, size)`

它们的巧妙之处在于：把 `main_cmd`（有时连带 `sub_cmd`）**序列化进一个 filter 字符串**（如 `"main_cmd=0x38"`），再用一个统一的 ioctl 号 `DMS_GET_GET_DEVICE_INFO_CMD` / `DMS_GET_SET_DEVICE_INFO_CMD` 下发。内核解析 filter 字符串即可还原出目标类别。

#### 4.4.2 核心流程

```text
DmsGetDeviceInfo(dev_id, DSMI_MAIN_CMD_FLASH, sub_cmd, buf, &size)
   ├─ 参数校验（buf/size 非空）
   ├─ DMS_MAKE_UP_FILTER_DEVICE_INFO(&filter, main_cmd)
   │     → filter = "main_cmd=0x38"   （sprintf_s 拼字符串）
   ├─ 填 in：{ dev_id, sub_cmd, buff=buf, buff_size=*size }
   ├─ 填 ioarg：
   │     main_cmd = DMS_GET_GET_DEVICE_INFO_CMD   （统一的「取设备信息」命令）
   │     sub_cmd  = ZERO_CMD
   │     filter   = "main_cmd=0x38"
   ├─ dms_get_ioctl → DmsIoctl(DMS_IOCTL_CMD, &ioarg) → 内核
   └─ *size = out.out_size（内核回填的实际长度）
```

`Ex` 后缀版本（`DmsGetDeviceInfoEx`/`DmsSetDeviceInfoEx`）则会把 `sub_cmd` 也拼进 filter（`"main_cmd=0x..,sub_cmd=0x.."`），用于需要同时按主/子命令精确定位的场景。

#### 4.4.3 源码精读

**filter 拼装宏**（在头文件里）：

```c
#define DMS_MAKE_UP_FILTER_DEVICE_INFO(f, main_cmd) do { \
    (f)->filter_len = (unsigned int)sprintf_s((f)->filter, sizeof((f)->filter), "main_cmd=0x%x", main_cmd); \
} while (0)

#define DMS_MAKE_UP_FILTER_DEVICE_INFO_EX(f, main_cmd, sub_cmd) do { \
    (f)->filter_len = (unsigned int)sprintf_s((f)->filter, sizeof((f)->filter), "main_cmd=0x%x,sub_cmd=0x%x", main_cmd, sub_cmd); \
} while (0)
```

见 [src/ascend_hal/dms/common/dms_user_common.h:43-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/common/dms_user_common.h#L43-L49)。

**通用 get 分发**：

```c
drvError_t DmsGetDeviceInfo(unsigned int dev_id, DSMI_MAIN_CMD main_cmd, unsigned int sub_cmd,
    void *buf, unsigned int *size)
{
    struct dms_filter_st filter = {0};
    struct dms_get_device_info_in in = {0};

    if ((buf == NULL) || (size == NULL) || (*size == 0)) {
        return DRV_ERROR_PARA_ERROR;
    }
    DMS_MAKE_UP_FILTER_DEVICE_INFO(&filter, main_cmd);   // 拼出 "main_cmd=0x.."

    in.dev_id = dev_id;
    in.sub_cmd = sub_cmd;
    in.buff = buf;
    in.buff_size = *size;

    return dms_get_ioctl(main_cmd, filter, in, size);
}
```

见 [src/ascend_hal/dms/board/dms_device_info.c:278-296](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L278-L296)。`dms_get_ioctl` 内部把 `main_cmd` 设为统一的 `DMS_GET_GET_DEVICE_INFO_CMD`、`sub_cmd` 设为 `ZERO_CMD`，并把 filter 字符串挂到 `ioarg.filter` 上，最终调 `DmsIoctl`，见 [src/ascend_hal/dms/board/dms_device_info.c:215-255](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L215-L255)。

**通用 set 分发**结构对称，见 [src/ascend_hal/dms/board/dms_device_info.c:196-213](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L196-L213)。

> 这个文件还承载了大量「按 main_cmd 类别枚举」的查询入口（`DmsGetDevBootStatus`、`DmsGetChipType`、`DmsGetDevProbeList`、`DmsGetAiCoreDieNum` 等），它们用的是另一种较新的写法——`urd_usr_cmd_fill` + `urd_usr_cmd`，即直接用 PBL 的 URD 接口而非 filter 字符串。可见 DMS 内部存在**新旧两套下发风格**，新接口倾向用结构化的 `urd_usr_cmd`。

#### 4.4.4 代码实践

**实践目标**：对比 DMS 内部新旧两套下发风格。

**操作步骤（源码阅读型）**：

1. 读 [src/ascend_hal/dms/board/dms_device_info.c:278-296](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L278-L296)（旧式 filter 字符串 `DmsGetDeviceInfo`）。
2. 读同文件 [src/ascend_hal/dms/board/dms_device_info.c:332-357](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L332-L357)（新式 `DmsGetChipType`，用 `urd_usr_cmd_fill` + `urd_usr_cmd`）。
3. 列表对比两者的差异：参数打包方式、是否用 filter 字符串、错误码处理。

**需要观察的现象**：新式 `urd_usr_cmd` 直接把 input/output 指针填进 `urd_cmd_para`，无需拼字符串；旧式则要 sprintf 拼出 `"main_cmd=0x.."`。

**预期结果**：你能说出「新接口为何更安全」——避免了 sprintf 拼字符串带来的格式约定耦合与潜在越界风险，类型也更明确。

#### 4.4.5 小练习与答案

**练习 1**：`DmsGetDeviceInfo` 与 `DmsGetDeviceInfoEx` 的核心区别是什么？什么场景该用 Ex 版本？

**参考答案**：区别在 filter 字符串：非 Ex 版只拼 `main_cmd`（`"main_cmd=0x.."`），Ex 版还拼 `sub_cmd`（`"main_cmd=0x..,sub_cmd=0x.."`）。当一个 `main_cmd` 类别下有多个子操作、且内核需要同时按主/子命令精确定位时（如温度查询下的 DDR/SOC 多档阈值），用 Ex 版本；若内核只按 main_cmd 大类分发、sub_cmd 已在 in 结构体里带了，则用非 Ex 版本。

**练习 2**：为什么 `dms_get_ioctl` 里对 `DSMI_MAIN_CMD_HCCS` 特别处理（不把 `OPER_NOT_PERMITTED` 改写成 `IOCRL_FAIL`）？

**参考答案**：见 [dms_device_info.c:245-248](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_device_info.c#L245-L248) 的注释「it is used to be compatible with the old errno」——HCCS（高速片间互联）相关的旧调用者依赖原始的 `OPER_NOT_PERMITTED` 错误码，改写会破坏向后兼容。这是一个典型的「为兼容历史行为而保留的特殊分支」。

---

### 4.5 dms_cc：板级机密计算（CC）模式管理

#### 4.5.1 概念说明

`board/dms_cc.c` 管理 **CC（Confidential Computing，机密计算）** 模式。CC 模式下，数据在芯片内以密文参与运算，需要在设备上配置「CC 模式开关」与「加密模式」。本模块提供两个接口：

- `dms_set_cc_info`：设置设备的 CC 模式（`cc_mode` + `crypto_mode`）。
- `dms_get_cc_info`：查询设备当前 CC 运行态与配置态。

它用 4.4 节提到的新式 `urd_usr_cmd` 下发，且**整体被 `CFG_FEATURE_CC_INFO` 编译宏门控**——只有支持机密计算的芯片（如 ascend950）才编译出真实实现，否则函数体直接返回 `DRV_ERROR_NOT_SUPPORT`。

#### 4.5.2 核心流程

```text
dms_set_cc_info(device_id, buf, buf_size)
   ├─ #ifdef CFG_FEATURE_CC_INFO  → 真实实现
   │     ├─ 参数校验（device_id 范围、buf 非空、buf_size == sizeof(dms_cc_mode)）
   │     ├─ memcpy_s 把 buf 拷进本地 mode 结构体
   │     ├─ urd_usr_cmd_fill(&cmd, DMS_MAIN_CMD_BASIC, DMS_SUBCMD_SET_CC_INFO, ...)
   │     ├─ urd_usr_cmd_para_fill(&cmd_para, &mode, sizeof(mode), NULL, 0)
   │     ├─ urd_dev_usr_cmd(device_id, &cmd, &cmd_para)  → 内核
   │     └─ 记 EVENT 日志，返回 DRV_ERROR_NONE
   └─ #else → 直接返回 DRV_ERROR_NOT_SUPPORT
```

#### 4.5.3 源码精读

**设置 CC 模式**：

```c
/* CC: confidential computing */
drvError_t dms_set_cc_info(unsigned int device_id, void *buf, unsigned int buf_size)
{
#ifdef CFG_FEATURE_CC_INFO
    ...
    if (device_id >= ASCEND_DEV_MAX_NUM || buf == NULL || buf_size != sizeof(struct dms_cc_mode)) {
        return DRV_ERROR_PARA_ERROR;       // 严格校验 buf_size 必须等于结构体大小
    }
    ret = memcpy_s(&mode, sizeof(struct dms_cc_mode), buf, buf_size);
    ...
    urd_usr_cmd_fill(&cmd, DMS_MAIN_CMD_BASIC, DMS_SUBCMD_SET_CC_INFO, NULL, 0);   // = 47
    urd_usr_cmd_para_fill(&cmd_para, (void *)&mode, sizeof(struct dms_cc_mode), NULL, 0);
    ret = urd_dev_usr_cmd(device_id, &cmd, &cmd_para);
    ...
    DMS_EVENT("Set cc mode success. (device_id=%u; cc_mode=%u; crypto_mode=%u)\n",
        device_id, mode.cc_mode, mode.crypto_mode);
    return DRV_ERROR_NONE;
#else
    ...
    return DRV_ERROR_NOT_SUPPORT;
#endif
}
```

见 [src/ascend_hal/dms/board/dms_cc.c:23-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_cc.c#L23-L60)。查询接口 `dms_get_cc_info` 结构对称，多一步把内核回填的 `cc_info` 拷回用户 buf，见 [src/ascend_hal/dms/board/dms_cc.c:62-106](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_cc.c#L62-L106)。

三个关键设计点：

1. **特性门控**：`#ifdef CFG_FEATURE_CC_INFO` 双分支。`ascend910B` 的 feature cmake（[ascend910B.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/feature/host/ascend910B.cmake)）里没有定义 `CFG_FEATURE_CC_INFO`，故 910B 上此函数恒返回 `NOT_SUPPORT`；950 才启用。这是 u3-l5「编译期宏冻结能力」的典型应用。
2. **新式 URD 下发**：用 `urd_usr_cmd_fill` + `urd_dev_usr_cmd`，比 flash 的 filter 字符串式更现代、类型更安全。
3. **CC 信息区分运行态/配置态**：`dms_get_cc_info` 返回的 `dms_cc_info` 含 `cc_running_info`（当前运行）与 `cc_cfg_info`（已配置）两份，因为 CC 模式切换通常需要重启才生效——运行态与配置态可能暂时不一致。

#### 4.5.4 代码实践

**实践目标**：体会「特性宏门控」如何让一份代码适配多种芯片能力。

**操作步骤（源码阅读型）**：

1. 读 [src/ascend_hal/dms/board/dms_cc.c:23-60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/board/dms_cc.c#L23-L60)，找到 `#ifdef CFG_FEATURE_CC_INFO` 与 `#else` 两个分支。
2. 对照 [ascend910B.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/feature/host/ascend910B.cmake)，确认 910B **未**定义 `CFG_FEATURE_CC_INFO`（即编译时走 `#else` 分支）。
3. 打开 [ascend950.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/feature/host/ascend950.cmake)，看 950 是否定义了该宏。

**需要观察的现象**：同一份 `dms_cc.c` 源码，在不同 `--soc` 下编译出的二进制行为不同——910B 上是「立即返回 NOT_SUPPORT 的桩」，950 上是「真实下发命令的实现」。

**预期结果**：你能解释「为什么上层调用者无需关心当前是不是 950」——因为无论哪种芯片，`dms_set_cc_info` 的签名与返回值约定都一致；不支持时统一返回 `DRV_ERROR_NOT_SUPPORT`，调用者据此降级即可。（运行结果待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：`dms_set_cc_info` 为什么要严格校验 `buf_size != sizeof(struct dms_cc_mode)` 就报参数错，而不是只检查 `buf_size >= sizeof`？

**参考答案**：CC 模式结构体是**定长**的固定布局（`cc_mode` + `crypto_mode` 等），内核侧按固定 offset 读取。若允许 `buf_size > sizeof`，可能是调用者传了结构体版本不匹配的缓冲区（字段错位），会导致内核误读。要求**严格相等**是最保守的防错策略，能尽早暴露版本不匹配问题。

**练习 2**：`dms_get_cc_info` 同时返回 `cc_running_info` 和 `cc_cfg_info`，这种「运行态/配置态分离」设计解决了什么问题？

**参考答案**：CC 模式切换通常需要**设备重启**才生效。因此存在一个过渡期：配置已改成新模式（`cc_cfg_info` 反映「下次生效的配置」），但当前仍按旧模式运行（`cc_running_info` 反映「当前实际状态」）。分开返回两个字段，让管理工具能准确告知用户「配置已更新，重启后生效」，避免用户误以为设置未生效而重复操作。

---

## 5. 综合实践

### 实践任务：横向浏览 dms 全部子目录，建立 DMS 管理能力全景图

本讲详细讲了 4 个最小模块（dms_user_common / dms_vdev / dms_flash / dms_cc），但 DMS 远不止于此。本实践要求你**横向浏览 `src/ascend_hal/dms/` 的全部子目录**，结合本讲建立的「ioctl 底座 + main_cmd/sub_cmd 分发 + 特性宏门控」认知，为每个子目录写一句管理职责说明，并最终回答「DMS 与 DMC 的分工差异」。

**操作步骤**：

1. 列出 `src/ascend_hal/dms/` 下全部子目录（参考 [CMakeLists.txt:51-73](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/CMakeLists.txt#L51-L73) 的 `add_subdirectory` 列表）。
2. 为下表「职责」列填空（已给出本讲涉及的 4 个作为示例，其余请阅读对应子目录的主源文件后填写）：

   | 子目录 | 代表源文件 | 管理职责（一句话） |
   | --- | --- | --- |
   | `common` | dms_user_common.c | DMS 公共 ioctl 底座：懒打开设备、DmsIoctl、errno 翻译、虚拟化门控 |
   | `vdev` | dms_vdev.c | SR-IOV 虚拟设备规格查询与开关 |
   | `flash` | dms_flash.c | 板上 flash 存储信息读写（擦写次数、写保护等） |
   | `board` | dms_cc.c / dms_device_info.c | 板级信息（机密计算 CC 模式）+ 通用 get/set device info 分发层 |
   | `lpm` | dms_lpm.c | *（请填写，提示：频率、电压/电流、温度、TOPS、MCU 透传）* |
   | `time` | dms_time_zone.c | *（请填写，提示：后台线程周期同步本地-UTC 时差）* |
   | `pcie` | dms_pcie_info.c | *（请填写）* |
   | `p2p` | dms_p2p_com.c | *（请填写）* |
   | `qos` | qos.c | *（请填写）* |
   | `power` | dms_power.c | *（请填写）* |
   | `hbm` | hbm_ctrl.c | *（请填写）* |
   | `emmc` | dms_emmc_info.c | *（请填写）* |
   | `ub` | dms_ub_info.c | *（请填写，提示：UB 灵衢超节点相关）* |
   | `fault` | dms_fault.c | *（请填写）* |
   | `chip/*` | soc/hccs/ts/sio/... | *（请填写，提示：芯片子系统信息）* |

3. **阅读两个参考模块**，验证你的填写：
   - 读 [src/ascend_hal/dms/lpm/dms_lpm.c:59-110](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/lpm/dms_lpm.c#L59-L110)（`DmsGetLpmInfo`），确认 lpm 用 `main_cmd=DMS_MAIN_CMD_LPM` 查频率，且 `dms_lpm_parameter_check` 做了 dev_id/vfid/core_id/sub_cmd 四重校验。
   - 读 [src/ascend_hal/dms/time/dms_time_zone.c:106-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dms/time/dms_time_zone.c#L106-L138)（`dmsStartTimeSyncServeDevice`），确认 time 模块用一条**独立后台线程**（`dms_time_sync`，栈 128KB）每 5 秒算一次本地-UTC 时差并经 `DMS_SET_TIME_ZONE_SYNC` 同步给设备。

4. **回答 DMS 与 DMC 的分工差异**（这是本讲的核心结论）。参考要点：
   - **DMS（管理系统）=「管理什么」**：vdev/flash/board/lpm/time/pcie/p2p/qos/power……都是具体的设备管理能力，面向 DSMI/DCMI 等上层接口。
   - **DMC（维护组件）=「怎么通信 + 维测工具」**：device_monitor 提供通用消息收发框架（`DM_CB_S`/`DM_INTF_S`，HDC/UDP/selfloop 管道），logdrv/prof 提供日志与性能采集工具。
   - **协作关系**：DMS 的管理动作要落到设备上，既可走 DMS 自己的同步 ioctl 通道（`/dev/davinci_manager` + `DMS_IOCTL_CMD`，本讲所讲），也可经 DMC 的 device_monitor 异步消息通路（u5-l1 所讲）往返设备固件——前者快但只能到本机内核，后者能到设备侧但更重。

**需要观察的现象**：

- 几乎所有 `dms/*/` 模块的主源文件都以 `#include "dms_user_common.h"` 开头，都通过 `DmsIoctl` 或 `DmsGetDeviceInfo`/`urd_dev_usr_cmd` 下发命令——**公共底座的高度复用**。
- 不同模块混用了新旧两套下发风格（filter 字符串 vs `urd_usr_cmd`），印证 DMS 是一个逐步演进的子系统。

**预期结果**：你产出一张完整的「DMS 子目录—职责」表，并能用自己的话向同事讲清「为什么 device_monitor（DMC）和 dms_vdev（DMS）不在同一个目录、各自解决什么问题」。（若无法在本机运行样例，请标注「待本地验证」，不要假装已运行。）

---

## 6. 本讲小结

- **DMS（设备管理系统）=「管理什么」**：`src/ascend_hal/dms/` 下二十多个子目录，每个承载一类设备管理能力（虚拟设备、flash、板级/CC、低功耗、时间、PCIe、P2P、QoS、电源……），编译进 `libascend_hal.so`。
- **公共 ioctl 底座** `dms_user_common.c`：懒打开 `/dev/davinci_manager`、单 ioctl `DMS_IOCTL_CMD` 按 `main_cmd/sub_cmd` 分发、双检锁 + fork 安全、errno 自动翻译、虚拟化环境门控——与 u3-l4 的 URD 同源同构。
- **两种下发风格并存**：旧式「filter 字符串」`DmsGetDeviceInfo`/`DmsSetDeviceInfo`（flash、部分 lpm 用），新式结构化 `urd_usr_cmd`（cc、chip info 等新接口用），后者类型更安全。
- **vdev**：SR-IOV 虚拟设备查询（核数/显存）与开关，特性宏 `CFG_FEATURE_SRIOV` 门控。
- **flash**：极薄包装，把 `DMS_MAIN_CMD_FLASH` 类别固化在调用点，委托通用分发层。
- **dms_cc**：机密计算模式 get/set，`CFG_FEATURE_CC_INFO` 门控，区分运行态与配置态。
- **DMS vs DMC**：DMS 提供具体管理动作（同步 ioctl 通道到本机内核），DMC 提供通信框架与维测工具（异步 HDC 消息到设备固件）；二者协作共同完成「主机管理设备」。

---

## 7. 下一步学习建议

- **横向深入某个 DMS 子模块**：建议接着读 `lpm`（低功耗/温控，体量大、同时用了新旧两套下发风格，是巩固本讲的好材料）或 `pcie`（PCIe 链路信息与热复位，与 u2-l4 的芯片复位呼应）。
- **纵向下沉到内核侧**：DMS 的用户态 ioctl 最终进入 `src/sdk_driver/dms/drv_devmng/`（内核态设备管理）。建议在学完单元 6（SDK-driver 内核层）后，回来对照 `devmng_cmd_def.h` 里的 `DEVDRV_MANAGER_*` 命令字，看用户态的 `main_cmd/sub_cmd` 如何映射到内核的命令处理函数。
- **回到 DCMI 视角**：DMS 的能力大多经 DSMI/DCMI 对外暴露（如 `dsmi_get_device_temperature` 最终调到 `DmsGetTemperature`）。可重读 u2-l1/u2-l2，从「DCMI → DSMI → DMS → 内核」串起完整调用链。
- **设备虚拟化专题**：若对 vdev 感兴趣，单元 7 的 u7-l5（vascend 算力切分与 vmng 虚拟化管理）会从更宏观的视角讲 SR-IOV 与虚拟机设备分配，与本讲的 vdev 模块互为补充。
