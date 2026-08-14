# 日志驱动 logdrv 与 msnpureport 日志导出

## 1. 本讲目标

本讲承接 [u5-l1 DMC 设备维护组件与 device_monitor 通路](u5-l1-dmc-device-monitor.md)，把视线从「消息收发框架」收拢到 DMC 中最贴近日常排障的一块：**日志**。读完本讲，你应当能够：

- 说清 driver 的「三层日志模型」——应用类日志、Host 内核态日志、Device 系统类日志——分别由谁产生、由谁读取。
- 理解 `drv_log_user` 这一用户态日志底座：它如何用一个「函数指针表」决定日志往哪里打，如何把 Linux 的 `errno` 翻译成对外的 `DRV_ERROR_*`。
- 掌握 `drv_share_log` 的「按通道读取」机制：`SHARE_LOG_ERR` 与 `SHARE_LOG_RUN_INFO` 两条通道如何用固定地址的共享内存把内核态日志搬到用户态。
- 读懂 `msnpureport` 工具的命令分发与导出链路，知道 `msnpureport -f` 这一行命令在源码里到底经过了哪些函数、最终经 HDC 把设备日志拉回 Host。

> ⚠️ 名词澄清：任务规格里提到的 `log_read_by_type` 在本仓库源码中**并不存在**（全文检索仅命中本讲义与历史转写文件）。本仓库真正实现「按通道读取」的接口是 `drv_share_log.c` 中的 `share_log_read_err` / `share_log_read_run_info` / `share_log_read`，通道类型由 `enum share_log_type_enum` 区分。本讲按真实代码讲解，不杜撰接口。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：日志分布在三个不同「世界」里。**昇腾驱动栈横跨 Host 用户态、Host 内核态、Device 侧三个执行环境，日志自然也分三处：

| 日志类别 | 产生位置 | 典型内容 | 查看方式 |
|---|---|---|---|
| 应用类日志 | Host 用户态（`libascend_hal.so` 等用户库） | 接口调用、参数校验、内存申请失败 | 系统日志 / slog |
| Host 内核态日志 | Host 内核态（`drv_*.ko` 内核模块） | 中断、ioctl 失败、PCIe 异常 | `dmesg` 或 `/var/log/messages` |
| 系统类日志 | Device 侧（NPU 固件/AI 核心） | 固件运行、栈信息、黑匣子 | `msnpureport` 导出 |

`docs/zh/QUICKSTART.md` 的「调试验证」一节明确给出了这个排查顺序：先看应用类日志，再用 `dmesg` 看内核态，最后用 `msnpureport` 导出 Device 日志（详见 [docs/zh/QUICKSTART.md:L143](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L143)）。

**直觉二：「日志通路」的核心矛盾是「跨执行环境搬运」。**应用类日志和产生它的代码在同一个进程里，直接 `printf`/`syslog` 即可；但内核态日志在内核地址空间，用户态读不到；Device 日志更是在另一块芯片上。因此 logdrv 要解决的本质问题是：**如何把后两类日志「搬」到用户能看到的地方**。本讲会看到两种搬法——内核态用「共享内存 + 按通道读」，Device 用「HDC 长连接拉文件」。

还需要了解的几个术语：`syslog` 是 Linux 标准日志接口（按 `LOG_ERR/LOG_INFO/LOG_DEBUG` 等优先级分级）；`ioctl` 是用户态陷入内核的系统调用；HDC 是 u5-l1 讲过的主机-设备通信底座。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [src/ascend_hal/dmc/logdrv/drv_log_user.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c) | 用户态日志初始化入口（constructor），读取默认控制台日志级别 |
| [src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c) | 日志底座实现：打印后端函数指针表、级别转换、`errno`→`DRV_ERROR_*` 映射 |
| [src/ascend_hal/dmc/logdrv/drv_log_user_common.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_common.c) | 对外公共 API 薄封装，把 `_inner` 后缀的实现函数转出为无后缀符号 |
| [src/ascend_hal/dmc/logdrv/drv_share_log.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c) | 内核态共享日志：固定地址 mmap + 环形缓冲 + 按通道读取 |
| [src/ascend_hal/inc/dmc/dmc_share_log.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h) | 共享日志的通道枚举与各模块固定地址宏定义 |
| [src/ascend_hal/inc/dmc/dmc_log_user.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h) | `DRV_ERR/DRV_WARN/DRV_RUN_INFO` 等日志宏定义 |
| [src/ascend_hal/msnpureport/msnpureport.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c) | `msnpureport` 命令的 main 入口 |
| [src/ascend_hal/msnpureport/options/msnpureport_options.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c) | 子命令（config/report）分发与选项解析 |
| [src/ascend_hal/msnpureport/config/msnpureport_config.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c) | `config` 子命令实现：经 HDC 短连接下发配置到设备 |
| [src/ascend_hal/msnpureport/report/msnpureport_report.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c) | `report` 子命令实现：经 HDC 拉取设备日志/黑匣子并落盘 |

## 4. 核心概念与源码讲解

### 4.1 logdrv 模块总览与三层日志模型

#### 4.1.1 概念说明

`logdrv`（日志驱动）是 DMC（Device Maintenance Components，u5-l1 讲过的设备维护组件集合）中的一个子模块，位于 [src/ascend_hal/dmc/logdrv/](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/CMakeLists.txt)。与 `device_monitor` 提供「通用消息收发框架」不同，logdrv 专门解决「日志的产生、搬运与导出」问题。

logdrv 目录下有四个 `.c` 文件，职责分明：

- `drv_log_user.c`——库加载时的初始化入口，极薄。
- `drv_log_user_kernel_api.c`——日志底座的真正实现（打印后端、级别转换、错误码映射），文件名带 `kernel_api` 是因为它处理「与内核交互得到的」错误码与级别，并非表示它运行在内核态；它仍是用户态代码。
- `drv_log_user_common.c`——对外公共封装层，注释里写明 `this file is a common.c`，把带 `_inner` 后缀的实现函数一一转出为不带后缀的对外符号。
- `drv_share_log.c`——内核态共享日志的按通道读取实现。

#### 4.1.2 核心流程

把 logdrv 放回三层日志模型里，它的定位是「**同时服务于应用类日志与 Host 内核态日志**」，而 Device 系统类日志则由同在 `ascend_hal` 树下的独立工具 `msnpureport` 负责：

```
┌──────────────── Host 用户态 (libascend_hal.so) ────────────────┐
│  各模块调用 DRV_ERR/DRV_INFO/... 宏                             │
│        │ (函数指针表 g_log_print_info)                          │
│        ▼                                                       │
│  drv_log_user 打印底座 ──→ syslog / 上层注册的 DlogInner        │
│        ▲                                                       │
│        │ share_log_read_*() 读取并重新打出                      │
│  drv_share_log (固定地址 mmap 的环形缓冲)                       │
└────────────────────────│──────────────────────────────────────┘
                         │ 共享内存 (同址映射)
┌────────────────────────▼──────────────────────────────────────┐
│              Host 内核态 (drv_*.ko)                            │
│  内核模块把日志写入共享内存环形缓冲                              │
└───────────────────────────────────────────────────────────────┘

┌──────── Device 侧 (NPU) ────────┐     ┌──── Host 用户态工具 ────┐
│  slog / message / system_info    │ ◀─▶ │  msnpureport            │
│  stackcore / bbox / event_sched  │ HDC │  (report/config 子命令) │
└──────────────────────────────────┘     └─────────────────────────┘
```

要点：应用类日志是「自产自销」，内核态日志是「内核写、用户读」的共享内存协作，Device 日志是「按需经 HDC 拉取」。下面三节分别拆解。

### 4.2 drv_log_user：用户态日志打印底座

#### 4.2.1 概念说明

整个 HAL 用户库（以及复用它的 DCMI/DSMI）打印日志时，并不会直接调 `printf` 或 `syslog`，而是统一调用 `DRV_ERR`、`DRV_WARN`、`DRV_INFO`、`DRV_RUN_INFO` 等宏。这些宏最终都汇聚到一个「打印后端」——一个由函数指针组成的结构体 `g_log_print_info`。这种设计的好处是：**日志往哪里打（syslog？上层 slog？）可以运行时替换，而调用点的代码完全不用改**。

#### 4.2.2 核心流程

1. **库加载即初始化**：`libascend_hal.so` 被载入时，GCC `constructor` 属性的 `drv_log_init` 自动执行，向内核管理模块查询默认控制台日志级别并设置。
2. **打印后端就位**：全局 `g_log_print_info` 装配好默认实现（级别字符串、时间格式、`vsyslog` 打印函数）。
3. **上层可接管**：若上层（如 CANN 的 slog 系统）想接管日志输出，调 `drv_log_out_handle_register` 注册自己的 `DlogInner` 回调，函数指针表被整体替换。
4. **调用点打日志**：业务代码调 `DRV_ERR(module, fmt, ...)`，宏经函数指针表落到当前后端。

#### 4.2.3 源码精读

**初始化入口**——`drv_log_user.c` 中的 constructor，库一加载就执行，向 devdrv_manage 查询默认级别并写入全局变量：

[src/ascend_hal/dmc/logdrv/drv_log_user.c:L14-L24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c#L14-L24)

> 说明：`__attribute__((constructor))` 让 `drv_log_init` 在 `main` 之前运行；`drvMngGetConsoleLogLevel` 取回系统配置的默认级别（默认 `LOG_ERR`），再由 `drv_log_rsyslog_console_level_set` 写入全局 `drv_log_rsyslog_console_level`，作为后续日志过滤的基准。

**打印后端函数指针表**——这是整个底座的中枢，四个函数指针分别负责「级别字符串、时间串、级别数值转换、真正打印」：

[src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c:L353-L359](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L353-L359)

> 说明：默认 `log_print` 指向 `drv_syslog`（即 `vsyslog` 封装），`con_log_level` 指向 `drv_log_rsyslog_console_level`。所有 `DRV_*` 宏都经 `get_log_Print()` 等取值函数间接调用这里。

**上层注册接管**——`drv_log_out_handle_register_inner` 把整个函数指针表替换为上层传入的实现，同时完成「工具日志级别 → glibc syslog 级别」的换算：

[src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c:L363-L395](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L363-L395)

> 说明：这是「策略可替换」的关键——上层只需传入一个 `log_out_handle`（含 `DlogInner` 回调与 `logLevel`），驱动就把所有日志改走该回调，并记住 `g_run_log_status` 供 `is_run_log()` 判断当前是否处于「运行日志」模式。

**两套级别体系的换算**——glibc 的 `LOG_ERR/LOG_INFO/...` 与工具侧的 `DLOG_ERROR/DLOG_INFO/...` 是两套词汇表，靠两张表互转：

[src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c:L315-L324](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L315-L324)

> 说明：例如 `LOG_WARNING` 对应 `DLOG_WARN`。注册接管后 `log_level_shift` 被设为 `drv_log_level_glibc_to_tool`，保证无论上层用哪套词汇，最终落到的优先级一致。

**`DRV_*` 宏如何走到后端**——在 `dmc_log_user.h` 里，宏展开后调用 `get_log_Print()` 取出当前后端函数并执行，同时拼好级别串、时间串、模块名、文件名行号：

[src/ascend_hal/inc/dmc/dmc_log_user.h:L50-L66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h#L50-L66)

> 说明：`DRV_SYSLOG_BASE` 是基础宏，先比较 `LEVEL <= actual_print_level` 决定是否打印（级别过滤就在这里），再调用 `(*get_log_Print())(...)`。`DRV_ERR`/`DRV_WARN`/`DRV_INFO` 只是固定 LEVEL 的快捷方式（见 L73-L75）。值得注意的是，对 `DMP` 和 `DEV_MANAGER` 两个模块，宏还会额外调一次 `dsmi_printf` 做镜像输出，方便管理工具观测。

**错误码映射**——ioctl 失败时内核返回的是 Linux `errno`（如 `EINVAL=22`、`ENOMEM=12`），但驱动对外要返回统一的 `drvError_t`（如 `DRV_ERROR_PARA_ERROR`、`DRV_ERROR_OUT_OF_MEMORY`）。`errno_to_user_errno_inner` 用一张以 `errno` 为下标的查表完成多对一收敛：

[src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c:L193-L221](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L193-L221)

> 说明：`user_err[]` 是一张覆盖到 `EHWPOISON+1` 的大表（定义在 L48-L191），每个 `errno` 下标映射到一个 `DRV_ERROR_*`；绝大多数 `errno` 都收敛为 `DRV_ERROR_IOCRL_FAIL`（ioctl 失败），少数有专属码（`ENOMEM→DRV_ERROR_OUT_OF_MEMORY`、`EBUSY→DRV_ERROR_BUSY`、`ETIMEDOUT→DRV_ERROR_WAIT_TIMEOUT`）。超出表范围的三个驱动私有码（150/151/152）单独处理。

#### 4.2.4 代码实践

**实践目标**：验证「打印后端可运行时替换」这一设计，并理解错误码映射的收敛行为。

**操作步骤**（源码阅读型，无需真实 NPU）：

1. 打开 `drv_log_user_kernel_api.c`，对比 L353-L359（默认 `g_log_print_info`）与 L363-L395（`drv_log_out_handle_register_inner` 替换后的赋值），圈出四个被替换的字段。
2. 阅读 L48-L191 的 `user_err[]` 表，统计：有多少个不同的 `errno` 被映射成 `DRV_ERROR_IOCRL_FAIL`？哪些 `errno` 拥有「专属」错误码？
3. 在 `dmc_log_user.h` 的 L73-L99 中，对比 `DRV_ERR`（普通错误日志）与 `DRV_RUN_INFO`（运行日志）展开路径的差异——后者经 `DRV_LOG_CMPT` 多了一层 `is_run_log()` 判断。

**需要观察的现象**：

- 替换前后，`con_log_level` 指向的全局变量从 `drv_log_rsyslog_console_level` 切到 `drv_log_tool_console_level`。
- `user_err[]` 中绝大多数条目都是 `DRV_ERROR_IOCRL_FAIL`，说明对外错误码做了大幅「多对一」收敛。

**预期结果**：能口述出「调用点用统一宏 → 函数指针表分发 → 后端可替换」三段式，并能解释为何一次 `ioctl` 返回 `EINVAL` 时，上层拿到的是 `DRV_ERROR_PARA_ERROR`。

> 待本地验证：在真实环境用 `strace` 跟踪一个会失败的 DCMI 调用，对照 `dmesg`/syslog 中驱动打出的 `[ERROR][...][drv][common]...` 日志，确认级别串与时间串由本节这些函数生成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DRV_ERR` 宏里要同时调用 `get_log_get_level_string()` 和 `get_log_get_print_time()`，而不是直接写死 `"[ERROR]"` 和调用 `localtime()`？
**答案**：因为打印后端是可替换的——注册接管后，级别字符串与时间串的生成方式也会随之改变（见 `drv_log_out_handle_register_inner` 把 `log_get_level_string` 换成 `drv_log_get_level_str`、`log_get_print_time` 换成 `drv_get_tm`）。直接写死就丧失了这种灵活性。

**练习 2**：内核返回 `errno=12 (ENOMEM)` 和 `errno=22 (EINVAL)` 时，上层分别会得到哪个 `drvError_t`？
**答案**：查 `user_err[]`：`ENOMEM→DRV_ERROR_OUT_OF_MEMORY`，`EINVAL→DRV_ERROR_PARA_ERROR`。

### 4.3 drv_share_log：Host 内核态日志的「按通道读取」

#### 4.3.1 概念说明

应用类日志自己打自己读，但 Host 内核态日志产生在内核里，用户态进程看不到。`drv_share_log` 解决的就是这个搬运问题：**在用户态和内核态之间，用一块预先约定好地址的共享内存当「信箱」**。内核模块把日志写进信箱，用户态库定期把信箱里的内容读出来、重新经 4.2 节的打印底座打一遍，于是内核日志就出现在了 syslog 里。

「按通道读取」里的「通道」（channel），在代码里就是 `enum share_log_type_enum`——区分**错误日志**与**运行信息日志**两条独立通道；再加上「按模块」维度（DEVMM、HDC、TSDRV 等），构成一张二维表。

#### 4.3.2 核心流程

1. **建信箱**：`share_log_create` 在固定地址（如 `DEVMM_SHARE_LOG_START`）`mmap` 一段匿名内存，写入魔数 `drvshartlogab90cd78ef56`，初始化环形缓冲的 `read`/`write` 游标。
2. **内核写**：内核模块把日志字节追加到 `record_base + write`，推进 `write` 游标。（内核侧写入逻辑在 sdk_driver 树，本讲聚焦用户侧读取。）
3. **用户读**：`share_log_read_in_single_module` 校验魔数后，读取 `[read, write)` 区间，把其中的 `\n` 替换成空格（避免一条日志被 syslog 拆成多行），然后按通道用 `DRV_ERR`（错误通道）或 `DRV_RUN_INFO`（运行信息通道）打出，最后把 `read` 推进到 `write`。
4. **按通道入口**：`share_log_read_err` 只读 `SHARE_LOG_ERR` 通道，`share_log_read_run_info` 只读 `SHARE_LOG_RUN_INFO` 通道，`share_log_read` 等同于 `share_log_read_err`。

#### 4.3.3 源码精读

**通道枚举与固定地址**——`dmc_share_log.h` 定义了两条通道和每个模块×通道的固定起始地址（注意 ERR 与 RUN_INFO 各占一段）：

[src/ascend_hal/inc/dmc/dmc_share_log.h:L17-L43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h#L17-L43)

> 说明：`SHARE_LOG_ERR=0`、`SHARE_LOG_RUN_INFO=1` 就是两条「通道」；`DEVMM_SHARE_LOG_START=0xE0000080000` 这类地址是用户态与内核态的约定坐标，双方都把同一物理页映射到这个虚拟地址，从而共享。`SHARE_LOG_MAX_SIZE=4KB` 是单个信箱上限。

**模块×通道二维表**——`drv_share_log.c` 用一张二维表管理所有信箱，行是模块、列是通道，每个单元格存「起始地址 + 是否已初始化」：

[src/ascend_hal/dmc/logdrv/drv_share_log.c:L48-L67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L48-L67)

> 说明：例如 `g_module_mng[HAL_MODULE_TYPE_DEVMM][SHARE_LOG_ERR]` 的地址是 `DEVMM_SHARE_LOG_START`，而 `[..., SHARE_LOG_RUN_INFO]` 的地址是 `DEVMM_SHARE_LOG_RUNINFO_START`。`init_flag` 防止重复初始化。

**创建信箱**——`share_log_create_single_type` 在固定地址做 `mmap`，校验返回地址必须等于请求地址（否则视为失败），写入魔数与环形缓冲元数据：

[src/ascend_hal/dmc/logdrv/drv_share_log.c:L71-L117](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L71-L117)

> 说明：`record_base` 指向跳过 100 字节头部的数据区（`SHARE_LOG_RECORD_OFFSET=100`），`record_size = size - 100`。`read`/`write` 两个游标构成一个简易环形缓冲。构造函数 `drv_log_base_init` 会在库加载时为 `HAL_MODULE_TYPE_COMMON` 调一次 `share_log_create`（见同文件 L415-L418）。

**按通道读取（核心）**——`share_log_read_in_single_module` 做了完整的「校验→拷贝→换行处理→按通道打出→推进游标」流程：

[src/ascend_hal/dmc/logdrv/drv_share_log.c:L167-L225](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L167-L225)

> 说明：这是一段「拷贝出数据后逐字节把 `\n` 改成空格」的处理（L206-L210），目的是让一条内核日志在 syslog 里只占一行；随后按 `log_type` 分流——`SHARE_LOG_ERR` 走 `DRV_ERR`，`SHARE_LOG_RUN_INFO` 走 `DRV_RUN_INFO`（L212-L216），这正是「按通道读取」决定日志级别的体现。读完后 `info->read = tmp_write` 标记已消费。

**三个对外入口**——按通道类型提供不同入口，内部都落到同一个读取函数：

[src/ascend_hal/dmc/logdrv/drv_share_log.c:L227-L243](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L227-L243)

> 说明：`share_log_read_err` 固定读 `SHARE_LOG_ERR` 通道，`share_log_read_run_info` 固定读 `SHARE_LOG_RUN_INFO` 通道；两者都会额外读一遍 `HAL_MODULE_TYPE_COMMON` 的公共信箱。

#### 4.3.4 代码实践

**实践目标**：说清 `share_log_read_*` 如何「按通道」读取内核态日志，并区分它与 Device 日志导出的不同。

**操作步骤**（源码阅读型）：

1. 在 `drv_share_log.c` 中跟踪 `share_log_read_run_info(HAL_MODULE_TYPE_DEVMM)` 的执行：它访问 `g_module_mng[DEVMM][SHARE_LOG_RUN_INFO]`，地址是 `DEVMM_SHARE_LOG_RUNINFO_START`。
2. 对比 `share_log_read_in_single_module` 中 `DRV_ERR` 与 `DRV_RUN_INFO` 两条分支（L212-L216），解释「通道类型」如何决定最终日志级别。
3. 结合 4.1 节的三层模型图，回答：这块共享内存搬运的是「Host 内核态日志」还是「Device 日志」？（答：Host 内核态。）

**需要观察的现象**：当 Host 内核驱动（如 SVM/devmm 模块）产生运行信息时，这些信息先落进 `DEVMM_SHARE_LOG_RUNINFO_START` 信箱，随后被用户库读出并以 `DRV_RUN_INFO` 形式出现在 syslog 中。

**预期结果**：能画出「内核写 `write` 游标 → 用户读 `[read,write)` → 推进 `read`」的环形缓冲消费模型，并指出通道枚举 `SHARE_LOG_ERR/SHARE_LOG_RUN_INFO` 就是「按类型读取」的类型参数。

> 待本地验证：在真实环境运行一个会触发 devmm 运行日志的用例，对比 `dmesg`（内核直接输出）与 syslog 中经 `share_log_read` 转发后的 `[INFO]` 行，确认两者内容一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `share_log_read_in_single_module` 要把读取到的内容里的 `\n` 替换成空格？
**答案**：因为读出的内容会被 `DRV_ERR`/`DRV_RUN_INFO` 再打一次到 syslog，而 syslog 一条记录通常对应一行；若内核日志内含 `\n`，会被 syslog 拆成多条，破坏「一条日志一行」的可读性，所以预先把 `\n` 改成空格。

**练习 2**：`share_log_create_single_type` 里为什么 `mmap` 返回地址必须严格等于请求的固定地址（L88 的判断），否则就视为失败？
**答案**：因为这块内存的虚拟地址是用户态与内核态**事先约定**的（如 `0xE0000080000`），双方都要用这个地址访问同一物理页。如果内核给了别的虚拟地址，双方就「对不上号」，共享失败，所以必须严格匹配。

### 4.4 msnpureport：Device 系统类日志导出与配置

#### 4.4.1 概念说明

`msnpureport` 是随驱动安装的命令行工具（安装后位于 `/usr/local/Ascend/driver/tools/msnpureport`），专门负责 Device 侧（NPU）的系统类日志：包括设备 `slog`、`message`、`system_info`、`event_sched`、`stackcore`（栈转储）、`bbox`（黑匣子）等。它有两大子命令：

- `report`：把设备日志**导出**到 Host 的一个时间戳目录里（一次性或持续）。
- `config`：**查询或设置**设备的配置项与日志级别（如 icache 检查范围、AI 核开关、全局/模块日志级别）。

它的底层通道就是 u5-l1 讲过的 HDC——配置走 HDC「短连接」，日志文件拉取走 HDC「长连接」。

#### 4.4.2 核心流程

`msnpureport` 的整体执行链路：

```
main (msnpureport.c)
  └─ MsnOptions (options/msnpureport_options.c)        ← 解析子命令
       ├─ argv[0]=="config" → MsnGetConfigOptions → MsnConfig (config/msnpureport_config.c)
       │      └─ MsnGetResult → AdxDevCommShortLink(HDC_SERVICE_TYPE_IDE_FILE_TRANS)  ← 短连接下发
       └─ argv[0]=="report" → MsnGetReportOptions → MsnReport (report/msnpureport_report.c)
              ├─ 一次性: SyncDeviceLog → CreateLogRootPath(时间戳目录) → GetHostDrvLog(导出Host内核日志)
              │            → MsnSyncDeviceLog → SlogStartSyncFile(多线程) + BboxStartSyncFile(黑匣子)
              │            → GetSpecificLogs → AdxGetDeviceFileTimeout (HDC 拉文件)
              └─ 持续:   MsnReportPermanent → MsnReportRecvSlogd + MsnReportRecvLogDaemon(长连接线程)
```

要点：`report` 一次性导出会先建一个以当前时间戳命名的目录（如 `2026-08-13-10-30-00/`），再分门别类把设备文件拉进来；同时它还会顺带导出 Host 内核日志（`host_kernel.log`）。持续模式（`--permanent`）则常驻两个收发线程，并订阅设备故障事件。

#### 4.4.3 源码精读

**main 入口**——极简，直接转交选项处理：

[src/ascend_hal/msnpureport/msnpureport.c:L18-L21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c#L18-L21)

> 说明：`MAIN` 宏在 UT 编译时被替换为 `MsnTest`，正常编译时就是 `main`。

**子命令分发**——`MsnOptions` 先按 `argv` 第一个非选项参数分流到 config/report/help/version 四条路径，再做 docker 环境校验，最后交给 `MsnHandleArgInfo` 执行：

[src/ascend_hal/msnpureport/options/msnpureport_options.c:L1028-L1079](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1028-L1079)

> 说明：注意 L1044 有一段「老命令」兼容——当参数过短或以 `-` 开头时走 `MsnOptionsOld`（兼容旧版无子命令的写法）；否则走新式的 `MsnOptionsHandle`（L1055，按子命令 dispatch）。L1070-L1078 设置本工具自身的打印模式与日志级别。

**子命令到执行的桥接**——`MsnHandleArgInfo` 根据 `cmdType` 调 `MsnConfig` 或 `MsnReport`：

[src/ascend_hal/msnpureport/options/msnpureport_options.c:L1002-L1026](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1002-L1026)

> 说明：`CONFIG_GET/CONFIG_SET` 走 `MsnConfig`；`REPORT/REPORT_PERMANENT` 走 `MsnReport`。失败时统一提示「check syslog for more information」——因为工具自身的诊断日志默认打到 syslog。

**report 子命令支持的选项**——`g_reportOptions` 用结构体数组描述所有长选项，`-f/--force` 就是其中之一：

[src/ascend_hal/msnpureport/options/msnpureport_options.c:L101-L126](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L101-L126)

> 说明：`-a/--all` 导出全部日志与 bbox 事件；`-f/--force` 在 `-a` 基础上额外导出历史维测信息；`-t/--type` 按类型导出（0 全部、1 slog/message/system_info、2 bbox、3 stackcore、4 vmcore、5 module log，UB 环境还有 6 ub）；`--permanent` 进入持续导出模式。

**config 子命令经 HDC 短连接下发**——`MsnGetResult` 把参数打包成 `TlvReq`/`MsnReq`，经 `AdxDevCommShortLink` 走 HDC 短连接送到设备，超时 120 秒：

[src/ascend_hal/msnpureport/config/msnpureport_config.c:L20-L52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L20-L52)

> 说明：`req->type = COMPONENT_MSNPUREPORT` 标识这是 msnpureport 报文；`HDC_SERVICE_TYPE_IDE_FILE_TRANS` 是业务频道号（与 u5-l1 讲过的 HDC serviceType 概念一致）。`MsnConfig`（L62-L102）在 `CONFIG_SET` 时会先校验 root 权限（`IsHaveRootPermission`，L54-L61），这与帮助信息里「Only root user is allowed to execute this command with '--set'」对应。

**report 一次性导出主链路**——`MsnReport` 区分持续与一次性，一次性走 `SyncDeviceLog`：

[src/ascend_hal/msnpureport/report/msnpureport_report.c:L1319-L1335](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1319-L1335)

> 说明：根据 `subCmd`/`reportType` 装配 `BboxDumpOpt`（是否导出全部、是否强制、是否 vmcore），然后调 `SyncDeviceLog`。

**建时间戳目录 + 顺带导出 Host 内核日志**——`SyncDeviceLog` 先 `CreateLogRootPath` 建目录，若是 `ALL_LOG` 还会调 `GetHostDrvLog` 把 Host 内核日志也导出一份：

[src/ascend_hal/msnpureport/report/msnpureport_report.c:L842-L864](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L842-L864)

> 说明：执行前会校验 root 权限（`IsHaveExecPermission`，L844）。注意 L854 `ALL_LOG` 时调的 `GetHostDrvLog`——它把 4.3 节的 Host 内核态日志以文件形式落盘，这就是 QUICKSTART 里 `msnpureport -f` 生成 `./时间戳/slog/host/host_kernel.log` 的源码出处。

**Host 内核日志落盘**——`GetHostDrvLog` 调 HAL 公共接口 `halGetDeviceInfoByBuff`，以 `MODULE_TYPE_LOG`+`INFO_TYPE_HOST_KERN_LOG` 为参数，把内核日志写到 `<logPath>/slog/host`：

[src/ascend_hal/msnpureport/report/msnpureport_report.c:L475-L499](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L475-L499)

> 说明：`INFO_TYPE_HOST_KERN_LOG` 在 [pkg_inc/ascend_hal_base.h:L401](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L401) 定义（值 35），`halGetDeviceInfoByBuff` 声明在同文件 [L1353](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L1353)。若环境不支持，返回 `DRV_ERROR_NOT_SUPPORT`，工具只打一条 warn（L491-L492）。

**按类型拉取设备文件**——`GetSpecificLogs` 经 HDC 把设备上的某类文件拉到本地指定路径：

[src/ascend_hal/msnpureport/report/msnpureport_report.c:L158-L206](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L158-L206)

> 说明：`AdxGetDeviceFileTimeout` / `AdxGetSpecifiedFile` 是对 HDC 文件传输的封装，业务频道用 `HDC_SERVICE_TYPE_LOG`（或新通道 `HDC_SERVICE_TYPE_PROFILING`，由 `HDC_NEW_CHANNEL` 宏控制，见 L186-L192）。`BLOCK_RETURN_CODE=4` 表示设备发现客户端在 docker 内而拒绝（L197-L198）。

#### 4.4.4 代码实践

**实践目标**：追踪 `msnpureport -f` 这一行命令的完整配置链路，并能解释它最终生成的目录结构。

**操作步骤**：

1. 从 [msnpureport.c:L18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c#L18) 出发，依次跟入 `MsnOptions`（options 文件 L1028）→ 因 `argv[0]="report"` 进入 `MsnGetReportOptions` → `-f` 被解析为 `REPORT_ARGS_REPORT_FORCE`（options 文件 L92/L105）→ `MsnReportLogCmd` 把 `subCmd` 设为 `REPORT_FORCE`（L790-L793）。
2. 跟入 `MsnHandleArgInfo`（L1002）→ `MsnReport`（report 文件 L1319）→ `SyncDeviceLog`（L842）→ `CreateLogRootPath`（L753，生成时间戳目录）→ `GetHostDrvLog`（L475，导出 host_kernel.log）→ `MsnSyncDeviceLog`（L682）→ `SlogStartSyncFile`（L383，拉 slog/stackcore/message/system_info/event_sched）+ `BboxStartSyncFile`（L451，拉黑匣子）。
3. 对照 `docs/zh/QUICKSTART.md` 的 [L164-L167](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L164-L167)：官方说明执行后生成「以时间戳命名的目录」，Host 内核日志位于 `./时间戳/slog/host/host_kernel.log`——这与源码中 `GetHostDrvLog` 拼出的 `%s/%s/host`（即 `<logPath>/slog/host`）完全对应。

**需要观察的现象**（在有 NPU 的 root 环境运行时）：

```
# /usr/local/Ascend/driver/tools/msnpureport -f
Start exporting logs and files to path: /root/2026-08-13-10-30-00
Export finished.
```

随后在该目录下应能看到 `slog/host/host_kernel.log`（Host 内核日志）、`slog/dev-os-*`（设备 slog）、`system_info/`、`stackcore/`、bbox 等子目录。

**预期结果**：能画出从命令行到 HDC 文件拉取的完整调用链，并指出 `-f` 比 `-a` 多导出了「历史维测信息」（`bboxDumpOpt.force = true`）。若运行环境不支持某类导出（如非 UB 环境的 type 6），相应分支返回 `DRV_ERROR_NOT_SUPPORT` 并打 warn，不影响其他类型。

> 待本地验证：实际目录内容与设备型号、是否 UB 形态有关，本讲无法在无设备环境给出确切的文件清单，请在真实 NPU 上确认。

#### 4.4.5 小练习与答案

**练习 1**：`msnpureport config --set` 与 `msnpureport report` 分别走哪种 HDC 连接？为什么？
**答案**：`config --set/--get` 走 HDC **短连接**（`MsnGetResult` 中的 `AdxDevCommShortLink`，超时 120s），因为它只是一次「下发请求-取回结果」的交互；`report` 拉取日志文件走 HDC **长连接**（`MsnReportCreateLongLink` / `MsnReportServerLongLink`），因为要持续接收大量数据，且持续模式需要常驻收发线程。

**练习 2**：为什么 `msnpureport report -f` 导出的结果里会包含一个 `host_kernel.log`？它和 Device 日志是同一来源吗？
**答案**：因为 `SyncDeviceLog` 在 `ALL_LOG` 时会调 `GetHostDrvLog`，经 `halGetDeviceInfoByBuff(... INFO_TYPE_HOST_KERN_LOG ...)` 把 **Host 内核态** 日志也落盘，方便一次性收齐三层日志。它与 Device 日志不是同一来源——前者来自 Host 内核（即 4.3 节共享内存搬运的那批），后者来自 NPU 设备。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「全链路日志追踪」。

**场景**：某上层应用调用 DCMI 接口失败，返回了一个 `DRV_ERROR_*` 错误码。请按以下步骤定位：

1. **应用类日志层**：在 syslog 中找到驱动打出的 `[ERROR]...[drv][common]...` 行。结合 4.2 节，说明这行日志是经 `g_log_print_info` 的哪个函数指针打出的（默认是 `drv_syslog`）。
2. **错误码溯源**：根据应用拿到的 `DRV_ERROR_*`，反查 `drv_log_user_kernel_api.c` 的 `user_err[]` 表（L48-L191），推断底层 `ioctl` 当时可能返回了哪个 Linux `errno`。例如得到 `DRV_ERROR_OUT_OF_MEMORY` → 推断 `errno=ENOMEM`。
3. **Host 内核态层**：用 `dmesg` 或 `/var/log/messages` 查看同时间点的内核日志；再执行 `msnpureport -f`，打开生成的 `./时间戳/slog/host/host_kernel.log`，对照 4.3 节说明这些内核日志原本是经共享内存（`SHARE_LOG_ERR` 通道）搬到用户态的。
4. **Device 系统类层**：在同一时间戳目录的 `slog/dev-os-*/` 与 `system_info/` 下查看 Device 侧日志，结合 4.4 节说明这些文件是经 HDC 长连接（`AdxGetDeviceFileTimeout`）从设备拉回的。

**交付物**：一份调用链图，标注「应用拿到的错误码 ← errno 映射 ← ioctl 失败 ← 内核日志（共享内存搬运）← msnpureport 导出」，并指出每一环对应的源码文件与关键函数。

> 待本地验证：本实践需要真实 NPU 与可复现的失败用例；若无设备，可降级为纯源码阅读——只完成步骤 2 的查表与步骤 1/3/4 的源码定位，写出预期日志路径即可。

## 6. 本讲小结

- driver 的日志分三层：**应用类**（用户库自产自销）、**Host 内核态**（共享内存搬运）、**Device 系统类**（msnpureport 经 HDC 拉取），排查应自上而下逐层下钻。
- `drv_log_user` 是用户态日志打印底座，核心是一个可运行时替换的函数指针表 `g_log_print_info`；上层可通过 `drv_log_out_handle_register` 接管输出。
- `drv_log_user_kernel_api.c` 中的 `user_err[]` 表把上百个 Linux `errno` 多对一收敛为对外 `DRV_ERROR_*`，是跨层错误码翻译的关键。
- `drv_share_log` 用固定地址的共享内存 + 环形缓冲实现 Host 内核日志到用户态的搬运，「按通道读取」即按 `enum share_log_type_enum`（`SHARE_LOG_ERR`/`SHARE_LOG_RUN_INFO`）选择 `share_log_read_err`/`share_log_read_run_info`。（本仓库不存在 `log_read_by_type` 这一接口。）
- `msnpureport` 用 `config`/`report` 两个子命令分别「配置设备」与「导出日志」；config 走 HDC 短连接，report 走 HDC 长连接拉文件，`-f` 会额外导出历史维测信息与 Host 内核日志。
- `msnpureport -f` 生成的时间戳目录里，`slog/host/host_kernel.log` 来自 `halGetDeviceInfoByBuff(... INFO_TYPE_HOST_KERN_LOG ...)`，与 Device 日志分属不同来源。

## 7. 下一步学习建议

- **继续 DMC 之旅**：下一讲 [u5-3 Profiling 性能采集适配](u5-l3-prof-adapt.md) 讲同一 DMC 家族里的 prof 模块，它会复用本讲提到的 HDC 通道（`HDC_SERVICE_TYPE_PROFILING`），可以对照理解「日志」与「性能数据」如何共用通信底座。
- **深入黑匣子**：本讲多次提到 bbox（黑匣子），其导出由 `BboxStartDump` 触发，完整机制在 [u7-l6 黑匣子 bbox 与基础设施工具](u7-l6-bbox-and-infrastructure.md) 详解，建议读完 u5 后带着「系统异常时如何保存临终日志」的问题去读。
- **回到通信底座**：若对 `Adx*`、`HDC_SERVICE_TYPE_*`、短/长连接仍感模糊，可回看 [u5-l1 device_monitor 通路](u5-l1-dmc-device-monitor.md) 与 [u3-l2 HDC 模型](u3-l2-hdc-communication.md)，把 HDC 这一「共用底座」彻底吃透。
- **端到端调试**：[u8-l1 日志体系与端到端调试验证](u8-l1-logging-and-debugging.md) 会把本讲的三层日志与 FAQ 打通成一套问题排查清单，作为本系列调试主题的总结。
