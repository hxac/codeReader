# 日志驱动 logdrv 与 msnpureport 日志导出

## 1. 本讲目标

本讲承接 u5-l1（DMC 设备维护组件与 device_monitor 通路）。在上一讲里我们看到，DSMI、logdrv、prof 等 DMC 子模块都复用 device_monitor 提供的「消息收发框架 + HDC 传输」来与设备交互。本讲要回答一个更基础的问题：**这些模块在运行过程中产生的日志，是怎么打出来、又怎么被收集导出的？**

学完后你应该能够：

- 理解 `logdrv` 子模块如何为整个 HAL 用户态库提供统一的日志打印框架（级别、模块名、输出后端）。
- 读懂 `drv_log_user` 的「可插拔输出后端」设计：默认写 syslog，上层注册后可改写为 `dlog`。
- 理解 `drv_share_log` 如何用一块预留虚拟地址 + 环形缓冲，实现 Host↔Device 之间的共享日志，以及真正的「按类型读取」机制。
- 读懂 `msnpureport` 命令行工具的子命令分发与参数解析。
- 串起「应用类日志 → dmesg 内核日志 → msnpureport 设备日志」三层调试链路，并能说清 `msnpureport -f` 的导出链路。

> 说明：规划稿里提到的 `log_read_by_type`、`channel_type` 两个名字，在当前源码的 logdrv 目录中并不存在（`channel_type` 只出现在芯片复位的 `dcmi_hot_reset_intf.c` 里，与日志无关）。本讲以**真实源码**为准，把「按类型读取」讲解为 `share_log_read_*` 系列函数与 `enum share_log_type_enum`，不再沿用那两个不存在的符号名。

## 2. 前置知识

在进入源码前，先建立几个直观概念：

- **syslog / dmesg**：Linux 经典的日志机制。用户态程序用 `vsyslog()` 把日志写到系统日志服务（`rsyslog`，落地到 `/var/log/messages` 或 `/var/log/syslog`）；内核态用 `printk`，可用 `dmesg` 查看。logdrv 用户态默认走的就是 syslog。
- **构造函数（constructor）**：GCC 扩展 `__attribute__((constructor))` 标记的函数，会在 `main` 执行之前、动态库被加载时自动运行。logdrv 用它在库加载阶段完成日志级别初始化，业务代码无需显式调用。
- **函数指针表 / 可插拔后端**：把「日志怎么打」抽象成一组成员全是函数指针的结构体，运行时替换成员即可改变行为。这是 logdrv 的核心设计。
- **预留虚拟地址 + mmap**：在固定的高端虚拟地址（如 `0xE000_0000_0000`）用 `mmap(MAP_PRIVATE|MAP_ANONYMOUS)` 申请一段匿名内存。多个进程映射同一约定地址时，就构成一块「共享黑板」。这是 `share_log` 跨进程/跨态传递日志的物理基础。
- **环形缓冲（ring buffer）**：用 `read`/`write` 两个下标管理一段定长缓冲，写到末尾绕回头部，无需搬移数据，是日志/队列类场景的标配。`share_log` 就是一个简单的环形缓冲。
- **TLV（Type-Length-Value）**：一种常见的协议封装格式：先写类型、再写长度、最后写数据体。`msnpureport` 与设备守护进程通信时就用 TLV 封装请求。
- **HDC 业务频道（serviceType）**：u5-l1 已讲过，HDC 用 `serviceType` 区分不同业务频道并做 QoS 分级。本讲会遇到 `HDC_SERVICE_TYPE_IDE_FILE_TRANS`（文件传输频道）、`HDC_SERVICE_TYPE_LOG`（日志频道）等。

> 与 u5-l1 的衔接：u5-l1 讲的 device_monitor 是「消息收发的公共底座」；本讲的 logdrv 是「日志打印与采集的公共底座」。两者都是 DMC 下被各子模块复用的基础设施，只是职责不同——前者管「把消息送达设备」，后者管「把日志打出来/捞回来」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/ascend_hal/dmc/logdrv/drv_log_user.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c) | 用户态日志库的构造函数入口，库加载时初始化控制台日志级别。 |
| [src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c) | 日志框架核心：级别/模块名字符串表、errno→drvError 映射、可插拔输出后端注册、错误消息上报。 |
| [src/ascend_hal/dmc/logdrv/drv_log_user_common.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_common.c) | 对 `kernel_api` 的薄封装层，对外暴露不带 `_inner` 后缀的稳定符号。 |
| [src/ascend_hal/dmc/logdrv/drv_share_log.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c) | 基于预留地址的共享日志（环形缓冲）：创建/销毁/按类型读取。 |
| [src/ascend_hal/inc/dmc/dmc_log_user.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h) | 日志公共头：`DRV_ERR/DRV_WARN/...` 等打印宏定义。 |
| [src/ascend_hal/inc/dmc/dmc_share_log.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h) | 共享日志公共头：各模块预留地址常量与 `share_log_type_enum`。 |
| [src/ascend_hal/msnpureport/msnpureport.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c) | msnpureport 工具的 `main` 入口。 |
| [src/ascend_hal/msnpureport/options/msnpureport_options.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c) | 子命令（config/report）分发与命令行选项解析。 |
| [src/ascend_hal/msnpureport/config/msnpureport_config.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c) | `config` 子命令实现：把请求封装成 TLV，经 HDC 短连接下发设备。 |
| [src/ascend_hal/msnpureport/report/msnpureport_report.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c) | `report` 子命令实现：拉取/导出设备日志与黑匣子。 |

补充参考文件：`msnpureport_common.h`（`ArgInfo`/`MsnReq`/`ConfigInfo` 结构体）、`inc/adump/ide_tlv.h`（TLV 请求结构）、`script/hal_log_collect_host.sh`（Host 侧日志一键采集脚本）、`docs/zh/QUICKSTART.md`（三层调试链路说明）。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：日志框架总览 → drv_log_user 用户态接口 → share_log 按类型读取 → msnpureport 子命令分发 → config/report 导出链路。

### 4.1 logdrv 日志框架总览：级别、模块、输出后端

#### 4.1.1 概念说明

`logdrv`（log driver）不是某一条具体的「读日志」接口，而是**整个 HAL 用户态库 `libascend_hal.so` 的日志打印基础设施**。HAL 库里几乎所有模块（SVM、DSMI、HDC、TRS、DMS……）打日志时调用的 `DRV_ERR / DRV_WARN / DRV_INFO` 等宏，最终都落到 logdrv 提供的这套框架上。

这套框架要解决三个问题：

1. **级别控制**：日志分 EMERG/ALERT/CRIT/ERR/WARNING/NOTICE/INFO/DEBUG 八级（与 POSIX syslog 对齐），运行时按「控制台日志级别」门槛过滤——只有级别号 ≤ 门槛的才真正输出，避免日志洪水。
2. **模块归属**：每条日志要标明来自哪个子模块（devmm、hdc、tsdrv、dmp……），方便定位。logdrv 用一张「模块类型枚举 → 模块名串」的表来翻译。
3. **输出后端可替换**：默认后端是 `vsyslog`（写系统日志）；但当本库被上层（如 Runtime/slog 体系）以「工具日志」模式加载时，上层会注册自己的打印函数，把日志改走 `dlog` 体系。这就要求打印后端必须是**可插拔**的。

此外，HAL 大量接口通过 `ioctl` 陷入内核，内核返回的是 POSIX `errno`，而对外要返回统一的 `drvError_t`。logdrv 顺带承担了 **errno → drvError_t 的查表映射**，这也是它的职责之一。

#### 4.1.2 核心流程

一条 `DRV_ERR(module, "...")` 的执行过程：

1. 宏展开后先比较「本条日志级别」与「当前控制台日志级别门槛」。
2. 门槛通过，则拼装一行带前缀的日志：`[级别串][时间戳][文件:行号][ascend][pid,tid][drv][模块名][函数名] 正文`。
3. 调用「当前生效的打印后端函数指针」把这一行送出去（默认 `vsyslog`）。
4. 若模块是 DMP 或 DEV_MANAGER，额外再走一份兼容的 `dsmi_printf` 输出。

关键在于第 3 步的「打印后端函数指针」是**运行时可替换**的——这就是「可插拔输出后端」。

#### 4.1.3 源码精读

日志级别到字符串的默认表（用于拼前缀 `[ERROR]` 等），位于 [drv_log_user_kernel_api.c:29-32](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L29-L32)：

```c
STATIC const char *drv_log_level_default_str[DRV_LOG_LEVEL_MAX] = {
    [LOG_EMERG] = "[EMERG]", ... [LOG_ERR] = "[ERROR]",
    [LOG_WARNING] = "[WARNING]", [LOG_INFO] = "[INFO]", [LOG_DEBUG] = "[DEBUG]",
};
```

模块类型到模块名串的表，在 `drv_log_get_module_str_inner` 中，[drv_log_user_kernel_api.c:223-260](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L223-L260)。它把 `HAL_MODULE_TYPE_DEVMM` 翻译成 `"devmm"`、`HAL_MODULE_TYPE_HDC` 翻译成 `"hdc"` 等，越界返回 `NULL`。这张表就是日志里 `[drv][devmm]` 这种模块前缀的来源。

真正的打印逻辑不在 logdrv 里，而在公共头 `DRV_SYSLOG_BASE` 宏中，[dmc_log_user.h:50-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h#L50-L66)。它的核心是这一句：

```c
(*get_log_Print())(mask, (int32_t)get_log_level_shift((uint32_t)LEVEL),
    "%s%s[%s:%d]...[drv][%s][%s]" fmt,
    get_log_get_level_string((uint32_t)LEVEL), get_log_get_print_time(),
    __FILE__, __LINE__, ..., drv_log_get_module_str(module), __func__, ##__VA_ARGS__);
```

注意 `get_log_Print()` 返回的是**一个函数指针**，`*` 解码后调用它。级别串、时间戳、模块名都在这里拼好。上层业务代码只需写 `DRV_ERR(HAL_MODULE_TYPE_DEVMM, "alloc failed size=%zu", sz)`，框架自动补齐所有上下文。

各级别宏只是 `DRV_SYSLOG` 的别名，[dmc_log_user.h:73-100](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h#L73-L100)：`DRV_ERR`→`LOG_ERR`、`DRV_WARN`→`LOG_WARNING`、`DRV_INFO`/`DRV_DEBUG` 类推；另有一组 `DRV_RUN_ERR/DRV_RUN_INFO`「运行日志」走 `DRV_LOG_CMPT`，受 `is_run_log()` 开关控制（默认降级为 `LOG_CRIT`，工具日志模式下才完整输出）。

#### 4.1.4 代码实践

**实践目标**：用源码确认「同一条日志在不同模式下的输出形态」。

**操作步骤**：

1. 打开 [dmc_log_user.h:50-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h#L50-L66)，找到 `DRV_SYSLOG_BASE` 的格式串。
2. 在 [drv_log_user_kernel_api.c:223-260](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L223-L260) 中查出 `HAL_MODULE_TYPE_DMP` 对应的模块名串。
3. 假设源码里有一句 `DRV_ERR(HAL_MODULE_TYPE_DMP, "reset failed ret=%d", ret)`，手工把宏展开，写出它在 syslog 里实际出现的那一行（含 `[ERROR]`、时间戳、`[drv][dmp][函数名]` 等前缀）。

**需要观察的现象 / 预期结果**：展开后应形如 `[ERROR][2026-08-13-10:00:00:123456][xxx.c:42][ascend][curpid:1234,5678][drv][dmp][some_func] reset failed ret=...`。注意第 58-64 行：当模块是 `DMP` 或 `DEV_MANAGER` 时，还会**额外**用 `dsmi_printf` 再输出一份兼容日志——这是历史兼容设计。

> 本实践为源码阅读型，不需要运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`DRV_INFO` 默认会被打印出来吗？
**答案**：默认不会。`DRV_SYSLOG` 用 `get_con_log_level()` 作为门槛，而门槛默认是 `LOG_ERR`（见 4.2.3 的 `drv_log_rsyslog_console_level` 初值）。`LOG_INFO` 的级别号大于 `LOG_ERR`，被门槛过滤。只有把控制台日志级别调到 `LOG_INFO` 或更详细时才会打印。

**练习 2**：为什么模块名要用「枚举→串」的表来翻译，而不是直接在调用处写字符串？
**答案**：用枚举做参数（`HAL_MODULE_TYPE_DEVMM`）让调用方传的是编译期常量、不易写错；集中维护一张表既保证全库模块名统一，又便于统计与按模块过滤。若调用处散落字符串，容易拼写不一致。

---

### 4.2 drv_log_user：用户态接口与可插拔输出后端

#### 4.2.1 概念说明

`drv_log_user` 是 logdrv 对外暴露的「用户态日志接口」层，由三个 `.c` 文件分工：

- `drv_log_user.c`：极薄，只放一个构造函数，库加载时把系统当前的 console 日志级别读进来作初值。
- `drv_log_user_kernel_api.c`：**实现核心**。级别/模块表、errno 映射、输出后端的注册与切换、错误消息上报都在这里。
- `drv_log_user_common.c`：一层薄封装，把 `_inner` 后缀的内部函数包装成不带后缀的对外符号（如 `errno_to_user_errno` 调 `errno_to_user_errno_inner`），并实现几个参数校验类错误上报函数（`report_arg_null_pointer` 等）。

「可插拔输出后端」是本模块的灵魂。框架定义了一个全是函数指针的结构体 `struct drv_log_print_info`，全局只有一个实例 `g_log_print_info`。打印日志时（`DRV_SYSLOG_BASE`）调用的 `get_log_Print()`、`get_con_log_level()` 等，都是去读这个实例里的指针。**谁改了这些指针，日志就改走谁的后端**。

#### 4.2.2 核心流程

后端切换的两种状态：

- **默认态（rsyslog）**：`g_log_print_info` 的成员指向 `drv_syslog`（调 `vsyslog`）、`drv_get_tm_default`（格式化时间）、`drv_log_get_level_str_default`（级别串）等默认实现，门槛指针指向 `drv_log_rsyslog_console_level`。
- **工具日志态（tool/dlog）**：当上层调用 `drv_log_out_handle_register` 注册了一个 `log_out_handle`（内含 `DlogInner` 打印函数与 `logLevel`），框架把 `g_log_print_info` 全部成员改指向新后端，并把门槛指针改指向 `drv_log_tool_console_level`，级别做 glibc↔tool 两套词汇表的换算。

注册/注销是一对对称操作：注册即「把默认指针替换为工具后端」，注销即「把指针恢复为默认后端」。

errno 映射则是另一条独立链路：`errno_to_user_errno(errno)` → 查 `user_err[]` 表 → 返回对应的 `DRV_ERROR_*`。

#### 4.2.3 源码精读

可插拔后端的「函数指针表」定义，[drv_log_user_kernel_api.c:40-46](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L40-L46)：

```c
struct drv_log_print_info {
    uint32_t *con_log_level;                                        // 当前门槛级别指针
    const char *(*log_get_level_string)(uint32_t level);            // 级别串
    const char *(*log_get_print_time)(void);                        // 时间戳
    uint32_t (*log_level_shift)(uint32_t level);                    // 级别换算
    void (*log_print)(int32_t module_id, int32_t level, ...);       // 真正打印
};
```

全局唯一实例，默认指向 rsyslog 后端，[drv_log_user_kernel_api.c:351-359](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L351-L359)：

```c
STATIC uint32_t drv_log_rsyslog_console_level = LOG_ERR;          // 默认门槛
STATIC uint32_t drv_log_tool_console_level = LOG_ERR;
struct drv_log_print_info g_log_print_info = {
    .con_log_level = &drv_log_rsyslog_console_level,               /* default log level */
    .log_get_level_string = drv_log_get_level_str_default,
    .log_get_print_time   = drv_get_tm_default,
    .log_level_shift      = drv_log_level_shift_default,
    .log_print            = drv_syslog,                            // 默认走 vsyslog
};
```

切换到工具后端的注册函数，[drv_log_user_kernel_api.c:363-395](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L363-L395)。校验 `handle` 与 `logLevel` 合法后，做四件替换 + 一个换算：

```c
g_run_log_status = flag;
drv_log_tool_console_level = drv_log_level_tool_to_glibc(handle->logLevel); // 工具级别换算成 glibc 级别
g_log_print_info.con_log_level        = &drv_log_tool_console_level;        // 门槛指针改向
g_log_print_info.log_get_level_string = drv_log_get_level_str;              // 后端函数全替换
g_log_print_info.log_get_print_time   = drv_get_tm;
g_log_print_info.log_level_shift      = drv_log_level_glibc_to_tool;
g_log_print_info.log_print            = handle->DlogInner;                  // 关键：打印换成上层注入的函数
```

注销则把指针全部还原为默认实现，[drv_log_user_kernel_api.c:403-412](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L403-L412)。

构造函数入口（库加载即运行），`drv_log_user.c` 全文只有它，[drv_log_user.c:14-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c#L14-L24)：

```c
static void __attribute__((constructor)) drv_log_init(void)
{
    uint32_t drv_log_rsyslog_console_level_tmp = LOG_ERR;
    drvMngGetConsoleLogLevel(&drv_log_rsyslog_console_level_tmp);   // 从设备管理读系统配置的级别
    drv_log_rsyslog_console_level_set(drv_log_rsyslog_console_level_tmp); // 写入默认门槛
}
```

它的作用是：进程一加载 `libascend_hal.so`，无需任何业务代码调用，日志门槛就已被初始化为「系统配置的 console 级别」（读不到则保持 `LOG_ERR`）。这就是为什么 HAL 库「开箱即用」能打日志。

errno→drvError 映射表 `user_err[]` 是一张以 errno 为下标的数组，[drv_log_user_kernel_api.c:48-191](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L48-L191)。查表函数 [drv_log_user_kernel_api.c:193-221](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L193-L221)：负数先取绝对值、越界的特殊码（150/151/152）单独处理、`0` 返回 `DRV_ERROR_NONE`、表项为 0 的兜底为 `DRV_ERROR_IOCRL_FAIL`。典型映射：`EINVAL→DRV_ERROR_PARA_ERROR`、`ENOMEM→DRV_ERROR_OUT_OF_MEMORY`、`ETIMEDOUT→DRV_ERROR_WAIT_TIMEOUT`、`EOPNOTSUPP→DRV_ERROR_NOT_SUPPORT`。

封装层 `drv_log_user_common.c` 把 `_inner` 函数包成对外符号，例如 [drv_log_user_common.c:18-21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_common.c#L18-L21) 的 `errno_to_user_errno`。该文件还实现了几个参数校验类错误上报函数，如 `report_arg_null_pointer` 用 `REPORT_PREDEFINED_ERR_MSG("EL0017", ...)` 上报标准化错误码，[drv_log_user_common.c:131-137](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_common.c#L131-L137)。

#### 4.2.4 代码实践

**实践目标**：验证「可插拔后端」的注册/注销对称性。

**操作步骤**：

1. 对照 [drv_log_user_kernel_api.c:363-395](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L363-L395)（注册）与 [drv_log_user_kernel_api.c:403-412](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L403-L412)（注销），逐个成员比对：注册时被替换的 5 个成员，注销时是否各自被还原为同名带 `_default` 后缀的默认实现？
2. 思考：如果上层只注册、忘记注销，会发生什么？

**需要观察的现象 / 预期结果**：

- 注册改写 `con_log_level / log_get_level_string / log_get_print_time / log_level_shift / log_print` 五项；注销把这五项分别还原为 `&drv_log_rsyslog_console_level / drv_log_get_level_str_default / drv_get_tm_default / drv_log_level_shift_default / drv_syslog`，完全对称。
- 若只注册不注销，进程后续所有日志都会继续走 `DlogInner` 后端，即便上层对象已被释放——这会埋下「使用已释放函数指针」的隐患，所以二者必须成对使用。

> 本实践为源码阅读型，「待本地验证」项为：实际运行时可在注册前后各打一条 `DRV_ERR`，观察其输出去向（syslog vs slog）是否确实切换。

#### 4.2.5 小练习与答案

**练习 1**：`drvMngGetConsoleLogLevel` 在哪里被调用？为什么放在构造函数里？
**答案**：在 `drv_log_init` 构造函数中调用（[drv_log_user.c:14-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c#L14-L24)）。放在构造函数里是为了「库一被加载、日志门槛就绪」，业务代码无需关心初始化顺序，避免出现「日志还没就绪就报错却看不到」的情况。

**练习 2**：`errno_to_user_errno(-EINVAL)` 会返回什么？
**答案**：返回 `DRV_ERROR_PARA_ERROR`。流程：负数取绝对值得 `EINVAL`（22），`22 < ERROR_NUN_MAX` 且 `user_err[EINVAL] != 0`，故返回表项 `DRV_ERROR_PARA_ERROR`。

---

### 4.3 share_log：预留地址共享日志与「按类型读取」

#### 4.3.1 概念说明

前面两节讲的是「Host 用户态自己产生的日志怎么打」。但昇腾是 Host↔Device 架构，**设备侧（Device OS、各内核模块）也会产生日志**，这些日志怎么送到 Host？有两条路径：

1. **主动拉取**：Host 用 `msnpureport` 工具经 HDC 把设备日志文件拉回来（见 4.5）。
2. **共享内存直写**：把一块约定好的虚拟地址同时映射给 Host 与 Device（或同一 Host 的多个进程），写日志的那一方直接往这块内存写，读的那一方直接从这块内存读——无需走消息收发。这就是 `share_log`。

`share_log` 的典型用途是：设备内核态驱动（或同一进程内不同子模块）把「错误日志 / 运行信息」写进共享内存，Host 用户态在合适的时机读出来，转成标准 `DRV_ERR` / `DRV_RUN_INFO` 打印。它**不收发消息**，是 logdrv 里最轻量的日志通道。

「按类型读取」的真正含义就在这里：共享日志分两类——`SHARE_LOG_ERR`（错误类）与 `SHARE_LOG_RUN_INFO`（运行信息类），分别存在同一模块的两段不同预留地址里，读取时按类型调用 `share_log_read_err` 或 `share_log_read_run_info`。

#### 4.3.2 核心流程

共享日志用**一个简单的环形缓冲**实现：

- 预留地址布局：每个参与模块（devmm/tsdrv/devmng/hdc/esched/xsmem/queue/common/apm）各占两段固定高端地址，分别给 ERR 与 RUN_INFO，见 [dmc_share_log.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h)（如 `DEVMM_SHARE_LOG_START = 0xE0000080000`、`DEVMM_SHARE_LOG_RUNINFO_START = 0xE00000C0000`）。每段上限 `SHARE_LOG_MAX_SIZE = 4KB`。
- 创建：`mmap` 到约定地址 → 写入魔数 `drvshartlogab90cd78ef56` → 初始化 `record_base / record_size / read=0 / write=0`。
- 写入：生产方追加字节、推进 `write` 下标。
- 读取：消费方比较 `write` 与 `read`，若有新数据，把 `[read, write)` 区间拷出，把换行替换成空格，按类型用 `DRV_ERR`（ERR 类）或 `DRV_RUN_INFO`（RUN_INFO 类）打印，再把 `read` 推进到 `write`。

环形缓冲的有效数据长度为 \( \text{write} - \text{read} \)（`share_log` 只处理 `write > read` 的顺序写场景，不做绕回拼接）。

#### 4.3.3 源码精读

类型枚举与预留地址常量在公共头 [dmc_share_log.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h)：

```c
enum share_log_type_enum {
    SHARE_LOG_ERR = 0,      // 错误类日志
    SHARE_LOG_RUN_INFO,     // 运行信息类日志
    SHARE_LOG_TYPE_MAX,
};
```

模块×类型 的二维管理表 `g_module_mng[模块][类型]`，记录每段共享内存的起始地址与初始化标志，[drv_share_log.c:48-67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L48-L67)。例如 `HAL_MODULE_TYPE_DEVMM` 的 ERR 段起于 `DEVMM_SHARE_LOG_START`、RUN_INFO 段起于 `DEVMM_SHARE_LOG_RUNINFO_START`。

创建单段共享日志，[drv_share_log.c:71-117](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L71-L117)。关键步骤：

```c
info = (struct share_log_info *)mmap(g_module_mng[...].start, size,
                                     PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
if (info != (struct share_log_info *)g_module_mng[...].start) { ... return; } // 地址必须精确命中
(void)memset_s(info, size, 0, size);
(void)snprintf_s(info->magic, ... , "%s", SHARE_LOG_MAGIC);   // 写魔数，供读端校验
info->record_base = (char *)start + SHARE_LOG_RECORD_OFFSET;  // 头部 100B 之后才是记录区
info->record_size = size - SHARE_LOG_RECORD_OFFSET;
info->read = 0; info->write = 0;
```

注意 `SHARE_LOG_RECORD_OFFSET = 100`——前 100 字节是头部（魔数 + 指针下标），其后才是真正的日志记录区。魔数校验（`share_log_magic_check`）是读端防野指针的护栏。

读取单模块单类型的核心，[drv_share_log.c:167-225](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L167-L225)。这就是真正的「按类型读取」。在校验未初始化/魔数不符/无新数据（`write==read`）/越界等条件后，关键逻辑：

```c
size_t out_size = (size_t)(tmp_write - tmp_read);
memcpy_s(read_buff, info->record_size, (char *)info->record_base + tmp_read, out_size);
for (i = 0; i < out_size - 1; i++) { if (read_buff[i] == '\n') read_buff[i] = ' '; } // 换行转空格
if (log_type == SHARE_LOG_ERR) {
    DRV_ERR(module_type, "%s", read_buff);          // ERR 类 → DRV_ERR
} else {
    DRV_RUN_INFO(module_type, "%s", read_buff);     // RUN_INFO 类 → DRV_RUN_INFO
}
info->read = tmp_write;                              // 读指针推进
```

三个对外读接口只是「按类型 + 固定附带读 COMMON 模块」的组合，[drv_share_log.c:227-243](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L227-L243)：

```c
void share_log_read_err(enum devdrv_module_type m) {      // 读错误类
    share_log_read_in_single_module(SHARE_LOG_ERR, m);
    share_log_read_in_single_module(SHARE_LOG_ERR, HAL_MODULE_TYPE_COMMON);
}
void share_log_read_run_info(enum devdrv_module_type m) { // 读运行信息类
    share_log_read_in_single_module(SHARE_LOG_RUN_INFO, m);
    share_log_read_in_single_module(SHARE_LOG_RUN_INFO, HAL_MODULE_TYPE_COMMON);
}
```

每个读接口除了读「指定模块」外，都**额外读一次 `COMMON` 模块**——`COMMON` 是公共缓冲，存放不便归类到具体模块的通用日志。这就是 `share_log_read` 系列的「按类型读取」全貌。

`share_log_destroy` 在销毁前会先调一次 `share_log_read(HAL_MODULE_TYPE_COMMON)`，[drv_share_log.c:144](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L144)——销毁前先把残余日志读出来，避免信息丢失。

> 与 errno 映射的呼应：`share_log_read_in_single_module` 把读出的 ERR 类日志用 `DRV_ERR` 打印（[drv_share_log.c:213](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L213)），而 `DRV_ERR` 最终走的就是 4.2 里那套可插拔后端——两条链路在此汇合。

#### 4.3.4 代码实践

**实践目标**：理解 share_log「按类型读取」的两类分流。

**操作步骤**：

1. 阅读 [drv_share_log.c:167-243](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L167-L243)。
2. 在 [dmc_share_log.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_share_log.h) 中查出 `HDC` 模块 ERR 段与 RUN_INFO 段各自的起始地址常量。
3. 回答：若设备侧 HDC 模块往 ERR 段写了 200 字节日志，Host 调 `share_log_read_err(HAL_MODULE_TYPE_HDC)` 后，这 200 字节最终以哪条 `DRV_*` 宏打印？读完后 `read` 下标变成多少（用 `write` 表示）？

**需要观察的现象 / 预期结果**：

- HDC 的 ERR 段 = `HDC_SHARE_LOG_START`，RUN_INFO 段 = `HDC_SHARE_LOG_RUNINFO_START`。
- 200 字节经 `share_log_read_in_single_module(SHARE_LOG_ERR, HDC)` 读出，因 `log_type == SHARE_LOG_ERR` 而走 `DRV_ERR(HDC, "%s", read_buff)` 打印；读完 `read` 被推进到 `write`（即 `info->read = tmp_write`）。

> 本实践为源码阅读型。实际写日志的一方在内核态/设备侧，单在用户态难以注入数据，故运行验证标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`share_log` 为什么用魔数 `SHARE_LOG_MAGIC` 校验？
**答案**：预留地址在不同环境/版本里未必都已被本模块 `mmap` 初始化过；读端若直接读未初始化的内存会拿到野值。魔数是一道护栏：只有写端按约定先写入了这个固定字符串，读端才认为这段内存是合法的 share_log（见 `share_log_magic_check`），否则跳过不读。

**练习 2**：为什么 `share_log_read_err` 要额外再读一次 `COMMON` 模块？
**答案**：`COMMON` 是公共日志缓冲，承载不归属某个具体模块的通用日志。每次按模块读取时附带读一次 COMMON，保证公共日志被及时消费，不会因为没有显式「读 COMMON」的调用而堆积丢失。

---

### 4.4 msnpureport：子命令分发与参数解析

#### 4.4.1 概念说明

`msnpureport` 是面向运维/开发的**命令行日志工具**，编译后安装到 `/usr/local/Ascend/driver/tools/msnpureport`（见 QUICKSTART）。它干两类事：

- `config`：查询/设置设备配置与日志级别（如 `msnpureport config --set --log --global info -d 0`）。
- `report`：从设备导出日志与黑匣子文件（如 `msnpureport report`、`msnpureport -f`）。

它支持两套命令语法：**旧语法**（短选项，如 `-f`、`-t`，靠 `getopts` 风格解析，QUICKSTART 里 `msnpureport -f` 即此）与**新语法**（子命令，如 `config`/`report` + `--xxx` 长选项）。入口 `MsnOptions` 会先判别属于哪套，再分别走 `MsnOptionsOld` 或 `MsnOptionsHandle`。

> 本节聚焦新语法的「子命令分发 + 选项表解析」，这是理解后续 config/report 链路的前提。`-f` 旧语法由 `msnpureport_options_old.c` 处理，最终也归并到同一套 `ArgInfo` 结构与 `MsnReport`。

#### 4.4.2 核心流程

新语法主流程：

1. `main`（[msnpureport.c:18-21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c#L18-L21)）→ `MsnOptions`。
2. `MsnOptions` 初始化一个全零的 `ArgInfo`（默认 `cmdType=INVALID_CMD`、`deviceId=MAX_DEV_NUM`）。
3. 判别新旧语法：参数过少或第二个参数以 `-` 开头 → 旧语法 `MsnOptionsOld`；否则 → 新语法 `MsnOptionsHandle(argc-1, &argv[1])`。
4. `MsnOptionsHandle` 按 `argv[0]` 分发：`config`→`MsnGetConfigOptions`、`report`→`MsnGetReportOptions`、`help`/`version` 直接处理。
5. 选项解析用 `mmGetOptLong`（mmpa 提供的跨平台 getopt_long）+ 选项表（`CONFIG_OPTS` / `g_reportOptions`），边解析边填充 `ArgInfo`。
6. 解析完由 `MsnHandleArgInfo` 按 `cmdType` 二次分发到业务函数：`CONFIG_GET/SET`→`MsnConfig`、`REPORT/REPORT_PERMANENT`→`MsnReport`。

`ArgInfo` 是贯穿全工具的「参数汇总结构」，所有选项最终都填进它，业务函数只读它。

#### 4.4.3 源码精读

入口，[msnpureport.c:18-21](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport.c#L18-L21)：

```c
int MAIN(int argc, char **argv) { return MsnOptions(argc, argv); }
```

`ArgInfo` 结构（贯穿全工具），[msnpureport_common.h:53-64](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport_common.h#L53-L64)：

```c
typedef struct {
    enum CmdType cmdType;     // CONFIG_GET / CONFIG_SET / REPORT / REPORT_PERMANENT / INVALID_CMD
    uint32_t subCmd;          // 具体子命令（ICACHE_RANGE / LOG_LEVEL / REPORT_FORCE ...）
    uint16_t deviceId;        // -d 指定的设备号
    uint16_t valueLen;        // value[] 有效长度
    int32_t reportType;       // report 的 -t 类型
    int32_t dockerFlag;       // --docker
    int32_t printMode;        // --print 0=syslog 1=stdout
    int32_t selfLogLevel;     // --log_level 工具自身日志级别
    char value[MAX_VALUE_STR_LEN + 64]; // 携带的值（配置值或日志级别串）
} ArgInfo;
```

顶层分发 `MsnOptions`，[msnpureport_options.c:1028-1080](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1028-L1080)。新旧语法判别是关键一句，[msnpureport_options.c:1044](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1044)：

```c
if ((argc < MIN_USER_ARG_LEN) || ((argv[1] != NULL) && (argv[1][0] == '-'))) {
    // 旧命令：msnpureport -f / -t 1 ...
    MsnOptionsOld(argc, argv, &argInfo, &flag);
} else {
    // 新命令：msnpureport config ... / report ...
    MsnOptionsHandle(argc - 1, &argv[1], &argInfo);
}
```

子命令分发 `MsnOptionsHandle`，[msnpureport_options.c:970-1000](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L970-L1000)，按 `argv[0]` 字符串比较分到 `config`/`report`/`help`/`version`。

`config` 的选项表 `CONFIG_OPTS`，[msnpureport_options.c:54-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L54-L71)，列出 `--get/--set/--icachecheck/--aic_switch/--coreid/--log/--global/--module/--event/-d/-h` 等。解析循环 `MsnGetConfigOptions` 用 `mmGetOptLong` 逐个取选项，分三类 handler：通用（`--get/--set/-d/--docker`，`HandleCommonCmd`）、DFX 设置（`--icachecheck/--aic_switch/...`，`HandleAicErrorCmd`）、日志级别（`--log/--global/--module/--event`，`HandleLogLevelCmd`）。

例如设设备日志级别 `--log --global info`，`MsnSetLogLevel` 把它格式化成字符串 `"SetLogLevel(LOGLEVEL_GLOBAL)[INFO]"` 存进 `argInfo.value`，[msnpureport_options.c:455-487](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L455-L487)——注意它并不直接改设备，而是把「指令串」原样带给设备侧解析。

业务二次分发 `MsnHandleArgInfo`，[msnpureport_options.c:1002-1026](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1002-L1026)：

```c
switch (argInfo->cmdType) {
    case CONFIG_GET: case CONFIG_SET: ret = MsnConfig(argInfo); break;   // → 4.5
    case REPORT: case REPORT_PERMANENT: ret = MsnReport(argInfo); break; // → 4.5
    ...
}
```

`report` 的选项表 `g_reportOptions` 与类型枚举，[msnpureport_options.c:73-126](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L73-L126)。其中 `-a/--all`、`-f/--force`、`-t/--type` 三者互斥（`MsnReportLogCmd` 校验 `subCmd != REPORT_DEFAULT` 报错），`--permanent` 开启持续导出模式。

#### 4.4.4 代码实践

**实践目标**：用源码确认新旧语法的分流边界，并预测 `msnpureport -f` 走哪条路径。

**操作步骤**：

1. 阅读 [msnpureport_options.c:1028-1059](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1028-L1059)。
2. 输入命令 `msnpureport -f`：`argc=2`、`argv[1]="-f"`。判断它命中第 1044 行的哪个分支。
3. 再输入 `msnpureport report --force`：`argv[1]="report"`，判断它命中哪个分支、最终 `argInfo.subCmd` 取何值（参考 [msnpureport_options.c:790-792](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L790-L792)）。

**需要观察的现象 / 预期结果**：

- `msnpureport -f`：`argv[1][0] == '-'` 命中**旧命令**分支，走 `MsnOptionsOld`。这与 QUICKSTART 第 164 行 `/usr/local/Ascend/driver/tools/msnpureport -f` 一致——文档示例用的是旧语法。
- `msnpureport report --force`：`argv[1]="report"` 不以 `-` 开头，走**新命令**分支 → `MsnOptionsHandle` → `MsnGetReportOptions` → `--force` 命中 `REPORT_ARGS_REPORT_FORCE` → `argInfo.subCmd = REPORT_FORCE`、`reportType = ALL_LOG`（[msnpureport_options.c:790-792](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L790-L792)）。两条路径最终都汇入 `MsnReport`。

> 本实践为源码阅读型。运行验证（待本地验证）：在已装驱动的机器上分别执行两种写法，观察输出目录是否一致。

#### 4.4.5 小练习与答案

**练习 1**：`msnpureport config --set --global info -d 0` 缺了哪个关键字会报错？
**答案**：缺 `--log`。`MsnSetLogLevel` 要求 `argInfo->subCmd == LOG_LEVEL` 才允许设置 `--global/--module/--event`（[msnpureport_options.c:457-460](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L457-L460)），而 `LOG_LEVEL` 是由 `--log` 置位的，故正确写法是 `--set --log --global info`。

**练习 2**：`report -a` 与 `report -f` 能否同时用？
**答案**：不能。`MsnReportLogCmd` 校验 `argInfo->subCmd != REPORT_DEFAULT` 时报错「only support one option at a time」（[msnpureport_options.c:781-784](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L781-L784)）。`-a/-f/-t` 三者互斥，一次只能选一个。

---

### 4.5 设备日志导出与配置链路（config / report）

#### 4.5.1 概念说明

`config` 与 `report` 是 `msnpureport` 的两个业务子命令，分别对应「查询/设置」与「导出」。二者都最终经 **HDC** 与设备侧守护进程通信，但形态不同：

- **config 是「短连接、一问一答」**：构造一个 TLV 请求，经 HDC 短连接（`AdxDevCommShortLink`）发给设备的 IDE 守护进程，等回一个 `ConfigInfo` 应答，超时 120s。适用于「设置一个值 / 查询一个值」这种轻量交互。
- **report 是「长连接、批量拉文件」**：建立 HDC 长连接，从设备拉取 slog/message/system_info/bbox/stackcore 等文件，落盘到以时间戳命名的目录。`-f/--force` 还会额外导出历史维测信息。

回顾 QUICKSTART 的三层调试链路（[docs/zh/QUICKSTART.md:143-167](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L143-L167)）：应用报错时，先看应用类日志 → 再看 `dmesg` 的 Host 内核日志 → 仍定位不了就用 `msnpureport -f` 导出 Device 侧日志（落到 `./时间戳/slog/host/host_kernel.log`）。本节的 report 链路就是第三层的实现。

#### 4.5.2 核心流程

**config 链路**（`MsnConfig`）：

1. 权限校验：`CONFIG_SET` 必须 root（`IsHaveRootPermission`）。
2. `MsnGetResult` 构造 TLV 请求：外层 `TlvReq{type=COMPONENT_MSNPUREPORT, devId, len}`，内层 `MsnReq{cmdType, subCmd, valueLen, value}`。
3. 若 `cmdType==CONFIG_SET`，把 `argInfo.value`（如 `"SetLogLevel(...)[INFO]"`）`memcpy` 进 `MsnReq.value`。
4. 经 `AdxDevCommShortLink(HDC_SERVICE_TYPE_IDE_FILE_TRANS, req, resultBuf, 120s)` 发给设备，等应答。
5. 设备回的 `ConfigInfo{len, isError, value}`：`isError` 为真则按 cmdType 打印告警/错误；`CONFIG_GET` 则 `MsnPrintInfo` 格式化打印，否则直接打印 `value`。

**report 链路**（`MsnReport`）：

1. `REPORT_PERMANENT`（持续导出）走 `MsnReportPermanent`：建目录、初始化文件老化管理、订阅故障事件、起 slogd 与 log daemon 接收线程常驻。
2. 一次性导出走 `SyncDeviceLog`：root 权限校验 → `CreateLogRootPath`（以时间戳建目录）→ 磁盘空间检查 → （`ALL_LOG` 时）`GetHostDrvLog` 取 Host 内核驱动日志 → `MsnSyncDeviceLog` 起线程从设备拉各类日志/bbox。
3. `-f/--force`：`bboxDumpOpt.force = true`（[msnpureport_report.c:1328-1330](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1328-L1330)），导出时含历史维测信息。

#### 4.5.3 源码精读

config 的请求构造与发送，`MsnGetResult`，[msnpureport_config.c:20-52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L20-L52)。TLV 二层封装是重点：

```c
TlvReq *req = (TlvReq *)MsnMalloc(sizeof(TlvReq) + sizeof(struct MsnReq) + argInfo->valueLen);
req->type = COMPONENT_MSNPUREPORT;          // 外层：标记来自 msnpureport 组件
req->devId = (int32_t)argInfo->deviceId;
req->len = sizeof(struct MsnReq) + argInfo->valueLen;
struct MsnReq *msnReq = (struct MsnReq *)req->value;  // 内层：业务请求体
msnReq->cmdType = argInfo->cmdType;         // CONFIG_GET / CONFIG_SET
msnReq->subCmd = argInfo->subCmd;           // ICACHE_RANGE / LOG_LEVEL ...
msnReq->valueLen = argInfo->valueLen;
if (argInfo->cmdType == CONFIG_SET) { memcpy_s(msnReq->value, ..., argInfo->value, ...); }

const uint32_t timeout = 120 * 1000;        // 120s
int32_t ret = AdxDevCommShortLink(HDC_SERVICE_TYPE_IDE_FILE_TRANS, req, resultBuf, bufLen, timeout);
```

`TlvReq` 结构定义见 [ide_tlv.h:65-70](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/inc/adump/ide_tlv.h#L65-L70)：`{enum cmd_class type; int dev_id; int len; char value[0];}`，`value[0]` 是柔性数组，承载内层 `MsnReq`。`MsnReq`/`ConfigInfo` 见 [msnpureport_common.h:66-77](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/msnpureport_common.h#L66-L77)。

config 的业务入口与应答处理 `MsnConfig`，[msnpureport_config.c:62-102](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L62-L102)：

```c
int32_t MsnConfig(const ArgInfo *argInfo) {
    if (argInfo->cmdType == CONFIG_SET && !IsHaveRootPermission()) { ... return EN_ERROR; } // root 校验
    char resultBuf[MSG_MAX_LEN] = {0};
    if (MsnGetResult(argInfo, resultBuf, MSG_MAX_LEN) != EN_OK) return EN_ERROR;
    struct ConfigInfo *configInfo = (struct ConfigInfo *)resultBuf;
    if (configInfo->isError) { ... return EN_ERROR; }      // 设备侧返回错误
    if (argInfo->cmdType == CONFIG_GET) MsnPrintInfo(...); // 查询结果格式化打印
    else MSNPU_PRINT("%s, device id:%u.", configInfo->value, argInfo->deviceId);
    return EN_OK;
}
```

report 的入口与 force 分支，`MsnReport`，[msnpureport_report.c:1319-1335](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1319-L1335)：

```c
int32_t MsnReport(ArgInfo *argInfo) {
    if (argInfo->cmdType == REPORT_PERMANENT)
        return MsnReportPermanent(argInfo->deviceId, (FileAgeingParam *)argInfo->value);
    struct BboxDumpOpt bboxDumpOpt = { false,false,false,false, argInfo->printMode, argInfo->selfLogLevel };
    if (argInfo->subCmd == REPORT_ALL)               bboxDumpOpt.all = true;
    else if ((argInfo->subCmd == REPORT_FORCE) ||
             ((argInfo->subCmd == REPORT_TYPE) && (argInfo->reportType == HISILOGS_LOG)))
        bboxDumpOpt.force = true;                    // ← -f / --force 在此置位
    else if (argInfo->reportType == VMCORE_FILE)     bboxDumpOpt.vmcore = true;
    return SyncDeviceLog(argInfo->deviceId, &bboxDumpOpt, argInfo->reportType);
}
```

一次性导出的主干 `SyncDeviceLog`，[msnpureport_report.c:842-864](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L842-L864)：root 校验 → 建时间戳根目录 → 磁盘空间检查 → `ALL_LOG` 时调 `GetHostDrvLog` 取 Host 内核日志 → `MsnSyncDeviceLog` 起线程拉取设备日志与 bbox。其中 `GetHostDrvLog` 取 Host 内核日志走的是 HAL 接口 `halGetDeviceInfoByBuff(0, MODULE_TYPE_LOG, INFO_TYPE_HOST_KERN_LOG, path, &len)`，[msnpureport_report.c:489](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L489)——这正是 QUICKSTART 里 `./时间戳/slog/host/host_kernel.log` 的来源（[QUICKSTART.md:167](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L167)）。

> 与 u5-l1 的呼应：config 经 `HDC_SERVICE_TYPE_IDE_FILE_TRANS`、report 的 slogd 接收经 `HDC_SERVICE_TYPE_LOG`（或新通道 `HDC_SERVICE_TYPE_PROFILING`）。这些 HDC 频道正是 u5-l1 讲的「HDC 业务频道 + serviceType」机制的具体落地。logdrv 提供「打印框架 + share_log 共享通道」，msnpureport 提供「HDC 文件/配置通道」，三者合力构成完整的「打日志 + 捞日志」闭环。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：串起 config 的「参数 → TLV → HDC → 设备应答」完整链路，并对照说明 `msnpureport -f` 的导出链路。

**操作步骤**：

1. **config 链路追踪**：以 `msnpureport config --set --log --global info -d 0` 为例，沿以下顺序阅读源码，画出数据流：
   - 选项解析：[msnpureport_options.c:455-487](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L455-L487)（`MsnSetLogLevel` 把 `info` 格式化成 `"SetLogLevel(1)[INFO]"` 写入 `argInfo.value`，`cmdType=CONFIG_SET`、`subCmd=LOG_LEVEL`）。
   - 业务分发：[msnpureport_options.c:1002-1026](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L1002-L1026)（`CONFIG_SET` → `MsnConfig`）。
   - 权限校验：[msnpureport_config.c:66-72](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L66-L72)（`CONFIG_SET` 需 root）。
   - TLV 封装与发送：[msnpureport_config.c:20-52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L20-L52)（`type=COMPONENT_MSNPUREPORT`，经 `AdxDevCommShortLink` 下发）。
   - 应答处理：[msnpureport_config.c:79-101](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L79-L101)（解析 `ConfigInfo` 并打印）。
2. **`-f` 导出链路对照**：阅读 [msnpureport_report.c:1319-1335](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1319-L1335) 与 [msnpureport_report.c:842-864](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L842-L864)，写出 `msnpureport -f`（等价新语法 `report --force`）从命令行到落盘的步骤。

**需要观察的现象 / 预期结果**：

- config 数据流：`--global info` → `argInfo.value="SetLogLevel(1)[INFO]"`、`cmdType=CONFIG_SET` → root 校验 → 包成 `TlvReq{COMPONENT_MSNPUREPORT, devId=0} + MsnReq{CONFIG_SET, LOG_LEVEL, ...}` → `AdxDevCommShortLink(HDC_SERVICE_TYPE_IDE_FILE_TRANS, 120s)` → 设备回 `ConfigInfo` → 打印 `value`。
- `-f` 数据流：旧语法 `MsnOptionsOld` 解析出 `cmdType=REPORT`、`subCmd=REPORT_FORCE`（等价新语法 `report --force`）→ `MsnReport` 置 `bboxDumpOpt.force=true`、`reportType=ALL_LOG` → `SyncDeviceLog`：root 校验 → 建时间戳目录 → `GetHostDrvLog` 取 host 内核日志（`halGetDeviceInfoByBuff(..., INFO_TYPE_HOST_KERN_LOG, ...)`）→ 起线程经 HDC 拉设备 slog/bbox 等 → 落盘到 `./时间戳/`。

> 运行验证（待本地验证）：在已安装驱动的环境执行 `msnpureport config --get -d 0`（root），观察返回的配置串；执行 `msnpureport -f`，观察生成的 `./时间戳/slog/host/host_kernel.log` 是否存在。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `CONFIG_SET` 必须是 root，而 `CONFIG_GET` 不要求？
**答案**：`SET` 会改动设备配置（日志级别、核开关、icache 范围等），属有副作用的写操作，必须限制权限以免误改；`GET` 只是查询无副作用，故放开（见 [msnpureport_config.c:66-72](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/config/msnpureport_config.c#L66-L72)）。同理 `report` 导出也要求 root（[msnpureport_report.c:844-847](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L844-L847)）。

**练习 2**：config 与 report 都用 HDC，二者用的频道一样吗？
**答案**：不一样。config 用「短连接」走 `HDC_SERVICE_TYPE_IDE_FILE_TRANS` 频道做一问一答；report 拉 slogd 用长连接走 `HDC_SERVICE_TYPE_LOG`（新通道为 `HDC_SERVICE_TYPE_PROFILING`），拉 log daemon 文件又用 `HDC_SERVICE_TYPE_IDE_FILE_TRANS`。频道（serviceType）区分了不同业务流，避免相互阻塞。

**练习 3**：`msnpureport -f` 比 `msnpureport report` 多导出了什么？
**答案**：`-f/--force` 使 `bboxDumpOpt.force=true`（[msnpureport_report.c:1328-1330](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1328-L1330)），bbox 导出时会额外包含「历史维测与计量信息」（见 report 帮助 [msnpureport_options.c:667-669](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/options/msnpureport_options.c#L667-L669)），信息更全但体积更大。

---

## 5. 综合实践

**任务：绘制 driver「日志产生 → 采集 → 导出」全链路图，并标注每段对应的源码函数。**

把本讲 5 个模块串起来，画一张覆盖「Host 用户态 / Host 内核态 / Device 侧」三栏的数据流图，至少包含以下 4 条通路，并在每条通路上标出关键函数与文件：

1. **Host 用户态自产日志**：业务代码 `DRV_ERR(module, ...)` → `DRV_SYSLOG_BASE` 宏（[dmc_log_user.h:50-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/dmc/dmc_log_user.h#L50-L66)）→ `g_log_print_info.log_print`（默认 `drv_syslog`→`vsyslog`）→ `/var/log/syslog`。
2. **Host 用户态日志级别初始化**：库加载 → 构造函数 `drv_log_init`（[drv_log_user.c:14-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user.c#L14-L24)）→ `drvMngGetConsoleLogLevel` → 写入门槛。
3. **共享日志直读**：设备/内核写 share_log 环形缓冲 → Host 调 `share_log_read_err/run_info`（[drv_share_log.c:227-243](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_share_log.c#L227-L243)）→ 按 `SHARE_LOG_ERR/RUN_INFO` 分流到 `DRV_ERR/DRV_RUN_INFO`。
4. **msnpureport 主动导出**：`msnpureport -f` → `MsnOptions`(旧) / `MsnOptionsHandle`(新) → `MsnReport`（[msnpureport_report.c:1319-1335](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/msnpureport/report/msnpureport_report.c#L1319-L1335)）→ `SyncDeviceLog` → `GetHostDrvLog`(`halGetDeviceInfoByBuff`) + HDC 长连接拉设备日志 → 落盘 `./时间戳/`。

完成后再回答一个综合问题：当上层以「工具日志模式」加载 HAL 库（调用 `drv_log_out_handle_register`），上述第 1、3 条通路的「打印后端」会发生什么变化？（提示：`g_log_print_info` 五个成员被整体替换，[drv_log_user_kernel_api.c:386-393](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/logdrv/drv_log_user_kernel_api.c#L386-L393)）

> 本实践为源码阅读 + 文档型，不涉及修改源码。运行态验证（如实际执行 `msnpureport -f`）标注「待本地验证」。

## 6. 本讲小结

- `logdrv` 是 HAL 用户态库的**日志打印基础设施**：`DRV_ERR/DRV_WARN/...` 等宏都落到它提供的「级别 + 模块名 + 可插拔后端」框架上。
- 可插拔后端的核心是全局函数指针表 `g_log_print_info`：默认走 `vsyslog`，上层 `drv_log_out_handle_register` 注册后切换到 `dlog`；注册/注销必须成对。构造函数 `drv_log_init` 在库加载即完成门槛初始化。
- `logdrv` 还兼任 **errno → drvError_t 映射**（`user_err[]` 表）与**标准化错误消息上报**（`REPORT_PREDEFINED_ERR_MSG`）。
- `share_log` 用「预留高端地址 + mmap + 环形缓冲 + 魔数校验」实现 Host↔Device 共享日志；真正的「按类型读取」是 `share_log_read_err / share_log_read_run_info` 按 `SHARE_LOG_ERR / SHARE_LOG_RUN_INFO` 分流——源码里没有 `log_read_by_type` / `channel_type` 这两个符号。
- `msnpureport` 同时支持旧短选项语法（`-f`、`-t`）与新子命令语法（`config`/`report`），`MsnOptions` 按 `argv[1]` 是否以 `-` 开头分流；所有选项汇总进 `ArgInfo`，再按 `cmdType` 二次分发到 `MsnConfig` / `MsnReport`。
- `config` 是「TLV + HDC 短连接」的一问一答（`AdxDevCommShortLink`，120s），`SET` 需 root；`report`（含 `-f`）是「HDC 长连接批量拉文件」，落盘到时间戳目录，`-f/--force` 额外含历史维测信息。二者与 QUICKSTART 的「应用日志 → dmesg → msnpureport」三层调试链路一一对应。

## 7. 下一步学习建议

- **u5-l3（Profiling 性能采集适配）**：同属 DMC，`prof` 子模块同样复用 HDC 通路（`HDC_SERVICE_TYPE_PROFILING`），可对照本讲理解「日志频道」与「性能频道」的分工。
- **u8-l1（日志体系与端到端调试验证）**：把本讲的三层调试链路（应用类 / Host 内核态 / Device 系统类日志）放到真实排障场景中演练，结合 `docs/zh/FAQ.md` 定位常见问题。
- **继续阅读源码**：可深入 `msnpureport/adcore`（HDC 客户端封装 `AdxDevCommShortLink`/`AdxGetDeviceFileTimeout` 等的实现）、`msnpureport/report/msnpureport_file_mgr.c`（导出文件的老化与轮转管理）、以及 `script/hal_log_collect_host.sh`（Host 侧 `/proc`、`/sys`、`dmesg` 一键采集脚本，是 report 之外的补充采集手段）。
