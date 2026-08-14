# DMC 设备维护组件与 device_monitor 通路

## 1. 本讲目标

本讲进入 HAL 层的「设备维护」专题。学完本讲后，你应当能够：

- 说清 **DMC（Device Maintenance Components，设备维护组件）** 的组成，以及 `device_monitor` 在其中的定位。
- 理解 `device_monitor` 提供的**通用消息框架**：一套与传输无关的「请求/响应」抽象（`DM_INTF_S` / `DM_CB_S`），以及挂在它下面的多种传输（HDC、UDP、selfloop 等）。
- 读懂 **dm_hdc** 如何把这套消息框架架到 HDC（主机-设备通信）之上，完成「Host ↔ Device」的报文收发。
- 读懂 **dev_mon_dmp_client** 如何在消息框架之上做 **DMP 报文的分片（slice）与重组（reassemble）**，把超大报文拆成多帧安全传输。
- 把本讲的 `device_monitor` 通路，和单元 2 学过的 DSMI（`dsmi_init` → DMP 命令）、单元 3 学过的 HDC（`halHdcSend`/`halHdcRecv`）串成一条完整的调用链。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**直觉一：什么是 DMP 报文？**
在 [u2-l2](u2-l2-dsmi-interface-impl.md) 里我们见过，DSMI 向设备下发命令用的是一套「设备管理协议（Device Management Protocol，DMP）」报文：一个报文 = 报文头（`op_fun`/`op_cmd`/`offset`/`length` 等字段）+ 命令负载。本讲要回答的问题是：**这些 DMP 报文，到底是经过哪条「管道」送到设备、设备的响应又是怎么回来的？** 答案就是 `device_monitor` 这条通路。

**直觉二：为什么要「与传输无关」的消息框架？**
DMP 报文可以走不同的物理管道送到设备：可以走 HDC（主机到设备的内核消息通道，[u3-l2](u3-l2-hdc-communication.md)），也可以走 UDP（socket）、selfloop（本机回环，用于测试），甚至 SMBus/IAM。如果每换一种管道就要把「发请求、等响应、超时重传」的逻辑重写一遍，代码会重复且难维护。于是 `device_monitor` 把「**收发流程**」和「**具体管道**」拆开：

- **收发流程**（`dm_msg_intf.c`）：统一的「发请求 → 进待答队列 → 收响应 → 配对回调」框架，谁实现都一样。
- **具体管道**（`dm_hdc.c` / `dm_udp.c` / `dm_loop.c`）：只负责「把字节搬过去 / 把字节搬回来」，用一组函数指针（`send_msg`/`recv_msg`）接入框架。

这和 [u3-l2](u3-l2-hdc-communication.md) 里 HDC 自己用 `g_hdcConfig` 做 PCIe/Socket/UB 分发是同一思想，只是层次更高：HDC 是「消息原语」层，`device_monitor` 是建在 HDC 之上的「业务报文」层。

**直觉三：为什么需要分片与重组？**
单帧报文有长度上限（`max_trans_len`，受 HDC 单段最大长度 `capacity.maxSegment` 与 `DM_MSG_DATA_MAX=4096` 约束）。当一条 DMP 命令的负载超过单帧上限时，必须**拆成多帧发送**，到对端再**拼回完整报文**。`dev_mon_dmp_client.c` 就是干这件事的客户端。

> 关键术语速查：**DMP**（设备管理协议报文）、**DM_INTF_S**（消息接口，一组函数指针）、**DM_CB_S**（消息控制块，含 poller 与三张链表）、**poller**（事件循环，基于 poll/epoll 监听 fd）、**pending list**（已发未答的请求队列）、**msgid**（用请求结构体地址当配对键）、**slice**（分片）、**reassemble**（重组）。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `src/ascend_hal/dmc/` 下：

| 文件 | 作用 |
| --- | --- |
| `dmc/CMakeLists.txt` | DMC 的子模块装配清单，一眼看清 DMC 由哪些组件构成。 |
| `device_monitor/include/device_monitor.h` | `device_monitor` 对外暴露的少量管理类接口（管理队列任务、时间同步）。 |
| `device_monitor/lib/include/dm_common.h` | **消息框架的核心类型**：`DM_INTF_S`、`DM_CB_S`、`PENDING_REQ_T`、地址类型宏、`DM_MSG_ST` 等。 |
| `device_monitor/lib/msg/dm_msg_intf.c` | **通用消息框架实现**：`dm_init`、`dm_send_req`、收发分发、待答队列与超时重传。 |
| `device_monitor/lib/msg/dm_hdc.c` | **HDC 传输适配**：把消息框架架到 HDC 会话之上。 |
| `device_monitor/include/dm_hdc.h` | HDC 传输相关结构（`HDC_MSG_ST`、`DM_HDC_ADDR_ST`）与 `dm_hdc_init` 声明。 |
| `device_monitor/msg/dev_mon_dmp_client.c` | **DMP 客户端**：报文分片、响应重组、对外入口 `dev_mon_send_request`。 |
| `device_monitor/include/device_monitor_type.h` | DMP 报文头 `DEV_MP_MSG_ST`、分片控制块 `SEND_CTL_CB` 等类型。 |
| `dmc/dsmi/dsmi_common/dsmi_common.c` | **集成点**：`dsmi_init` 调 `dm_init`+`dm_hdc_init` 装配通路，DSMI 命令经 `dev_mon_send_request` 下发。 |

> 阅读建议：先看 `dm_common.h` 建立类型心智模型 → 再看 `dm_msg_intf.c` 理解框架 → 再看 `dm_hdc.c` 理解一种管道实现 → 最后看 `dev_mon_dmp_client.c` 理解分片层。本讲三个最小模块正是按这个顺序展开。

## 4. 核心概念与源码讲解

### 4.1 device_monitor：DMC 的通用消息框架

#### 4.1.1 概念说明

DMC（Device Maintenance Components）是 HAL 层里负责「设备维护」的一组组件。从装配清单 [dmc/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/CMakeLists.txt) 可以一眼看清它的组成：

| 子模块 | 职责（一句话） |
| --- | --- |
| `device_monitor` | 提供 DMP 报文的通用消息收发框架与多种传输适配（本讲主角）。 |
| `dsmi` | 设备系统管理接口（DSMI）实现，[u2-l2](u2-l2-dsmi-interface-impl.md) 已讲。 |
| `logdrv` | 日志驱动，打通 Host/Device 日志通路（[u5-l2](u5-l2-logdrv-and-msnpureport.md) 详讲）。 |
| `prof` / `prof_sample` | 性能采集（Profiling）适配层。 |
| `verify_tool` | 设备维测/校验工具。 |

其中 `device_monitor` 是**公共底座**：DSMI、logdrv、prof 等要把报文送到设备时，都复用它提供的消息框架，而不是各自再造一套收发逻辑。从 `device_monitor.h` 也能看出它还承担少量后台管理职责：

[device_monitor.h:13-22](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/include/device_monitor.h#L13-L22) — 声明了管理队列任务 `create_management_queue_task` 与时间同步 `dmp_start_time_sync` 等后台接口。但本讲聚焦它的核心价值：**消息通路**。

`device_monitor` 的「通用消息框架」由两个关键类型撑起，都定义在 [dm_common.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/include/dm_common.h)：

- **`DM_INTF_S`（消息接口）**：一组函数指针，代表「一种管道怎么收发」[dm_common.h:112-141](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/include/dm_common.h#L112-L141)。关键字段：`recv_msg`（怎么收）、`send_msg`（怎么发）、`close`（怎么关）、`rfd`/`wfd`（供 poller 监听的读/写 fd）、`max_trans_len`（单帧上限）。HDC、UDP、selfloop 各写一份这组函数指针的实现，就接入了框架。
- **`DM_CB_S`（消息控制块）**：一个消息「域」的总管 [dm_common.h:148-154](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/include/dm_common.h#L148-L154)，含一个 `intf_poller`（事件循环）和三张链表：`intf_list`（已注册的接口清单）、`pending_list`（已发未答的请求）、`cmd_reg_list`（本端作为服务端时注册的命令处理函数）。

> 一句话：`DM_CB_S` 是「调度中枢」，`DM_INTF_S` 是「可插拔的管道」，框架代码只跟这两个抽象打交道，不关心底下是 HDC 还是 UDP。

#### 4.1.2 核心流程

框架的初始化由 `dm_init` 完成，它建好中枢并启动事件循环：

1. `malloc` 出 `DM_CB_S`。
2. `poller_create` + `poller_run`：创建并启动事件循环（基于 poll，监听各接口的 `rfd`）。
3. 创建三张链表：`intf_list`、`pending_list`、`cmd_reg_list`。
4. `selfloop_init`：注册一个「回环接口」，用于本机内发响应给自己（超时回包等场景）。

发请求 `dm_send_req` 的流程（这是框架最核心的路径）：

1. `__dm_pending_req_add`：把请求挂进 `pending_list`，并启动一个 poller 定时器（带超时与重试次数）。
2. **msgid 的小技巧**：把待答请求结构体 `PENDING_REQ_T` 的**地址**当作 `msgid` 传给 `send_msg`。等响应回来时，响应里也带回这个地址，框架用它直接定位是哪条请求——省去了维护自增序号表。
3. 调 `intf->send_msg(...)`：交给具体管道把字节发出去。

收响应的流程（由 poller 的事件回调驱动）：

1. poller 监测到某接口 `rfd` 可读 → 调 `__dm_recv`。
2. `__dm_recv` 调该接口的 `intf->recv_msg` 把字节读出来，填到 `DM_RECV_ST`。
3. `__dm_msg_handle` 按 `recv_type` 分发：`RESPONSE_MSG`（响应）→ `__dm_rsp_handle`；`REQUEST_MSG`（对端主动发来的请求）→ `__dm_cmd_handle`。
4. `__dm_rsp_handle` 用响应里的 `msgid` 在 `pending_list` 找到原请求，调用其 `rsp_hndl` 回调，再删除该 pending 项与定时器。

超时与重传：若定时器先于响应到期，`__dm_timeout_handle` 看是否还有重试次数：有就重发并重置定时器；没有就经 `timeout_hndl` 填一个超时响应（典型值 `0xC3`），走 selfloop 接口回送给等待者。

#### 4.1.3 源码精读

**框架初始化 `dm_init`** —— 建中枢、起 poller、建三表、注册 selfloop：

[dm_msg_intf.c:637-710](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L637-L710) — 关键是先 `poller_create`/`poller_run` 起事件循环，再用 `list_create` 建 `intf_list`/`pending_list`/`cmd_reg_list` 三张表，最后 `selfloop_init` 注册回环接口；任何一步失败都 `goto out` 调 `dm_destroy` 回滚。

**接口注册 `dm_intf_register`** —— 把一条管道接入框架，同时把它的读 fd 交给 poller：

[dm_msg_intf.c:712-734](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L712-L734) — 先 `list_append` 把接口加入 `intf_list`，再 `poller_fd_add(cb->intf_poller, intf->rfd, POLLIN, __dm_recv, ...)`：**让 poller 监听这个接口的可读事件，回调统一指向 `__dm_recv`**。这就是「框架统一收口」的关键——无论哪种管道，数据到达都走同一个 `__dm_recv`。

**发请求 `dm_send_req`** —— 加 pending、记 msgid、调管道发送：

[dm_msg_intf.c:755-796](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L755-L796) — 注意 `tmpptr = (intptr_t)p_req`（待答请求的地址）随后作为 `msgid` 传给 `intf->send_msg`；同时这里还顺手把报文头里的 `opcode` 解出来打日志，方便排查「哪条命令发出错」。

**收包总入口 `__dm_recv`** —— poller 回调，读包并分发：

[dm_msg_intf.c:418-471](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L418-L471) — 调 `intf->recv_msg` 读出报文到 `st_recv`，成功后 `__dm_msg_handle` 分发；并用 `clock_gettime` 给「单包处理耗时」做统计（超阈值累计告警）。

**响应/请求分发 `__dm_msg_handle`**：

[dm_msg_intf.c:382-398](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L382-L398) — 简单的 switch：响应走 `__dm_rsp_handle`，请求走 `__dm_cmd_handle`。

**响应配对 `__dm_rsp_handle`** —— msgid 即地址：

[dm_msg_intf.c:305-341](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L305-L341) — 注释写得很清楚：「把 `PENDING_REQ_T` 的地址当 msgid 发出去，响应回来时就能凭 msgid 找回它」。找到后调原请求注册的 `rsp_hndl`，再 `__dm_pending_req_del` 清理。

**超时重传 `__dm_timeout_handle`**：

[dm_msg_intf.c:43-107](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L43-L107) — `retries > 0` 时重发并重置定时器；`retries == 0` 时经 `timeout_hndl` 造超时响应，走 selfloop 接口回送。

#### 4.1.4 代码实践（源码阅读型）

> 实践目标：验证「框架与传输解耦」这一设计，并找到 DSMI 是在哪里把 HDC 管道接入框架的。

操作步骤：

1. 打开 [dsmi_common.c:1165-1163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L1147-L1163)，找到 `dsmi_init`。
2. 观察它先调 `dm_init((DM_CB_S **)&g_dm_cb)` 建好中枢（[dsmi_common.c:1177](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L1177)）。
3. 再在 `CFG_FEATURE_DMP_HDC` 宏保护下调 `dm_hdc_init(...)`（[dsmi_common.c:1155](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L1155)），把 HDC 这根管道接进去。
4. 注意同一函数里还有 `CFG_FEATURE_DMP_UDP` → `dm_udp_init`、`IAM_CONFIG` → `dm_iam_init` 的并列分支：**同一套框架，按编译宏换不同管道**，这就是解耦的直观证据。

需要观察的现象 / 预期结果：

- `dm_init` 与 `dm_hdc_init` 是两次独立调用：前者建框架，后者填管道。换芯片/产品形态时（`build.sh --soc` 决定的特性宏），接入的管道可能不同，但框架代码不变。
- 如果你本地能编译（`bash build.sh --pkg --soc=ascend910b`），可在编译产物里 grep `dm_hdc_init`/`dm_udp_init` 是否被链接进 `libascend_hal.so`，以此判断当前形态启用了哪种管道。**若不具备编译环境，标注「待本地验证」**。

#### 4.1.5 小练习与答案

**练习 1**：`DM_INTF_S` 里的 `rfd`/`wfd` 是给谁用的？为什么框架需要它们？
**答案**：给 `DM_CB_S` 里的 `intf_poller`（事件循环）用。poller 监听 `rfd` 的可读事件来触发 `__dm_recv`。因为不同管道（HDC 会话、UDP socket）的「数据到达」方式不同，框架用一个统一的 fd（接口自己准备，比如 HDC 用 pipe、UDP 用 socket fd）把「到达通知」归一化，poller 才能用同一套逻辑监听所有接口。

**练习 2**：为什么 `dm_send_req` 用「请求结构体的地址」当 msgid，而不是用一个自增整数序号？
**答案**：用地址做 msgid，响应回来时可以 O(1) 直接拿到指向 `PENDING_REQ_T` 的指针（`req = (PENDING_REQ_T *)(uintptr_t)precv->msgid`），无需维护「序号 → 请求」的查找表，也避免了序号回绕、并发分配等问题。代价是 msgid 只在本进程内有意义——但 DMP 报文本来就是本端框架内部的配对键，不需要跨进程全局唯一。

---

### 4.2 dm_hdc：把消息框架架到 HDC 之上

#### 4.2.1 概念说明

`dm_hdc.c` 是「HDC 传输适配层」：它实现 4.1 里那组 `DM_INTF_S` 函数指针，把抽象的「收/发」落到 HDC 的会话原语上。回忆 [u3-l2](u3-l2-hdc-communication.md)：HDC 提供 `drvHdcClientCreate`/`drvHdcServerCreate`（建客户端/服务端）、`drvHdcSessionConnect`/`drvHdcSessionAccept`（建/接受会话）、`halHdcSend`/`halHdcRecv`（发/收消息）这套原语。`dm_hdc` 就是建在这些原语之上的「业务层」。

它要解决一个关键的**模型适配问题**：

- HDC 的收是**会话级、阻塞式**的——你拿着一个 `HDC_SESSION` 调 `halHdcRecv` 阻塞等数据；
- 而框架的 poller 是 **fd 级、事件驱动**的——它只认 fd 的可读事件。

两者怎么对接？`dm_hdc` 的答案是**用一根 pipe 当桥**：起专用线程做阻塞式 `halHdcRecv`，收到数据后写进 pipe 的写端；poller 监听 pipe 的读端，一可读就用 `recv_msg` 把数据读走。这样就把「阻塞会话接收」适配成了「fd 事件」。

`dm_hdc` 有两种角色，由地址里的 `hdc_type` 决定（[dm_common.h:54](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/include/dm_common.h#L54)，`DMP_SERVER=1`）：

- **客户端（`hdc_type=0`）**：主动 `drvHdcSessionConnect` 连到对端设备，发请求、收响应。Host 侧的 DSMI 就是这个角色（见 [dsmi_common.c:1151](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L1151) 设 `hdc_type = 0`）。
- **服务端（`hdc_type=1`）**：`drvHdcServerCreate` 监听，`drvHdcSessionAccept` 接受连接，处理对端主动发来的请求。

#### 4.2.2 核心流程

**初始化 `dm_hdc_init`**：

1. 校验地址类型必须是 `DM_HDC_ADDR_TYPE`/`DM_HDC_CHANNEL`。
2. `drvHdcGetCapacity` 查 HDC 单段最大长度，算出本接口的 `max_trans_len`（扣掉 dm 自己的报文头 `HDCMSG_HEAD_SIZE`）。
3. `__dm_hdc_open`：建 pipe；按 `hdc_type` 建 HDC client 或 server；若是 server，起一个 accept 线程。
4. 填好函数指针（`recv_msg=__dm_hdc_recv`、`send_msg=__dm_hdc_send`、`close=__dm_hdc_close` 等），调 `dm_intf_register` 把自己挂进框架。

**发送（客户端路径）`__dm_hdc_send` → `__dm_hdc_client_send`**：

1. 按 `hdc_type` 分发到 client 或 server 发送。
2. 客户端：`dm_hdc_session_connect` 连会话（带重试，处理「对端还没 listen」「无可用 session」等情况）→ `dm_hdc_send_msg`（`halHdcSend`）发出 → `__dm_session_recv_proc`（`halHdcRecv`）同步收响应 → 写进 pipe → 关会话。

**接收（poller 路径）`__dm_hdc_recv`**：

1. poller 监到 pipe 可读 → 从 pipe 读出 `HDC_MSG_ST`（先读定长头 `HDCMSG_HEAD_SIZE`，再读变长 `data`）。
2. 把源地址、msgid、会话属性等填进 `DM_RECV_ST`，框架随即分发。

**服务端收请求（后台线程）`__dm_server_accept_proc` → `__dm_server_recv_msg_proc`**：

1. accept 线程循环 `drvHdcSessionAccept`，每接到一个会话就 `pthread_create` 起一个 detached 工作线程（限流：全局 `g_hdc_thread_num` 上限 `HDC_ACCEPT_THREAD_MAX=1024`）。
2. 工作线程做 `__dm_session_recv_proc`（`halHdcRecv`），把收到的报文写进 pipe，由 poller 接力。

数据帧格式（[dm_hdc.c:868](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L868) 注释）：

```
| dm: hdc msg head (HDC_MSG_ST 定长头) | dm msg head | dm cmd payload |
```

即一层 HDC 包装头 + DMP 业务数据。`max_trans_len = 单段最大长度 - HDCMSG_HEAD_SIZE`，这正是 4.3 分片要遵循的单帧上限。

#### 4.2.3 源码精读

**`dm_hdc_init`** —— 建接口、装函数指针、注册：

[dm_hdc.c:808-899](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L808-L899) — 关键几行：`drvHdcGetCapacity(&capacity)` 查能力（[L835](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L835)）；算 `max_trans_len`（[L869-L874](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L869-L874)）；装回调 `intf->recv_msg = __dm_hdc_recv; intf->send_msg = __dm_hdc_send; ...`（[L875-L882](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L875-L882)）；最后 `dm_intf_register` 接入框架（[L885](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L885)）。

**`__dm_hdc_open`** —— 建 pipe、按角色建 client/server、server 起 accept 线程：

[dm_hdc.c:323-405](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L323-L405) — `pipe(fds)` 建桥并把两端设为非阻塞 + `FD_CLOEXEC`（[L337-L343](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L337-L343)）；客户端调 `drvHdcClientCreate(&client, MAX_HDC_CLIENT, HDC_SERVICE_TYPE_DMP, 0)`（[L359](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L359)）——注意业务类型是 `HDC_SERVICE_TYPE_DMP`，这正是设备侧把 DMP 报文和其他 HDC 业务区分开的「频道号」；服务端调 `drvHdcServerCreate` 并起 `__dm_server_accept_proc` 线程（[L370-L401](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L370-L401)）。

**`__dm_hdc_send` / 客户端发送** —— 分发 + 连接 + 发 + 收：

[dm_hdc.c:600-614](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L600-L614) 是分发入口；客户端实现 [dm_hdc.c:500-531](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L500-L531)：`dm_hdc_session_connect` → `dm_hdc_send_msg` → `__dm_session_recv_proc`，三步走完「发-收」后 `drvHdcSessionClose`。

**`dm_hdc_send_msg`** —— 组 HDC 报文 + `halHdcSend`（带超时重试）：

[dm_hdc.c:443-498](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L443-L498) — `__format_hdc_msg` 把业务数据装进 `HDC_MSG_ST`（填 `peer_devid`/`src_devid`/`msgid`/`data`）；`drvHdcAllocMsg`+`drvHdcAddMsgBuffer` 组 HDC 消息描述符；`halHdcSend` 发送，遇 `DRV_ERROR_WAIT_TIMEOUT` 最多重试 `DMHDC_CLIENT_SEND_TIMEOUT_RETRY_TIMES=3` 次。

**会话连接 `dm_hdc_session_connect`** —— 先探活再连，带差异化重试：

[dm_hdc.c:407-441](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L407-L441) — 先 `drvGetDmpStarted` 探测对端 DMP 服务是否就绪；`drvHdcSessionConnect` 失败时，针对 `REMOTE_NOT_LISTEN`/`DEVICE_NOT_READY`（重试 40 次，每次 sleep 1s）与 `REMOTE_NO_SESSION`（重试 20 次，每次 sleep 1ms）分别处理——体现了「设备启动有先后，要等它 listen 起来」的工程现实。

**服务端 accept 循环与工作线程**：

[dm_hdc.c:268-320](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L268-L320)（accept）与 [dm_hdc.c:236-266](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L236-L266)（工作线程）——每会话一线程做 `__dm_session_recv_proc`，写完 pipe 后线程结束、会话关闭；用 `__sync_fetch_and_add/sub` 维护全局线程计数，`hdc_thread_num_limit` 在超限时 `usleep` 限流。

**poller 侧接收 `__dm_hdc_recv`** —— 从 pipe 读回 `HDC_MSG_ST`：

[dm_hdc.c:689-774](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L689-L774) — 先读定长头 `HDCMSG_HEAD_SIZE`（[L708](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L708)），校验 `data_len` 合法后再读变长 `data`（[L723](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L723)）；若是服务端还会 `dm_get_session_propery` 从会话属性取出对端的运行环境（物理机/容器/虚拟机）与 uid、vfid，用于权限判定（[L745-L750](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L745-L750)）。

#### 4.2.4 代码实践（源码阅读型）

> 实践目标：看清「pipe 桥」如何把 HDC 的阻塞式会话接收适配成 poller 的 fd 事件。

操作步骤：

1. 读 [dm_hdc.c:236-266](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L236-L266)（`__dm_server_recv_msg_proc`）与 [dm_hdc.c:165-214](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L165-L214)（`__dm_session_recv_proc`）：工作线程里 `halHdcRecv` 阻塞收到一帧后，调 `__hdc_write_to_pipe`（[dm_hdc.c:91-118](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L91-L118)）写入 pipe 写端 `intf->wfd`。
2. 再读 [dm_hdc.c:689-741](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_hdc.c#L689-L741)（`__dm_hdc_recv`）：从 pipe 读端 `intf->rfd`（即 `fd`）把同一帧读出来。
3. 回看 4.1 的 `dm_intf_register`：注册时 `poller_fd_add(cb->intf_poller, intf->rfd, POLLIN, __dm_recv, ...)`——poller 监听的就是这根 pipe 的读端。

需要观察的现象 / 预期结果：

- 画出数据流：`halHdcRecv`(工作线程) → `__hdc_write_to_pipe` → **pipe** → poller 触发 `__dm_recv` → `__dm_hdc_recv` → 框架分发。pipe 正是「线程模型」与「事件模型」的黏合剂。
- 思考：为什么 pipe 两端要设 `O_NONBLOCK`？因为 poller 的事件循环不能被一个慢读者阻塞，写端非阻塞 + 失败计数（`pipe_wr_fail`）能在管道满时优雅告警而非卡死。**实际运行行为待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：客户端发送 `__dm_hdc_client_send` 里，`halHdcSend` 之后为什么紧接着调 `__dm_session_recv_proc`（同步收）？既然同步收了，poller 那套又有什么用？
**答案**：客户端是「一发一收」的阻塞式请求模型——发完就在同一线程里 `halHdcRecv` 等响应，收到后写入 pipe，再由 poller 侧的 `__dm_hdc_recv` 读出并走框架的响应配对（`__dm_rsp_handle` 调业务回调）。也就是说，「同步收」负责把 HDC 会话里的字节捞出来塞进 pipe；poller 负责把响应配对回原请求并驱动业务回调。两者分工不同：前者是「取数」，后者是「派发」。

**练习 2**：`HDC_SERVICE_TYPE_DMP` 这个常量在 `drvHdcClientCreate`/`drvHdcServerCreate` 调用里起什么作用？
**答案**：它是 HDC 的「业务频道号」。HDC 是各模块共用的通信底座（见 [u3-l2](u3-l2-hdc-communication.md)），用 `serviceType` 把不同业务（DMP、prof、其他）的会话隔开，避免报文串台。`device_monitor` 用 `HDC_SERVICE_TYPE_DMP` 表明自己走的是「DMP 管理报文」这一频道。

---

### 4.3 dev_mon_dmp_client：DMP 报文的分片与重组

#### 4.3.1 概念说明

`dev_mon_dmp_client.c` 是消息框架之上的一层「客户端增强」。框架本身的 `dm_send_req` 只处理「单帧请求-单帧响应」；但 DMP 命令的负载可能远超单帧上限 `max_trans_len`（[4.2](#42-dmhdc把消息框架架到-hdc-之上) 里由 HDC 能力算出）。这层就负责：

- **分片（slice）**：发送时把一条大报文拆成多帧，每帧 ≤ `max_trans_len`，按序发出。
- **重组（reassemble）**：接收时把对端分多帧返回的大响应拼回完整报文。
- **拉取（read-more）**：响应没传完时，自动更新 `offset`/`length` 重发请求，把剩余部分「拉」回来。

DMP 报文头 `DEV_MP_MSG_ST`（[device_monitor_type.h:81-89](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/include/device_monitor_type.h#L81-L89)）里有专门支撑分片的字段：

- `op_fun` / `op_cmd`：功能号 / 命令号，标识这是哪条命令（也用作重组时的哈希键）。
- `offset` / `length`：本帧负载在完整报文里的偏移与本帧负载长度。
- `lun` 的 bit7（`0x80`）：首/末帧标志（`SET_BIT(lun,7)` 置位，`CLR_BIT(lun,7)` 清除）。

分片控制块 `SEND_CTL_CB`（[device_monitor_type.h:135-145](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/include/device_monitor_type.h#L135-L145)）用 `next` 串成链表，记录每一片的地址、报文、回调，并带一个 `split_msg_node` 挂到全局 `g_slice_msg_list` 上便于生命周期管理。

#### 4.3.2 核心流程

**发送入口 `dev_mon_send_request`**：

1. `slice_msg`：把大报文切成 N 片，串成 `SEND_CTL_CB` 链表，每片填好 `offset`/`length` 与首末帧标志。
2. 对第 1 片调 `dm_send_req(...)`，回调注册为 `comm_msg_handle`。
3. 后续片在响应回来时按需续发。

分片数计算（当需要分片时）：

\[
\text{count} = \left\lfloor \frac{\text{data\_len} - \text{HEAD\_LEN} - 1}{\text{new\_data\_len}} \right\rfloor + 1
\]

其中 `new_data_len = max_trans_len - DEV_MON_REQUEST_HEAD_LEN`，即每片能装的最大业务负载。`-1` 再 `+1` 是为了保证「恰好整除」时不会少算一片。

**响应到达 `comm_msg_handle`**（作为 `dm_send_req` 的回调被框架调用）：

1. 若响应里 `err_code != 0`（对端报错）：直接调用户的 `rsp_hndl` 回调把错误传上去，清理该片。
2. 若当前片是末片（`p->next == NULL`）：调 `dmp_msg_recv_resp` 做重组判定。
3. 若还有下一片：释放当前片，对下一片调 `dm_send_req` 续发。

**重组 `dmp_msg_recv_resp`**：

1. 用 `op_fun`/`op_cmd` 拼 key（`"tag.<op_fun>.<op_cmd>"`）在哈希表 `g_client_rsp_hashtable` 里累计本命令的响应数据。
2. 若累计长度 `== total_length + 报文头`：收齐了——拼成完整报文，调用户 `rsp_hndl`，清理哈希项。
3. 若没收齐：更新请求里的 `offset`/`length`，重新 `dm_send_req` 拉取下一段。
4. 若超长（累计 `>` 应有长度）：非法，清哈希项报错。

> 注意 SMBus 旁路：若响应来自 SMBus 通道（`addr_type==DM_SMBUS_ADDR_TYPE`），不走分片重组，直接回调——见 [dev_mon_dmp_client.c:379-385](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L379-L385)。

#### 4.3.3 源码精读

**对外入口 `dev_mon_send_request`**：

[dev_mon_dmp_client.c:663-685](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L663-L685) — 参数校验 → `slice_msg` 切片 → 对 `cb_head`（第 1 片）调 `dm_send_req(..., comm_msg_handle, &cb_head, ...)`。注意它把 `&cb_head`（指针的指针）当 `user_data` 传下去，回调里据此找回这条分片链。

**分片 `slice_msg`**：

[dev_mon_dmp_client.c:499-612](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L499-L612) — 关键点：
- 不需分片时（`msg->data_len <= max_trans_len`）置 `count=1` 并 `SET_BIT(data->lun, 7)` 标记「单片即全帧」（[L515-L518](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L515-L518)、[L548](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L548)）。
- 需要分片时循环建 `SEND_CTL_CB`，每片 `memcpy_s` 拷对应区段，设置 `offset = i*new_data_len`、`length = new_data_len`，中间片 `CLR_BIT(lun,7)`、末片 `SET_BIT(lun,7)`（[L556-L605](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L556-L605)）。
- `SET_CTL_CB` 宏（[dev_mon_dmp_client.c:43-62](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L43-L62)）统一填充每片控制块的地址/回调/用户数据。

**响应回调 `comm_msg_handle`**：

[dev_mon_dmp_client.c:289-363](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L289-L363) — 从 `user_data` 取回分片链指针 `p`；`ob->err_code != 0` 时直接回调并清链（[L327-L336](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L327-L336)）；末片走 `dmp_msg_recv_resp`（[L341](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L341)）；非末片续发下一片（[L348-L358](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L348-L358)）。

**重组 `dmp_msg_recv_resp`**：

[dev_mon_dmp_client.c:365-461](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L365-L461) — `client_resp_hash_insert` 按 key 累计（[L390-L391](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L390-L391)）；判定收齐（`value->data_len == ob->total_length + DDMP_CMD_RESP_HEAD_LEN`）后重组完整报文并回调（[L402-L436](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L402-L436)）；未收齐则改 `req->offset`/`req->length` 重发拉取（[L441-L460](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L441-L460)）。

**哈希累计 `client_resp_hash_insert`**：

[dev_mon_dmp_client.c:179-287](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L179-L287) — 已存在同 key 则「刷新」：把新数据追加到旧数据后（注意扣掉一份重复的响应头 `DDMP_CMD_RESP_HEAD_LEN`），并更新 `data_length`（[L198-L230](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L198-L230)）；不存在则新建并 `hash_table_put2` 入表（[L233-L286](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L233-L286)）。

**初始化**：`slice_msg_list_init` / `client_rsp_hashtable_init`（[dev_mon_dmp_client.c:614-661](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L614-L661)）分别建分片链表与响应哈希表，供上述流程使用。

#### 4.3.4 代码实践（源码阅读型）

> 实践目标：把「DSMI 下发一条 DMP 命令」到「device_monitor 把它送到 HDC」的完整调用链走通，并指出各子模块共享的消息基础设施。

操作步骤（自上而下跟踪）：

1. **DSMI 侧**：打开 [dsmi_common.c:609](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/dsmi/dsmi_common/dsmi_common.c#L609)，DSMI 的命令下发调用 `dev_mon_send_request(g_dsmi_intf, &dest_addr, ..., &(dmp->send_msg), dsmi_msg_recev, ...)`——`dsmi_msg_recev` 就是用户侧响应回调。
2. **分片层**：进入 [dev_mon_dmp_client.c:663](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/msg/dev_mon_dmp_client.c#L663)，`slice_msg` 切片后对首片调 `dm_send_req`。
3. **框架层**：进入 [dm_msg_intf.c:755](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/dmc/device_monitor/lib/msg/dm_msg_intf.c#L755)，加 pending、记 msgid，调 `intf->send_msg`。
4. **HDC 传输层**：`send_msg` 实为 `__dm_hdc_send` → `__dm_hdc_client_send` → `dm_hdc_send_msg`（`halHdcSend`），报文最终经 HDC（[u3-l2](u3-l2-hdc-communication.md)）送达设备。
5. **响应回程**：设备回包 → 工作线程 `halHdcRecv` → pipe → poller → `__dm_hdc_recv` → 框架 `__dm_rsp_handle`（按 msgid 配对）→ 调 `comm_msg_handle` → `dmp_msg_recv_resp` 重组 → 最终回调 DSMI 的 `dsmi_msg_recev`。

需要观察的现象 / 预期结果：

- **共享基础设施**：DMC 各子模块（DSMI、logdrv、prof 等）共享同一套 `dm_*` 框架（`DM_CB_S` + poller + pending/cmd 链表）与同一组传输适配（`dm_hdc`/`dm_udp`/`selfloop`）。它们不直接碰 HDC，而是经 `device_monitor` 这层统一收口。
- 用一张时序图把上面 5 步画出来，标注每一步所在的文件与函数。**完整运行验证需 NPU 硬件与已部署驱动，本地无设备时标注「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：`slice_msg` 里 `lun` 的 bit7（`0x80`）什么时候置位、什么时候清除？
**答案**：当一帧就是完整报文（无需分片，单片）时置位，表示「这一帧即是首也是末」；需要分片时，中间帧清除该位，只有**最后一帧**置位。对端据此判断「是否已收到完整报文的最后一帧」，从而决定是直接处理还是继续等待/拉取。

**练习 2**：重组时为什么用 `op_fun`+`op_cmd` 作哈希 key，而不是用 msgid？
**答案**：响应的累计是按「哪条命令」聚合的——同一条命令可能分多帧返回，这些帧共享相同的 `op_fun`/`op_cmd`，所以用它做 key 能把同一命令的多个响应片段归到一处。msgid 是框架层「请求-响应」一对一配对用的（一次 `dm_send_req` 一次回调），而重组是「把同一条命令的多帧负载拼起来」，粒度不同，故用命令号做 key。

## 5. 综合实践

**任务：绘制 device_monitor 消息通路的完整架构图，并标注「解耦点」。**

请完成以下子任务：

1. **画三层架构图**：从上到下分三层——
   - 业务层：DSMI（`dsmi_common.c`）、logdrv、prof 等 DMC 子模块；
   - 框架层：`dm_msg_intf.c`（`DM_CB_S` + poller + pending/cmd 链表 + `dm_send_req`/`__dm_recv`）；
   - 传输层：`dm_hdc.c`、`dm_udp.c`、`dm_loop.c`（selfloop）三种 `DM_INTF_S` 实现，底下接 HDC/socket/回环。
2. **标出三个解耦点**：
   - 业务层与框架层之间：经 `dev_mon_send_request` / `dm_send_req` 这组稳定 API 解耦；
   - 框架层与传输层之间：经 `DM_INTF_S` 的函数指针（`send_msg`/`recv_msg`）解耦；
   - HDC 阻塞会话与 poller 事件模型之间：经 pipe（`rfd`/`wfd`）解耦。
3. **画一次「DSMI 下发大 DMP 命令」的时序**：包含分片 → `dm_send_req`（pending+msgid）→ `__dm_hdc_send` → `halHdcSend` → 设备 → 响应回程 → pipe → poller → `__dm_rsp_handle` → `comm_msg_handle` → `dmp_msg_recv_resp` 重组 → `dsmi_msg_recev` 回调。
4. **写一段反思**：如果把传输从 HDC 换成 UDP，框架层和业务层的代码要不要改？为什么？（提示：看 `dsmi_init` 里 `CFG_FEATURE_DMP_HDC` 与 `CFG_FEATURE_DMP_UDP` 的并列分支。）

> 预期：你能指出业务层与框架层代码完全不用改，只需要在初始化时换成 `dm_udp_init`（提供另一套 `DM_INTF_S` 函数指针）即可——这正是 `device_monitor` 这套抽象的核心价值。架构图与时序可作为你后续阅读 logdrv（[u5-l2](u5-l2-logdrv-and-msnpureport.md)）、prof（[u5-l3](u5-l3-prof-adapt.md)）时的「地图」。

## 6. 本讲小结

- **DMC** 是 HAL 层的设备维护组件集合，`device_monitor` 是其中提供**通用消息收发框架**的公共底座，DSMI/logdrv/prof 等都复用它。
- 框架的核心是两个抽象：`DM_CB_S`（调度中枢：poller + `intf_list`/`pending_list`/`cmd_reg_list`）与 `DM_INTF_S`（可插拔管道：一组 `send_msg`/`recv_msg`/`close` 函数指针）。
- 框架用「**请求结构体地址当 msgid**」做请求-响应配对，用 poller 定时器做**超时与重传**，用 selfloop 接口做本机回送。
- **dm_hdc** 把框架架到 HDC 上：用一根 **pipe** 把 HDC 的「阻塞式会话接收」（专用线程 `halHdcRecv`）适配成 poller 的「fd 可读事件」；客户端走 connect→send→recv，服务端走 accept→每会话一线程。
- **dev_mon_dmp_client** 在框架之上做 **DMP 报文的分片与重组**：发送时按 `max_trans_len` 切片（首末帧用 `lun` bit7 标记），接收时按 `op_fun`+`op_cmd` 在哈希表里累计，没收齐就更新 `offset` 续发拉取。
- 一条 DSMI 命令的完整链路：`dsmi_common.c` → `dev_mon_send_request` → `dm_send_req` → `__dm_hdc_send` → `halHdcSend`（HDC）→ 设备；响应原路经 pipe/poller/配对/重组回到 DSMI 回调。

## 7. 下一步学习建议

- **[u5-l2 日志驱动 logdrv 与 msnpureport](u5-l2-logdrv-and-msnpureport.md)**：logdrv 同样是 DMC 子模块，会复用本讲的 `device_monitor` 消息框架把日志报文从设备搬到 Host。学完本讲再看 logdrv，你会自动聚焦在「业务层怎么用框架」，而非被收发细节困住。
- **[u5-l3 Profiling 性能采集适配](u5-l3-prof-adapt.md)**：prof 数据上报也走类似的「设备→Host」通路，可对比它与 `device_monitor` 在传输选型上的异同。
- **回看 [u3-l2 HDC 通信模型](u3-l2-hdc-communication.md)**：本讲多次用到 `halHdcSend`/`halHdcRecv`/`drvHdcSessionConnect` 等原语，若对会话/epoll 细节有疑问，可回到 u3-l2 对照。
- **动手（可选）**：若你关注多芯片适配，可研究 `build.sh --soc` 如何通过 `CFG_FEATURE_DMP_HDC`/`CFG_FEATURE_DMP_UDP` 等宏切换 `device_monitor` 启用哪种传输——这是 [u8-l3 多芯片适配](u8-l3-multi-chip-and-build-config.md) 的伏笔。
