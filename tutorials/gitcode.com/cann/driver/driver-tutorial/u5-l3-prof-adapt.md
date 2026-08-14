# Profiling 性能采集适配

## 1. 本讲目标

本讲聚焦 DMC（设备维护组件）中的 **prof（Profiling，性能采集）** 子模块，回答一个核心问题：

> NPU 在设备侧（Device）持续产生的性能采样数据，是如何被搬运到主机侧（Host）、并被性能工具读走的？

读完本讲，你应当能够：

1. 说清 prof 模块的分层结构：对外接口层（`prof_interface.c`）、核心管理层（`prof_core.c` / `prof_chan.c`）、适配层（`prof_adapt.*`）、通信层（`comm/`）、缓冲层（`prof_buff.c`）各管什么。
2. 理解 **适配层（prof_adapt）** 的角色——它是一个「策略选择器 + 反向通知桥」，根据通道模式与连接形态，为每个性能通道挑选一套 `prof_chan_ops` 操作表，并把底层通信层收到的数据用回调方式送回核心层。
3. 掌握 **prof_hdc** 如何用 HDC 会话把 Host 与 Device 连起来，区分「控制面（同步请求/应答）」与「数据面（异步上报）」两条逻辑。
4. 理解 **prof_buff** 这个环形缓冲区的数据结构，以及它在「设备直接写主机内存」与「主机读出数据」之间的承接作用。

## 2. 前置知识

本讲承接 u5-l1（DMC 与 device_monitor 通路），并大量复用 u3-l2（HDC 通信模型）建立的概念。阅读前请确认你理解以下术语：

- **Host / Device**：主机侧（用户态进程）与设备侧（NPU 固件/内核）。
- **HDC（Host-Device Communication）**：驱动各模块共用的主机-设备消息底座，提供会话（session）、epoll 事件循环、`halHdcSend`/`halHdcRecv` 等原语（详见 u3-l2）。
- **UB（灵衢超节点）/ URMA**：UB 是一种高速互联形态；URMA 是其上的 RDMA 通信框架，允许设备直接读写主机内存（详见 u3-l5）。
- **通道（channel）**：prof 的核心抽象。一类性能数据（如 AICPU 采样、模块内存采样）对应一个通道，用 `chan_id` 标识（如 `CHANNEL_AICPU = 143`、`CHANNEL_NPU_MODULE_MEM = 142`，定义在 `ascend_hal_base.h`）。
- **函数指针表 / 虚表（ops）**：C 语言里实现多态的常用手法，一组函数指针聚合在 struct 里，不同实现填充不同函数，调用方只认 struct。
- **`drvError_t`**：HAL 层统一错误码类型，`DRV_ERROR_NONE`（即 0）表示成功（详见 u3-l1）。

一句话定位：prof 模块是「**采集设备性能数据 → 缓存到主机 → 交给性能工具**」的流水线，本讲拆解这条流水线上「适配、通信、缓冲」三段。

## 3. 本讲源码地图

prof 子模块位于 [`src/ascend_hal/dmc/prof/`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof)，目录按职责分层：

| 目录 / 文件 | 作用 |
|---|---|
| `prof_interface.c` | **对外接口层**。暴露 `hal*`/`prof_*` 公共 API（注册通道、启停、刷新、读、查询、上报、获取通道列表），是性能工具/Runtime 的入口。 |
| `core/prof_core.c` | **核心层**。把对外接口转交给通道管理，并在库加载时注册两组「反向通知」回调。 |
| `core/prof_chan.c` | **通道状态机**。每「设备×通道」一个管理结构，维护 `DISABLE→STARTING→ENABLE→FLUSH→STOPPING` 状态，调用 ops 完成具体动作。 |
| `adapt/prof_adapt.c` / `.h` | **适配层（本讲主角之一）**。根据通道模式与连接形态挑选 ops，并提供反向通知 notifier。 |
| `adapt/kernel/` | KERNEL 模式的具体适配：`prof_adapt_h2d.c`（按 UB/HDC 分发）、`prof_adapt_hdc.c`（HDC 通道 ops）、`prof_adapt_urma.c`（URMA 通道 ops）。 |
| `adapt/user/` | USER 模式适配：数据源是用户态注册的回调函数（定时线程采样）。 |
| `comm/hdc/prof_hdc.c` | **HDC 协议层（本讲主角之二）**。定义 prof 自有消息类型、构造/解析消息、同步等待应答。 |
| `comm/hdc/prof_hdc_comm.c` | HDC 传输层。负责 HDC 会话建链、epoll 收发线程、消息发送。 |
| `comm/urma/` | URMA 传输层（UB 形态专用）。 |
| `adapt/prof_buff.c` / `.h` | **环形缓冲（本讲主角之三）**。主机侧暂存采样数据的环形缓冲区。 |
| `prof_hdc_msg.h` | prof 消息结构与消息类型枚举（Host 与 Device 共享协议）。 |

本讲聚焦三个最小模块：**prof_adapt**（适配与分发）、**prof_hdc**（HDC 数据通路）、**prof_buff**（数据缓冲）。三者关系可以用一句话概括：

> **prof_adapt 决定「用哪条通路」、prof_hdc 负责「在 HDC 通路上搬消息」、prof_buff 负责「把搬来的数据先攒起来等主机来读」。**

## 4. 核心概念与源码讲解

在进入三个最小模块前，先用一张图建立整体数据流心智模型。一次完整的性能数据采集分为「控制面」和「数据面」两条线：

```
                       Host 用户态 (libascend_hal.so)
 ┌───────────────────────────────────────────────────────────────┐
 │  性能工具/Runtime                                              │
 │     │ halProfSampleRegister / prof_drv_start / prof_channel_read│
 │     ▼                                                          │
 │  prof_interface.c  ──►  prof_core.c  ──►  prof_chan.c           │
 │     （对外 API）        （转发）       （状态机 + 调用 ops）      │
 │                                              │                 │
 │                prof_adapt_get_chan_ops ◄──────┘ 选 ops          │
 │                          │                                     │
 │        ┌─────────────────┼──────────────────┐                  │
 │        ▼                 ▼                  ▼                  │
 │   USER ops           HDC ops            URMA ops  ◄── adapt 层  │
 │  (定时线程采样)      (prof_hdc +         (设备 RDMA 直写         │
 │                      epoll 收发)          主机内存)              │
 │        │                 │                  │                  │
 │        └─────────► prof_buff（环形缓冲）◄──────┘ 数据面汇入缓冲  │
 │                          │                                     │
 │     poll_report / chan_report 回调 ◄──── notifier（反向通知）   │
 └──────────────────────────┬──────────────────────────────────────┘
                            │ HDC session / ioctl / URMA
                            ▼
                       Device (NPU)
```

- **控制面（同步）**：Host 主动发 start/stop/flush/get_channels 等命令，Device 应答后才返回。对应 `prof_hdc.c` 里的 `prof_hdc_start` 等。
- **数据面（异步）**：Device 主动把采样数据推上来（HDC 路径）或直接写进主机内存（URMA 路径），数据最终落入 `prof_buff`，Host 性能工具用 `prof_channel_read` 取走。

### 4.1 prof_adapt：适配层与通道操作表分发

#### 4.1.1 概念说明

适配层要解决的核心问题是「**多形态、多数据源统一成同一套接口**」：

- **数据源不同**：有的性能数据来自设备内核（KERNEL 模式），有的来自主机用户态注册的回调（USER 模式）。
- **传输形态不同**：普通 PCIe 形态走 HDC 消息；UB 超节点形态走 URMA（设备可直接 RDMA 写主机内存）。
- **特殊约束**：机密计算（CC）模式下 prof 需要被禁用。

如果让上层核心直接面对这些差异，代码会塞满 `if-else`。prof 的做法是经典的 **适配器模式 + 策略模式**：定义一张统一的操作表 `struct prof_chan_ops`，为每种情形写一份实现（`prof_hdc_chan_ops`、`prof_urma_chan_ops`、`prof_user_chan_ops`），由适配层在通道启动时挑出正确的那一张。此后核心层只对着 `ops->start(...)`、`ops->read(...)` 编程，完全不关心背后是 HDC 还是 URMA。

此外，适配层还充当「**反向通知桥**」：底层通信层（如 HDC 收到一帧数据）不能直接调用核心层函数（那样会形成头文件循环依赖），而是通过适配层暴露的一个 notifier（一组函数指针）回调上去。核心层在库加载时把真正的实现填进 notifier。

#### 4.1.2 核心流程

prof 通道的「启动」流程能体现适配层的分发逻辑：

1. 性能工具调用 `prof_drv_start(dev_id, chan_id, ...)`。
2. 经 `prof_interface.c` → `prof_core_chan_start` → `prof_chan_start`（`prof_chan.c`）。
3. `prof_chan_start` 先做状态机检查（必须处于 `CHANNEL_DISABLE`），再调 `prof_chan_init`。
4. `prof_chan_init` 调用 **`prof_adapt_get_chan_ops`** 取得 ops，再调 `ops->init` 初始化通道私有数据。
5. 随后 `prof_chan_start` 调 `ops->start` 真正启动采集。

分发判定伪代码（简化自 `prof_adapt_get_chan_ops`）：

```
prof_adapt_get_chan_ops(dev_id, chan_mode, &ops, support_host_sample):
    if 是机密计算平台 且 当前处于 CC 模式:
        return PROF_NOT_SUPPORT          # 机密计算下禁用 prof
    if support_host_sample:
        ops = prof_user_get_host_sample_chan_ops()   # 主机采样
    elif chan_mode == PROF_CHAN_MODE_USER:
        ops = prof_user_get_chan_ops()               # 用户态回调数据源
    else:  # PROF_CHAN_MODE_KERNEL
        ops = prof_kernel_get_chan_ops(dev_id)       # 进一步按 UB/HDC 二选一
    return ops
```

而 KERNEL 模式内部还有一层「UB vs HDC」分发（在 `prof_adapt_h2d.c`）：查询设备的连接类型 `INFO_TYPE_HD_CONNECT_TYPE`，若为 `HOST_DEVICE_CONNECT_TYPE_UB`（值为 2）则走 URMA，否则走 HDC。

反向通知则由两个 notifier 完成：

- `prof_adapt_core_notifier`（适配层 ↔ 核心层）：含 `poll_report`（通知 poller 有数据可读）和 `chan_report`（把一帧数据上报给某通道）。
- `prof_comm_core_notifier`（通信层 ↔ 核心层）：含 `chan_start`/`chan_stop`/`chan_report`，由 `prof_communication.c` 持有。

#### 4.1.3 源码精读

先看统一的操作表定义。`struct prof_chan_ops` 列出了一个性能通道需要的全部动作（[prof_adapt.h:19-28](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.h#L19-L28)）：

```c
struct prof_chan_ops {
    drvError_t (*init)(uint32_t dev_id, uint32_t chan_id, bool event_flag, char **priv);
    void (*uninit)(char **priv);
    drvError_t (*start)(uint32_t dev_id, uint32_t chan_id, struct prof_user_start_para *para, char *priv);
    drvError_t (*stop)(uint32_t dev_id, uint32_t chan_id, struct prof_user_stop_para *para, char *priv);
    drvError_t (*flush)(uint32_t dev_id, uint32_t chan_id, uint32_t *data_len, char *priv);
    int (*read)(uint32_t dev_id, uint32_t chan_id, struct prof_user_read_para *para, char *priv);
    drvError_t (*query)(uint32_t dev_id, uint32_t chan_id, uint32_t *avail_len, char *priv);
    drvError_t (*report)(uint32_t dev_id, uint32_t chan_id, void *data, uint32_t data_len, char *priv);
};
```

注意每个动作都带一个 `char *priv`——这是该实现的「私有数据」指针（HDC 路径里它指向 `prof_buff` 缓冲区，URMA 路径里指向 `prof_urma_chan_priv`）。`init` 负责创建 priv，后续动作共享它，`uninit` 释放它。这就是 C 里「对象 + 方法」的写法：`priv` 是对象状态，ops 是方法表。

核心的「选 ops」逻辑在 [prof_adapt.c:46-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.c#L46-L71)：

```c
drvError_t prof_adapt_get_chan_ops(uint32_t dev_id, uint32_t chan_mode, struct prof_chan_ops **ops, bool support_host_sample)
{
#ifdef CFG_SOC_PLATFORM_CLOUD_V4
    HAL_CC_INFO cc_info = {0};
    int size = sizeof(HAL_CC_INFO);
    drvError_t ret = halGetDeviceInfoByBuff(dev_id, MODULE_TYPE_CC, INFO_TYPE_CC, &cc_info, &size);
    if (ret != DRV_ERROR_NOT_SUPPORT) {
        if (ret != 0) { ... return PROF_ERROR; }
        if (cc_info.cc_cfg_info.cc_mode == HAL_CC_MODE_NORMAL) {
            PROF_INFO("CPU is currently in confidential computing mode, and prof drv is disable.\n");
            return PROF_NOT_SUPPORT;
        }
    }
#endif
    if (support_host_sample) {
        return prof_user_get_host_sample_chan_ops(ops);
    }
    if (chan_mode == (uint32_t)PROF_CHAN_MODE_USER) {
        return prof_user_get_chan_ops(ops);
    } else {
        return prof_kernel_get_chan_ops(dev_id, ops);
    }
}
```

这段做了三件事：(1) 在机密计算平台（`CFG_SOC_PLATFORM_CLOUD_V4`）上查 CC 模式，若处于 CC 模式则禁用 prof；(2) `support_host_sample` 优先级最高，走主机采样 ops；(3) 否则按 USER/KERNEL 模式分流。注意 `prof_user_get_host_sample_chan_ops` 是个 **弱符号**（`__attribute__((weak))`，见 [prof_adapt.c:39-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.c#L39-L44)），默认返回 NULL——只有链接了主机采样实现的平台才会提供强定义，这是一种「可选特性」的解耦手法。

KERNEL 模式内的二次分发在 [prof_adapt_h2d.c:61-85](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_h2d.c#L61-L85)：

```c
drvError_t prof_kernel_get_chan_ops(uint32_t dev_id, struct prof_chan_ops **ops)
{
    int64_t h2d_type;
    drvError_t ret = halGetDeviceInfo(dev_id, MODULE_TYPE_SYSTEM, INFO_TYPE_HD_CONNECT_TYPE, &h2d_type);
    if (ret != DRV_ERROR_NONE) { ... return ret; }

    if (h2d_type == HOST_DEVICE_CONNECT_TYPE_UB) {
        if (urma_ops->get_chan_ops == NULL) { return DRV_ERROR_INNER_ERR; }
        return urma_ops->get_chan_ops(ops);     // UB → URMA ops
    } else {
        return prof_hdc_get_chan_ops(ops);      // 否则 → HDC ops
    }
}
```

这里有个精巧的注册机制：`urma_ops` 不是直接调用 `prof_urma_get_chan_ops`，而是经 `prof_h2d_regiser_urma_ops` 注册的函数指针。URMA 适配文件在自己的 `__attribute__((constructor))` 构造函数里把自己注册进去（见 [prof_adapt_urma.c:206-214](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_urma.c#L206-L214)）。这样 `prof_adapt_h2d.c` 不必静态依赖 URMA 的符号——URMA 模块没编进来时注册函数就不存在，`urma_ops` 保持 NULL，运行时给出明确错误而非链接失败。

再看「反向通知桥」。适配层持有一个全局 notifier（[prof_adapt.c:16](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.c#L16) 与 [prof_adapt.h:34-37](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.h#L34-L37)）：

```c
struct prof_adapt_core_notifier {
    void (*poll_report)(uint32_t dev_id, uint32_t chan_id);
    drvError_t (*chan_report)(uint32_t dev_id, uint32_t chan_id, void *data, uint32_t data_len, bool hal_flag);
};
```

核心层在库加载的构造函数里把真实实现填进去（[prof_core.c:115-130](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/core/prof_core.c#L115-L130)）：

```c
static void __attribute__((constructor)) prof_core_callback_register(void)
{
    struct prof_adapt_core_notifier adapt_notifier = {
        .poll_report = prof_poll_report,
        .chan_report = prof_chan_report,
    };
    ...
    prof_adapt_register_notifier(&adapt_notifier);
    prof_comm_register_notifier(&comm_notifier);
}
```

于是底层 HDC/URMA 代码要通知核心时，只需 `prof_adapt_get_notifier()->chan_report(...)`，不必 `#include` 核心层头文件。这是一个用「函数指针注册」打破分层依赖环的典型工程手法，和 u5-l1 里 device_monitor 的 `DM_INTF_S` 可插拔管道思想一致。

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，画出 prof_adapt 对一个通道的「分发决策树」，并验证 KERNEL 模式下 UB 与非 UB 走向不同代码。

**操作步骤**：

1. 打开 [prof_adapt.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_adapt.c)，定位 `prof_adapt_get_chan_ops`（L46），列出它可能返回的 4 种 ops（host_sample / user / kernel-hdc / kernel-urma）。
2. 打开 [prof_adapt_h2d.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_h2d.c)，确认 KERNEL 模式里 `halGetDeviceInfo(dev_id, MODULE_TYPE_SYSTEM, INFO_TYPE_HD_CONNECT_TYPE, &h2d_type)` 决定了走 URMA 还是 HDC。
3. 在 `pkg_inc/ascend_hal_base.h` 中搜索 `HOST_DEVICE_CONNECT_TYPE_UB`，确认其值为 `2`（即只有该取值才走 URMA）。
4. 打开 [prof_adapt_urma.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_urma.c) 的构造函数（L206），确认 URMA ops 是「自注册」进 `prof_h2d_regiser_urma_ops` 的。

**需要观察的现象**：分发逻辑是「两层」的——第一层在 `prof_adapt.c` 按 chan_mode/support_host_sample 分，第二层在 `prof_adapt_h2d.c` 按 h2d_type 分。

**预期结果**：你应得到一棵决策树，根节点是 `prof_adapt_get_chan_ops`，叶子是三张具体 ops 表（`prof_user_chan_ops`、`prof_hdc_chan_ops`、`prof_urma_chan_ops`）。

> 待本地验证：若手头有 NPU 环境，可运行性能采集工具（如 `msprof`）并用 `strace` 观察是否出现 HDC 相关 syscall；UB 形态设备与非 UB 设备的内核模块加载列表会不同。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prof_user_get_host_sample_chan_ops` 要声明为 `__attribute__((weak))` 且默认返回 NULL？

**参考答案**：主机采样（host sample）是一种「可选特性」，并非所有平台/编译配置都编入它的实现。声明为弱符号后，未编入时链接器用默认的弱定义（返回 NULL/0），调用方据此判断「不支持」；编入时强符号覆盖弱定义。这样适配层代码无需改动就能适配「有/无主机采样」两种构建，避免硬依赖导致的链接错误。

**练习 2**：如果要在 prof 里新增一种传输方式（比如未来的某总线），最少要改动哪里？

**参考答案**：写一份新的 `prof_chan_ops` 实现（init/uninit/start/stop/flush/read/query/report 八个函数），再用类似 `prof_h2d_regiser_urma_ops` 的注册函数在构造函数里把自己挂进去，并在 `prof_kernel_get_chan_ops` 的分发条件里加一个分支判断。核心层（`prof_chan.c`）、接口层（`prof_interface.c`）完全不需要改动——这正是 ops 虚表 + 注册机制带来的可扩展性。

---

### 4.2 prof_hdc：HDC 数据通路与控制/数据双平面

#### 4.2.1 概念说明

`prof_hdc.c` 是「普通 PCIe 形态」下 prof 的通信协议层。它在 HDC 这条消息底座之上，定义了 prof 自己的应用层协议：一组消息类型（[prof_hdc_msg.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/prof_hdc_msg.h)）、消息的构造与解析、以及「发命令后等应答」的同步语义。

理解 prof_hdc 的关键是把它的消息分成两类：

- **控制类消息**（`PROF_HDC_CMD_GET_CHANNEL` / `PROF_HDC_CMD_START` / `PROF_HDC_CMD_STOP` / `PROF_HDC_DATA_FLUSH`）：Host 发出后，**同步阻塞等待** Device 的应答。应答携带 `ret_val`，Host 用它判断成功与否。
- **数据类消息**（`PROF_HDC_DATA`）：Device **异步主动推送**，携带一段采样数据。Host 收到后写入缓冲，不需要逐帧应答。

这种「控制面同步、数据面异步」的二分，是高性能采集系统的常见设计——控制命令低频但需要确认结果，采样数据高频且容忍尽力交付。

#### 4.2.2 核心流程

消息结构体（Host 与 Device 共享的协议契约）定义在 [prof_hdc_msg.h:33-41](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/prof_hdc_msg.h#L33-L41)：

```c
struct prof_hdc_msg {
    int msg_type;        // 消息类型（enum prof_hdc_msg_type）
    int ret_val;         // 应答返回值
    uint32_t cmd_verify; // get_channels 用的命令序号（防串扰）
    uint32_t channel_id; // 所属通道
    uint32_t data_len;   // data[] 有效长度
    uint32_t rsv;
    unsigned char data[]; // 柔性数组，承载 payload
};
```

消息类型枚举见 [prof_hdc_msg.h:15-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/prof_hdc_msg.h#L15-L23)（`PROF_HDC_CMD_GET_CHANNEL` 到 `PROF_HDC_CMD_MAX`）。注意末尾的 `data[]` 是 C99 柔性数组成员——消息头是定长，payload 紧跟其后，长度由 `data_len` 说明。这就是 u5-l1 里讲过的「带柔性数组的消息头」模式在 prof 里的又一次复用。

**控制面同步等待**的机制是「信号量配对」：Host 发命令前初始化一个信号量（初值 0），发完消息后进入 `prof_hdc_get_ret` 用 `sem_trywait` 轮询（每 500us 一次，直到超时）；Device 的应答消息到达后，接收路径会 `sem_post` 唤醒等待者。每条通道有独立的信号量（`g_prof_hdc_chan[dev_id][chan_id]`），互不干扰。

**数据面异步上报**的机制是 epoll 事件循环：一个专门的接收线程 `prof_epoll_client_recv`（在 `prof_hdc_comm.c`）阻塞在 `drvHdcEpollWait` 上，一旦某条 HDC 会话有数据可读就被唤醒，调用 `halHdcRecv` 取出消息，再交给 `prof_hdc_msg_proc` 按 `msg_type` 分发。

一条 HDC 会话（session）的建链是「按需」且「按设备共享」的：同一个 `dev_id` 的多个通道共用一条 session（用引用计数 `prof_channel_num_count` 管理），第一个通道启动时建链并加入 epoll，最后一个通道停止时拆除。

#### 4.2.3 源码精读

先看控制面。`prof_hdc_start` 是「发 start 命令并等应答」的典型实现（[prof_hdc.c:475-497](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L475-L497)）：

```c
drvError_t prof_hdc_start(uint32_t dev_id, uint32_t chan_id, struct prof_user_start_para *para)
{
    struct process_sign sign_info = {0};
    drvError_t ret;

    ret = drvGetProcessSign(&sign_info);          // 1. 取进程签名（鉴权/进程标识）
    if (ret != DRV_ERROR_NONE) { ... return ret; }

    ret = prof_hdc_chan_sem_init(dev_id, chan_id); // 2. 初始化该通道的应答信号量
    if (ret != DRV_ERROR_NONE) { return ret; }

    ret = prof_hdc_start_msg_send(dev_id, chan_id, para); // 3. 构造并发送 START 消息
    if (ret != DRV_ERROR_NONE) { return ret; }

    return prof_hdc_get_ret(dev_id, chan_id, PROF_HDC_RESPOND_MAXTIME, PROF_HDC_CMD_START); // 4. 等应答
}
```

四步非常清晰：鉴权 → 备信号量 → 发消息 → 等应答。`prof_hdc_stop`（[prof_hdc.c:499-514](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L499-L514)）、`prof_hdc_flush`（[prof_hdc.c:516-531](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L516-L531)）结构完全一样，只是换了消息类型与超时时长（stop/flush 等待时间更长，因为要等 Device 把残留数据刷完）。

发消息的构造细节见 `prof_hdc_start_msg_send`（[prof_hdc.c:205-242](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L205-L242)）：它 `calloc` 一块 `sizeof(prof_hdc_msg) + sizeof(prof_hdc_start_para)` 的内存，填好 `msg_type=PROF_HDC_CMD_START`、`channel_id`，把采样周期等参数拷进 `data[]`，再调 `prof_hdc_msg_send` 发出。值得注意的细节：当 `para->remote_pid == 0`（非跨进程场景）时才下发 `sample_period`，否则置 0——跨进程采样由另一套 event 机制驱动周期。

同步等待的核心在 `prof_hdc_get_ret`（[prof_hdc.c:135-178](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L135-L178)），它用 `sem_trywait` + `usleep(500)` 循环模拟带超时的等待（标准 `sem_timedwait` 在某些平台语义不稳，这里手搓轮询更可控）。超时后会走 `destroy_session` 拆掉这条 session 以求自愈：

```c
STATIC drvError_t prof_hdc_get_ret(uint32_t dev_id, uint32_t chan_id, int timeout, uint32_t msg_type)
{
    sem_t *sem = NULL;
    uint32_t wait_count = 0;
    uint32_t wait_count_max = (uint32_t)(timeout * 1000000 / 500);  // 超时换算成 500us 次数
    ...
    ret = sem_trywait(sem);
    while (ret != 0) {
        if (wait_count == wait_count_max) { ... goto destroy_session; }  // 超时
        (void)usleep(500);
        wait_count++;
        ret = sem_trywait(sem);
    }
    ...
destroy_session:
    ...
    drv_ret = prof_session_destroy(dev_id, chan_id);  // 超时自愈：拆 session
    return DRV_ERROR_WAIT_TIMEOUT;
}
```

再看数据面与消息分发入口。`prof_hdc_msg_proc` 是所有从 HDC 收到的 prof 消息的总入口（[prof_hdc.c:389-416](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L389-L416)），它就是一个 `switch(msg_type)`：

```c
void prof_hdc_msg_proc(uint32_t dev_id, void *msg, uint32_t len)
{
    ...
    switch (msg_head->msg_type) {
        case PROF_HDC_CMD_GET_CHANNEL:
            prof_hdc_receive_channels(dev_id, msg_head, len);   // 应答：通道列表
            break;
        case PROF_HDC_CMD_START:
        case PROF_HDC_CMD_STOP:
        case PROF_HDC_DATA_FLUSH:
            prof_hdc_receive_chan_respond(dev_id, msg_head);    // 应答：唤醒等待者
            break;
        case PROF_HDC_DATA:
            prof_hdc_receive_chan_data(dev_id, msg_head, len);  // 数据：上报
            break;
        default:
            PROF_ERR("Profile detected an unknown msg. ...\n");
            return;
    }
}
```

`prof_hdc_msg_proc` 本身不直接被 HDC 调用，而是在库加载的构造函数里注册成回调（[prof_hdc.c:533-537](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L533-L537)）：

```c
STATIC void __attribute__((constructor)) prof_hdc_proc_init(void)
{
    prof_hdc_register_msg_proc_func(prof_hdc_msg_proc);
    PROF_INFO("Register prof hdc msg proc successfully.\n");
}
```

`prof_hdc_register_msg_proc_func` 把这个函数指针存进全局 `g_hdc_msg_proc_fuc`（[prof_hdc_comm.c:27-32](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L27-L32)）。这样 `prof_hdc_comm.c`（传输层）在 epoll 线程里收到消息后，调 `g_hdc_msg_proc_fuc(...)` 就能回到协议层 `prof_hdc_msg_proc`，又是「函数指针注册」解耦。

数据帧的处理见 `prof_hdc_receive_chan_data`（[prof_hdc.c:369-387](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L369-L387)）：校验长度后，调用 **通信层 notifier** 的 `chan_report` 把数据送上去：

```c
STATIC void prof_hdc_receive_chan_data(uint32_t dev_id, struct prof_hdc_msg *msg_head, uint32_t total_len)
{
    struct prof_comm_core_notifier *notifier = prof_comm_get_notifier();
    ...
    if (msg_head->data_len > 0) {
        notifier->chan_report(dev_id, msg_head->channel_id, msg_head->data, msg_head->data_len, false);
    }
}
```

这个 `chan_report` 最终会路由到该通道 ops 的 `report` 方法（HDC 实现见下一节），把数据写进缓冲。至此数据面闭环：**Device 推 `PROF_HDC_DATA` → epoll 线程收 → `prof_hdc_msg_proc` 分发 → `chan_report` 回调 → ops.report → 写缓冲**。

最后看一眼 HDC 会话的「按设备共享 + 引用计数」管理。`prof_hdc_msg_send`（[prof_hdc_comm.c:481-503](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L481-L503)）每次发送前都调 `prof_session_connect`，后者在 `prof_per_session_connect`（[prof_hdc_comm.c:308-352](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L308-L352)）里判断：该 `dev_id` 若已有 session 且计数 > 0，只做 `prof_channel_num_count[dev_id]++`；否则才真正 `drvHdcSessionConnect` 建链、加入 epoll。对应拆除在 `prof_session_destroy`（[prof_hdc_comm.c:460-479](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L460-L479)），计数归零时才 close。epoll 接收线程 `prof_epoll_client_recv`（[prof_hdc_comm.c:208-252](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L208-L252)）在会话数归零时自行退出，释放 epoll 与 client。

#### 4.2.4 代码实践

**实践目标**：用源码阅读法，跟踪一条 `prof_hdc_start` 命令从「发」到「收应答」的完整时序，并对比一条 `PROF_HDC_DATA` 数据帧的异同。

**操作步骤**：

1. 从 [prof_hdc.c:475](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc.c#L475) 的 `prof_hdc_start` 出发，依次跳读：`prof_hdc_chan_sem_init`（L122）→ `prof_hdc_start_msg_send`（L205）→ `prof_hdc_msg_send`（prof_hdc_comm.c:481）→ `prof_session_msg_send`（prof_hdc_comm.c:373，真正调 `halHdcSend`）。
2. 然后看「等应答」：`prof_hdc_get_ret`（L135），注意 `sem_trywait` 的轮询节奏（500us）与超时自愈（`prof_session_destroy`）。
3. 切换到接收侧：[prof_hdc_comm.c:208](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L208) 的 `prof_epoll_client_recv` → `prof_handle_hdc_events`（L89，调 `halHdcRecv`）→ `prof_hdc_call_msg_proc_func`（L34）→ 回到 `prof_hdc_msg_proc`（prof_hdc.c:389）。
4. 对比 `PROF_HDC_CMD_START`（走 `prof_hdc_receive_chan_respond`，`sem_post` 唤醒等待者）与 `PROF_HDC_DATA`（走 `prof_hdc_receive_chan_data`，调 `chan_report` 写缓冲，**没有** `sem_post`）。

**需要观察的现象**：控制类应答会触发 `sem_post` 解除 `prof_hdc_get_ret` 的阻塞；数据类消息不触发任何信号量，而是把数据塞进缓冲后调 `poll_report` 通知 poller。

**预期结果**：你能画出一张时序图，左半是 start 命令的「请求—信号量等待—应答唤醒」，右半是数据帧的「epoll 收—分发—写缓冲—通知 poller」，两者复用同一条 HDC session 但走不同分支。

> 待本地验证：上述流程无须运行即可由源码确认；若要在真机观察，可在 `prof_hdc_msg_proc` 的 `switch` 各分支临时加 `PROF_INFO` 日志（仅学习用途，勿提交），用性能工具触发采集后查看驱动日志确认消息类型与通道号。

#### 4.2.5 小练习与答案

**练习 1**：`prof_hdc_get_ret` 为什么用 `sem_trywait` + `usleep` 轮询，而不是直接用 `sem_timedwait`？

**参考答案**：作者用「固定 500us 步长的轮询 + 累计次数上限」来精确控制等待节奏与超时，并在超时时能干净地走到 `destroy_session` 做自愈（拆掉可能已经卡死的 session）。这种手搓轮询在不同 POSIX 实现上行为更一致、超时边界更可控，也便于在超时分支统一插入会话回收逻辑。

**练习 2**：同一个 `dev_id` 上同时启动 3 个不同通道，会建立几条 HDC session？

**参考答案**：1 条。`prof_per_session_connect` 用 `prof_channel_num_count[dev_id]` 做引用计数：第一个通道建立 session 并加入 epoll，后两个通道只做 `count++` 和 `prof_channel_enable_flag[dev_id][chan_id]=1`。反之 `prof_session_destroy` 时每停一个通道 `count--`，归零才真正 close。这样避免了每通道一条 session 的资源浪费。

---

### 4.3 prof_buff：环形缓冲区管理

#### 4.3.1 概念说明

`prof_buff.c` 实现了一个 **无锁友好的环形缓冲区（ring buffer）**，用来在主机用户态暂存采样数据。它的存在解决两个工程问题：

1. **生产/消费解耦**：Device 推数据的速率 ≠ Host 性能工具读数据的速率。缓冲区吸收这种速率差，避免 Device 因 Host 读得慢而被反压或丢数据。
2. **跨路径共享读写指针**：URMA 路径下，Device 直接 RDMA 写缓冲区的数据区，并读取 Host 的 `read_ptr` 来知道「Host 已经读到哪了」；Host 读完后更新 `read_ptr`。这种「Device 写数据 + 共享读写指针」要求缓冲区布局固定、指针地址可暴露。

环形缓冲（也叫循环缓冲）的核心思想是：一块定长内存，写指针写到末尾后「翻转」（wrap）回到开头继续写，读指针跟上，只要写指针不追上读指针（即不满），数据就不会被覆盖。有效数据量与空闲量随两指针差值动态变化。

#### 4.3.2 核心流程

缓冲区的内存布局由头部结构体 `prof_data_head` 决定（[prof_buff.c:21-26](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L21-L26)）：

```c
#define PROF_DATA_HEAD_RSV_LENGTH 1021
struct prof_data_head {
    uint32_t read_ptr;     // 读指针（Host 消费位置）
    uint32_t write_ptr;    // 写指针（生产位置）
    uint32_t data_buf_len; // 数据区长度（= 总长 - 头大小）
    uint32_t rsv[PROF_DATA_HEAD_RSV_LENGTH];  /* align 4kb */
};
```

3 个 `uint32_t` 头字段 + 1021 个保留 `uint32_t`，合计 1024 个 `uint32_t` = 4096 字节，正好把头部对齐到 **4KB**（一个内存页）。这个对齐非常关键：URMA 路径里 Device 做的是整页 RDMA 映射，头部与数据区一起按页对齐，Device 才能稳定地读到 `read_ptr`。数据区紧跟头部之后，长度 = 缓冲总长 − 头大小。

缓冲区默认 4MB（`0x400000`），AICPU 通道翻倍到 8MB（因为 AICPU 采样数据量更大），见 `prof_buff_init`（[prof_buff.c:28-54](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L28-L54)）。

读写逻辑用「相对位置」管理，两指针都在 `[0, data_buf_len)` 范围内循环。判空与判满的规则：

- **空**：`read_ptr == write_ptr`。
- **满**：刻意保留 1 字节间隙——`prof_buff_get_avail_len` 返回 `data_buf_len - data_len - 1`（[prof_buff.c:178-184](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L178-L184)）。即写指针最多追到离读指针 1 字节处停下，这样「满」与「空」的状态不会都表现为两指针相等而产生歧义。这是环形缓冲的经典设计（牺牲 1 字节换取状态可区分）。

有效数据量 `prof_buff_get_data_len`（[prof_buff.c:165-176](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L165-L176)）：

\[
\text{data\_len} =
\begin{cases}
\text{write\_ptr} - \text{read\_ptr}, & \text{write\_ptr} \ge \text{read\_ptr} \\
\text{data\_buf\_len} - \text{read\_ptr} + \text{write\_ptr}, & \text{write\_ptr} < \text{read\_ptr}
\end{cases}
\]

写操作 `prof_buff_write`（[prof_buff.c:64-100](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L64-L100)）处理「跨尾翻转」：若剩余空间不够一次写下，先写到末尾，再把 `write_ptr` 翻回 0 继续写剩余部分。读操作 `prof_buff_read`（[prof_buff.c:119-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L119-L163)）分三种情形：空（返回 0）、`read < write`（一次读）、`read > write`（分段读：先读到末尾，再从头续读）。

#### 4.3.3 源码精读

`prof_buff_init`（[prof_buff.c:28-54](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L28-L54)）用 `posix_memalign` 申请 4KB 对齐的内存，清零后把头部解释成 `prof_data_head`，初始化两个指针为 0：

```c
drvError_t prof_buff_init(uint32_t chan_id, uint8_t **buff)
{
    uint8_t *buff_addr = NULL;
    struct prof_data_head *data_head = NULL;
    uint32_t buff_len = 0x400000; /* 0x400000: 4MB */
    size_t alignment = 0x1000;    /* align 4kb */
    ...
    if (chan_id == CHANNEL_AICPU) {
        buff_len = 0x800000;       /* AICPU 通道 8MB */
    }
    ret = posix_memalign((void**)&buff_addr, alignment, buff_len);
    ...
    data_head = (struct prof_data_head *)buff_addr;
    data_head->write_ptr = 0;
    data_head->read_ptr = 0;
    data_head->data_buf_len = buff_len - (uint32_t)sizeof(struct prof_data_head);  // 数据区 = 总长 - 头
    *buff = buff_addr;
    return DRV_ERROR_NONE;
}
```

写操作处理翻转的关键片段（[prof_buff.c:76-89](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L76-L89)）：

```c
if (dest_len < src_len) {
    ret = memcpy_s(dest_base, dest_len, src_base, dest_len);  // 先写满到末尾
    ...
    src_base += dest_len;
    src_len -= dest_len;
    /* write_ptr flip */
    ATOMIC_SET(&data_head->write_ptr, 0);                      // 翻转回 0
    dest_base = (uint8_t *)data_head + sizeof(struct prof_data_head);
    dest_len = data_head->data_buf_len;
}
ret = memcpy_s(dest_base, dest_len, src_base, src_len);        // 写剩余部分
...
ATOMIC_SET(&data_head->write_ptr, (data_head->write_ptr + src_len) % data_head->data_buf_len);
```

注意指针更新一律用 `ATOMIC_SET`（底层是 `__sync_lock_test_and_set`，见 [prof_common.h:45](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/prof_common.h#L45)），保证 URMA 路径下 Device 读到的指针值是完整的、不会读到「写了一半」的中间态。

缓冲区与外部（Device/URMA）的交互接口是几个 getter（[prof_buff.c:186-219](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L186-L219)）：

```c
void *prof_buff_get_buf_addr(uint8_t *buff) { return (void *)(buff + sizeof(struct prof_data_head)); }      // 数据区起始地址
void *prof_buff_get_readptr_addr(uint8_t *buff) { return (void *)&data_head->read_ptr; }                    // 读指针地址（暴露给 Device）
uint32_t prof_buff_get_writeptr(uint8_t *buff) { return data_head->write_ptr; }                            // 读 Device 写入的写指针
void prof_buff_update_writeptr(uint8_t *buff, uint32_t write_ptr) { ATOMIC_SET(&data_head->write_ptr, write_ptr); }
```

`prof_buff_get_readptr_addr` 是理解 URMA 路径的钥匙：它把 Host 缓冲区里 `read_ptr` 字段的 **地址** 暴露出去。在 `prof_adapt_urma.c` 的 `prof_urma_chan_start` 里，这个地址被传给设备（[prof_adapt_urma.c:76-79](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_urma.c#L76-L79)），于是 Device RDMA 写完数据后，能反过来读 Host 的 `read_ptr` 判断空闲量，避免覆盖未读数据。HDC 路径则不需要这个，因为数据是经消息拷贝进来的（见 `prof_hdc_chan_report`，[prof_adapt_hdc.c:130-146](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_hdc.c#L130-L146)）：

```c
static drvError_t prof_hdc_chan_report(unsigned int dev_id, unsigned int chan_id, void *data, unsigned int data_len, char *priv)
{
    uint8_t *buff = (uint8_t *)priv;
    struct prof_adapt_core_notifier *notifier = NULL;
    drvError_t ret;

    ret = prof_buff_write(buff, data, data_len);   // 数据写入环形缓冲
    ...
    notifier = prof_adapt_get_notifier();
    notifier->poll_report(dev_id, chan_id);         // 通知 poller：有数据可读了
    return DRV_ERROR_NONE;
}
```

注意这里 `priv` 就是缓冲区指针——回想 4.1.3 里 `prof_hdc_chan_init`（[prof_adapt_hdc.c:30-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_hdc.c#L30-L44)）调 `prof_buff_init` 把 buff 存进 `*priv`。于是 HDC 通道的私有数据就是一个环形缓冲区，整个 ops 表围绕它读写。

最后看一个「优雅停止」的细节：`prof_buff_wait_read_empty`（[prof_buff.c:221-246](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L221-L246)）在通道停止时被调用（见 `prof_hdc_chan_stop`、`prof_urma_chan_stop`），它先发一次 `poll_report` 催 Host 赶紧把残留数据读走，然后轮询 `prof_buff_get_data_len` 直到为空或超时（最多 1000 次 × 1ms = 1 秒）。这保证了 stop 之后缓冲区里不会遗留未消费的数据。

```c
void prof_buff_wait_read_empty(uint8_t *buff, uint32_t dev_id, uint32_t chan_id)
{
    struct prof_adapt_core_notifier *notifier = prof_adapt_get_notifier();
    uint32_t data_len, wait_num = 0;

    data_len = prof_buff_get_data_len(buff);
    if (data_len == 0) { return; }
    notifier->poll_report(dev_id, chan_id);              // 催 Host 读
    while ((data_len != 0) && (wait_num < 1000)) {       /* 1000 */
        (void)usleep(1000); /* 1000 us */
        wait_num++;
        data_len = prof_buff_get_data_len(buff);
    }
    ...
}
```

#### 4.3.4 代码实践

**实践目标**：理解环形缓冲区的读写指针演化，并能用一段独立的示例代码验证 `prof_buff_write` / `prof_buff_read` 的行为（不依赖 NPU 环境）。

**操作步骤**：

1. 阅读 [prof_buff.c:64-100](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L64-L100)（write）与 [prof_buff.c:119-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/prof_buff.c#L119-L163)（read），手算一个例子：`data_buf_len = 8`，依次 write 3 字节、read 2 字节、write 7 字节，跟踪 `read_ptr` 与 `write_ptr` 的变化（注意翻转）。
2. 验证「满」的判定：连续 write 直到 `prof_buff_get_avail_len` 返回 0，观察此时 `write_ptr` 是否恰好停在 `read_ptr - 1`（模意义下）。
3. 下面是一段 **示例代码**（非项目原有代码，仅用于在普通 Linux 上验证环形缓冲语义；需要链接项目里的 `prof_buff.c` 或自行复制其实现）：

```c
/* 示例代码：独立验证 prof_buff 读写语义 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "prof_buff.h"   /* 假设已把 prof_buff.c/h 加入编译 */

int main(void)
{
    uint8_t *buff = NULL;
    /* 注意：prof_buff_init 内部固定申请 4MB/8MB，无法指定小尺寸；
       因此本示例仅验证“写后读、再写触发翻转”的行为，不验证具体指针值。 */
    if (prof_buff_init(CHANNEL_NPU_MODULE_MEM, &buff) != 0) {
        printf("init failed\n");
        return 1;
    }
    printf("buf_size=%u\n", prof_buff_get_buf_size(buff));

    char payload[] = "hello-prof";
    prof_buff_write(buff, payload, sizeof(payload));
    printf("after write: data_len=%u avail=%u\n",
           prof_buff_get_data_len(buff), prof_buff_get_avail_len(buff));

    char out[64] = {0};
    int n = prof_buff_read(buff, out, sizeof(out));
    printf("read %d bytes: %s\n", n, out);
    printf("after read: data_len=%u\n", prof_buff_get_data_len(buff));

    prof_buff_uninit(&buff);
    return 0;
}
```

**需要观察的现象**：写入后 `data_len` 等于写入字节数，`avail` 相应减少；读出后 `data_len` 归零，读到的内容与写入一致。

**预期结果**：示例输出大致为 `data_len` 写后增、读后归零，验证了环形缓冲「先入先出」与「写—读抵消」的语义。

> 待本地验证：`prof_buff_init` 内部用固定大小申请内存，若想验证「翻转」需写入超过剩余空间的数据量（4MB 量级），可在循环里写满后再读一部分再写，观察 `prof_buff_get_data_len` 是否正确反映跨翻转的数据量。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prof_data_head` 要用 `rsv[1021]` 把头部撑到整整 4KB？

**参考答案**：为了让「头部 + 数据区」整体按 4KB（一个内存页）对齐。URMA 路径下 Device 对这块内存做的是按页 RDMA 映射，只有头部与数据区都落在整页边界上，Device 才能稳定地读到 `read_ptr`（头部里）并按页写数据区。`rsv` 是用「预留填充」实现的对齐手段。

**练习 2**：`prof_buff_get_avail_len` 为什么返回 `data_buf_len - data_len - 1` 而不是 `data_buf_len - data_len`？少这 1 字节的意义是什么？

**参考答案**：环形缓冲必须能区分「空」与「满」。若允许写满到 `write_ptr == read_ptr`，则该状态与「空」完全相同，产生歧义。刻意少用 1 字节，让「满」时 `write_ptr` 停在 `(read_ptr - 1) mod len`，与「空」（`write_ptr == read_ptr`）区分开。这是用 1 字节空间换状态可判定性的经典取舍。

**练习 3**：HDC 路径和 URMA 路径分别如何更新 `write_ptr`？

**参考答案**：HDC 路径里数据是 Host 自己经 `prof_buff_write` 拷进缓冲的，`write_ptr` 在 `prof_buff_write` 内部随拷贝推进而更新。URMA 路径里数据是 Device 直接 RDMA 写进数据区的，Host 不主动写缓冲；Device 通过另一条消息把新的 `write_ptr` 告诉 Host，Host 在 `prof_urma_chan_report` 里用 `prof_buff_update_writeptr` 写入这个值（见 [prof_adapt_urma.c:168-187](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_urma.c#L168-L187)）。两种路径都靠 `read_ptr`（Host 持有）与 `write_ptr` 的差值判断数据量。

## 5. 综合实践

把三个模块串起来，完成一次「**端到端数据通路跟踪**」。

**任务**：以「HDC 路径下一帧采样数据从 Device 到达 Host 性能工具」为线索，写一份调用链文档，要求标注每一跳所在的文件、函数与行号，并解释 prof_buff 在其中的位置。

**建议步骤**：

1. **入口假设**：Device 通过 HDC session 推送了一帧 `PROF_HDC_DATA` 消息。
2. **接收侧**（prof_hdc 通信层 → 协议层）：从 [prof_hdc_comm.c:208](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/hdc/prof_hdc_comm.c#L208) `prof_epoll_client_recv` 起，经 `prof_handle_hdc_events`（L89）→ `halHdcRecv` → `prof_hdc_call_msg_proc_func`（L34）→ `prof_hdc_msg_proc`（prof_hdc.c:389），定位到 `PROF_HDC_DATA` 分支调 `prof_hdc_receive_chan_data`（prof_hdc.c:369）。
3. **跨层回调**（协议层 → 核心层）：`prof_hdc_receive_chan_data` 调 `prof_comm_get_notifier()->chan_report`（prof_hdc.c:385），该指针由 `prof_core.c:115` 的构造函数注册，指向 `prof_chan_report`（prof_chan.c:357）。
4. **回到 ops**（核心层 → HDC 适配）：`prof_chan_report` 调 `chan_mng->ops->report`（prof_chan.c:385），即 HDC 实现的 `prof_hdc_chan_report`（prof_adapt_hdc.c:130）。
5. **入缓冲**（HDC 适配 → buff）：`prof_hdc_chan_report` 调 `prof_buff_write`（prof_buff.c:64）把数据写入环形缓冲，再调 `notifier->poll_report` 通知 poller。
6. **消费侧**（性能工具读）：性能工具调 `prof_channel_read`（prof_interface.c:213）→ `prof_core_chan_read` → `prof_chan_read`（prof_chan.c:297）→ `ops->read` = `prof_hdc_chan_read`（prof_adapt_hdc.c:112）→ `prof_buff_read`（prof_buff.c:119）把数据拷出。

**产出**：一张含 6 跳的调用链表，每跳标注文件:行号与一句话职责。在「入缓冲」那一跳旁注明：**prof_buff 的作用是把异步到达、速率不均的采样数据先攒起来，等性能工具按自己的节奏来读**——这就是它在整条链路里的缓冲解耦价值。

## 6. 本讲小结

- prof 模块按「接口层 → 核心层 → 适配层 → 通信层 → 缓冲层」分层，核心层只认统一的 `prof_chan_ops` 操作表，不感知具体传输方式。
- **prof_adapt** 是「策略选择器 + 反向通知桥」：`prof_adapt_get_chan_ops` 按通道模式（USER/KERNEL）、是否主机采样、连接形态（UB/HDC）挑出正确的 ops；notifier 机制让底层能回调核心而不引入头文件循环依赖。
- KERNEL 模式内部还有一层分发：UB 形态走 URMA（设备 RDMA 直写主机内存），非 UB 走 HDC（消息收发）；URMA 通过构造函数自注册，解耦编译。
- **prof_hdc** 把通信分成「控制面同步、数据面异步」：start/stop/flush/get_channels 用信号量阻塞等应答（超时自愈拆 session），采样数据走 epoll 线程异步上报；同一设备的多个通道共享一条 HDC session（引用计数）。
- **prof_buff** 是 4KB 对齐的环形缓冲（默认 4MB，AICPU 8MB），头部含 read_ptr/write_ptr；HDC 路径由 Host 自己 `prof_buff_write`，URMA 路径由 Device 直写数据区、Host 仅更新 write_ptr；两种路径都靠读写指针差值管理数据量。
- 整条数据链贯穿「epoll 收 → msg_proc 分发 → chan_report 回调 → ops.report → buff 写入 → poll_report 通知 → 性能工具 buff_read 读出」，prof_buff 是其中吸收速率差、解耦生产与消费的关键。

## 7. 下一步学习建议

- **横向对比通信底座**：回看 u3-l2（HDC client/server/core 模型）与 u5-l1（device_monitor 的 `DM_INTF_S` 可插拔管道），体会 prof 的 ops 虚表 + notifier 注册与它们在「分层解耦」思想上的同构性。
- **URMA 路径深入**：本讲侧重 HDC 路径，URMA 仅做对比。建议接着阅读 [`comm/urma/prof_urma.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/comm/urma/prof_urma.c) 与 [`prof_adapt_urma.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/adapt/kernel/prof_adapt_urma.c)，理解 `prof_urma_post_recv`、远端 read_ptr 写回等机制，需要结合 u3-l5（comm/urma 适配）的 URMA 基础概念。
- **通道状态机**：若关心启停时序与并发，可精读 [`core/prof_chan.c`](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/prof/core/prof_chan.c) 的 `enum prof_channel_state` 五态机与 `prof_chan_start`/`prof_chan_stop` 的状态迁移。
- **上层用法**：本讲止于 `libascend_hal.so` 内部。若想看性能数据如何被上层消费，可继续学习 CANN 的 Profiling 工具（如 msprof）如何调用本讲提到的 `halProfSampleRegister`/`prof_drv_start`/`prof_channel_read` 等接口。
