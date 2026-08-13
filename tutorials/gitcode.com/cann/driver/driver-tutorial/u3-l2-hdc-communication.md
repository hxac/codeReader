# 主机-设备通信：HDC client/server/core 模型

> 单元 3 · HAL 层与主机-设备通信 · 第 2 讲（依赖 `u3-l1`）

## 1. 本讲目标

在上一讲（`u3-l1`）里，我们把 HAL 层（`libascend_hal.so`）当作一个整体来认识，并提到 **HDC 是 HAL/SDK 各模块共用的「通信底座」**。本讲就把这个底座拆开看。学完本讲，你应该能够：

1. 说出 **HDC（Host-Device Communication，主机-设备通信）** 在整个驱动栈里扮演的角色——为什么 HAL、DSMI、设备监控（DMC）、性能采集（prof）等模块都要复用它。
2. 理解 **客户端（client）/ 服务端（server）/ 核心（core）** 三件套的职责划分，以及它们各自管理的数据结构（`hdc_client_head` / `hdc_server_head` / `hdc_session`）。
3. 看懂 HDC 的两条工作模式：**阻塞式收发** 与基于 **epoll 的事件驱动** 收发，并理解 `hdc_epoll_ops` 这张虚函数表如何让同一套 API 同时跑在 PCIe、Socket、UB（统一总线）三种底层链路上。
4. 跟踪一次「主机侧发起请求 → 到达设备侧」的完整调用链，并能画出客户端-服务端的交互时序草图。

## 2. 前置知识

- **用户态与内核态**：NPU 驱动分两半——`libascend_hal.so` 跑在用户态，`drv_hdcdrv.ko` 跑在内核态。用户态要操作硬件，必须通过系统调用 `ioctl` 陷入内核。HDC 的「主机侧」代码就在用户态库里，真正的搬数据发生在内核态驱动里。
- **client/server 模型**：和写网络程序一样，通信分「主动连接」的一端（client）和「监听并接受连接」的一端（server）。在昇腾场景里，Host 侧进程通常是 client，Device 侧（由内核驱动代理）是 server；反过来 Device 主动上报时角色互换。
- **session（会话）**：一条已建立的逻辑连接。HDC 里几乎所有收发接口都以 `HDC_SESSION` 作为第一参数，就像 socket 编程里的 `fd`。
- **epoll**：Linux 下高效的多路复用机制，用一个线程同时等待大量 fd 的事件。HDC 把 epoll 抽象成自己的 `drvHdcEpollCreate/Ctl/Wait`，使得「一个线程服务上千条 session」成为可能。
- **传输类型（trans_type / h2d_type）**：HDC 不绑定某一种物理链路。`g_hdcConfig.trans_type` 决定走 **PCIE** 还是 **SOCKET**；当 `trans_type=PCIE` 时，再用 `h2d_type` 细分是真正的 PCIe（`HDC_TRANS_USE_PCIE`）还是统一总线 UB（`HDC_TRANS_USE_UB`，超节点/ascend950 场景）。这是本讲反复出现的「按类型分发」开关。

> 一句话直觉：**HDC ≈ 把 socket 那套 connect/accept/send/recv/epoll 的编程模型，搬到「主机↔NPU 设备」之间，并屏蔽底层到底是 PCIe、Socket 还是 UB。**

## 3. 本讲源码地图

HDC 的源码集中在 `src/ascend_hal/hdc/`，按底层链路再分子目录。本讲聚焦 `common/` 下与链路无关的「公共骨架」：

| 文件 | 作用 |
| --- | --- |
| [src/ascend_hal/hdc/common/hdc_client.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c) | **客户端**：创建 client、分配/释放 session、发起连接 `halHdcSessionConnectEx`、关闭 session。 |
| [src/ascend_hal/hdc/common/hdc_server.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c) | **服务端**：创建 server（监听）、接受连接 `drvHdcSessionAccept`、关闭 session。 |
| [src/ascend_hal/hdc/common/hdc_core.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c) | **核心**：库加载时的自动初始化、配置读取、消息描述符（msg）管理、阻塞式 `halHdcSend`/`halHdcRecv`、内存相关。是体量最大、最关键的一个文件。 |
| [src/ascend_hal/hdc/common/hdc_epoll.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c) | **事件驱动**：epoll 的创建/注册/等待，通过 `hdc_epoll_ops` 虚表分发到具体链路。 |

辅助理解（非本讲主角，但会被引用）：

- [src/ascend_hal/hdc/inc/hdc_cmn.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h) — 所有公共数据结构（session/head/config）与 ioctl 命令码宏的集中定义。
- [src/ascend_hal/hdc/common/hdc_epoll.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.h) — `struct hdc_epoll_ops` 虚函数表。
- [src/ascend_hal/hdc/pcie/hdc_pcie_drv.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c) — PCIe 后端，`hdc_ioctl` 与 `hdc_pcie_send/recv` 的真正实现，是把 HDC 调用翻译成 `ioctl` 陷入内核的「桥梁」。

---

## 4. 核心概念与源码讲解

### 4.1 公共骨架：传输类型、全局配置与核心数据结构

#### 4.1.1 概念说明

在看 client/server 之前，必须先建立三个全局共识，否则后面满屏的 `g_hdcConfig.trans_type` 判断会让人迷路：

1. **链路是可插拔的**：HDC 的公共代码不直接读写 PCIe 寄存器，也不直接 `send()` socket。它总是先查 `g_hdcConfig`，判断当前是哪种链路，再调对应后端（`hdc_pcie_*` / `hdc_ub_*` / `drv_hdc_socket_*`）。这就解释了为什么 client/server/core/epoll 四个文件里几乎每个关键函数都有 `if (g_hdcConfig.trans_type == ...)` 的分叉。
2. **一套全局配置 `g_hdcConfig`**：整个进程只有一份 `struct hdcConfig g_hdcConfig`，在库加载时由 core 的构造函数填好，之后所有模块只读它。
3. **所有句柄都带「魔数」`HDC_MAGIC_WORD`**：这是 HDC 的健壮性设计——每个 client/server/session/epoll 句柄头部都有一个 magic 字段，函数入口处校验它，防止传入野指针或已释放的句柄。

#### 4.1.2 核心数据结构

最核心的几个结构体都定义在 `hdc_cmn.h`。先看「会话」本身：

```c
struct hdc_session {
    unsigned int magic;            // HDC_MAGIC_WORD，校验用
    unsigned int device_id;        // 对端设备号
    signed int sockfd;             // 内核侧分配的会话标识（类比 socket fd）
    unsigned int type;             // HDC_SESSION_SERVER=0 / HDC_SESSION_CLINET=1
    mmProcess bind_fd;             // PCIe 设备 fd
    unsigned int session_cur_alloc_idx;
    ...
};
```

> 小贴士：宏 `HDC_SESSION_CLINET`（值 1）是源码里真实存在的拼写（「CLINET」少了个 T），见 [hdc_cmn.h:33-34](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h#L33-L34)。读源码时遇到这类「历史遗留拼写」不要慌，原样沿用即可——改宏名会牵动所有调用点。

`sockfd` 这个名字容易误导：在 PCIe/UB 模式下它并不是真正的 socket 文件描述符，而是**内核 HDC 驱动分配的会话编号**，后续 `HDCDRV_SEND`/`HDCDRV_RECV` 等 ioctl 都靠它定位是哪一条会话。

再看两个「容器」结构（都用 C 的柔性数组 `session[0]` 预留 N 个会话槽位）：

```c
struct hdc_client_head {           // 客户端容器
    unsigned int magic;
    signed int serviceType;        // 业务类型：FRAMEWORK / TDT / LOG / PROF ...
    unsigned int flag;             // 连接超时等
    unsigned int maxSessionNum;    // 容器最多容纳几条 session
    mmMutex_t mutex;               // 保护下面 session 数组的并发分配
    struct hdc_client_session session[0];
};

struct hdc_server_head {           // 服务端容器
    unsigned int magic;
    signed int serviceType;
    unsigned int session_num;      // 当前已接受的 session 数
    mmSockHandle listenFd;         // 监听「fd」（PCIe 下实为 deviceId）
    signed int deviceId;
    mmProcess bind_fd;
    mmMutex_t mutex;
    ...
    struct hdc_server_session session[0];
};
```

最后是全局配置与魔数：

- [hdc_cmn.h:60](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h#L60) — `#define HDC_MAGIC_WORD 0x484443FF`（ASCII "HDC" + 0xFF）。
- [hdc_cmn.h:578](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h#L578) — `struct hdcConfig`，含 `trans_type`、`h2d_type`、`pcie_handle`（打开的 `/dev` 设备 fd）、`pcie_segment`、各业务端口号表等。
- 传输类型枚举在公共头里：[ascend_hal_base.h:70-73](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L70-L73)，`HDC_TRANS_USE_SOCKET=0`、`HDC_TRANS_USE_PCIE=1`；UB 的值 2 定义在 [hdc_cmn.h:294](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h#L294)。

掌握了这三个结构 + 一个全局配置，下面四个模块就是「围绕它们做生命周期管理与数据搬运」。

#### 4.1.3 代码实践

**实践目标**：在源码里建立「结构体—字段—校验」的直觉。

1. 打开 `hdc_cmn.h`，定位 `struct hdc_session`、`struct hdc_client_head`、`struct hdc_server_head`、`struct hdcConfig` 四个定义，记住它们各自的「magic 字段」位置。
2. 用编辑器搜索 `HDC_MAGIC_WORD`，统计它在 `hdc_client.c`/`hdc_server.c`/`hdc_core.c` 里被**读取校验**（如 `if (pHead->magic != HDC_MAGIC_WORD)`）的次数。

**需要观察的现象**：几乎每个对外函数入口都会校验 magic；这是 HDC 抵御「悬空指针/重复释放」的第一道防线。

**预期结果**：你会看到 client/server 侧至少各有 3~5 处 magic 校验，core 侧更多。

#### 4.1.4 小练习与答案

- **练习 1**：`struct hdc_session` 里的 `sockfd` 字段，在 PCIe 模式下到底存的是什么？
  - **答案**：不是真正的 socket fd，而是内核 HDC 驱动为该会话分配的**会话编号**。用户态后续发 `HDCDRV_SEND`/`HDCDRV_RECV` ioctl 时，把它填进 `hdcCmd.send.session`，内核据此找到对应会话。
- **练习 2**：为什么 client/server 的 head 结构都用柔性数组 `session[0]`，而不是固定大小的数组？
  - **答案**：因为 `maxSessionNum`（最多会话数）由调用方按业务类型在 `drvHdcClientCreate` 时传入，柔性数组允许「一次 malloc 同时拿下 head + N 个 session 槽」，既省一次分配又保证内存连续、对缓存友好。

---

### 4.2 hdc_client：主动发起连接的客户端

#### 4.2.1 概念说明

client 是通信的**主动方**：它先创建一个「客户端容器」（`drvHdcClientCreate`），声明自己最多同时持有几条 session、属于哪种业务（`serviceType`），然后对目标设备发起连接（`halHdcSessionConnectEx`）。client 不直接收发数据——收发是 core（4.4 节）的事；client 只负责「会话的生命周期」。

#### 4.2.2 核心流程

创建并使用一个 client 的典型流程：

```text
drvHdcClientCreate(&client, maxSessionNum, serviceType, flag)
        │  分配 hdc_client_head + N 个 session 槽，全部 alloc=false
        ▼
halHdcSessionConnectEx(peer_node, peer_devid, peer_pid, client, &session)
        │  1) 参数校验（devid 范围、magic）
        │  2) drv_hdc_client_alloc_session：从 N 个槽里找一个 alloc=false 的，置 alloc=true
        │  3) 按 trans_type/h2d_type 分发：
        │       PCIE+UB   → hdc_ub_connect
        │       PCIE+PCIE → hdc_pcie_connect  (→ ioctl HDCDRV_CONNECT)
        │       SOCKET    → drv_hdc_socket_session_connect
        │  4) 成功：session.type=HDC_SESSION_CLINET，返回 HDC_SESSION 句柄
        │     失败：释放刚分配的槽，映射错误码后返回
        ▼
（用 session 做 halHdcSend / halHdcRecv，见 4.4 节）
        ▼
drv_hdc_client_session_close(session, ...)   // 关闭单条会话
drvHdcClientDestroy(client)                  // 全部会话关闭后才允许销毁 client
```

#### 4.2.3 源码精读

**创建 client** —— [hdc_client.c:68-113](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L68-L113)。关键是一次性分配 head + 槽位数组并初始化每个槽：

```c
size = sizeof(struct hdc_client_session) * (size_t)maxSessionNum + sizeof(struct hdc_client_head);
pHead = (struct hdc_client_head *)drv_hdc_zalloc(size);   // 清零分配
...
pHead->magic = HDC_MAGIC_WORD;
pHead->flag = (unsigned int)((flag == 0) ? HDC_SESSION_CONN_TIMEOUT : flag);
pHead->maxSessionNum = (unsigned int)maxSessionNum;
for (cnt = 0; cnt < maxSessionNum; cnt++) {
    pSession[cnt].alloc = false;        // 所有槽初始空闲
    pSession[cnt].session.sockfd = -1;
}
```

**抢占一个空闲 session 槽** —— [hdc_client.c:204-244](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L204-L244)。在 mutex 保护下线性扫描，找到第一个 `alloc==false` 的槽并占位。这是 HDC 并发安全的核心套路：**对 session 数组的任何改动都在 `mmMutexLock/UnLock` 之间**。

**发起连接（client 的灵魂）** —— [hdc_client.c:298-378](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L298-L378)。`halHdcSessionConnectEx` 集中体现了「按链路分发 + 错误码映射」两大模式：

```c
if (g_hdcConfig.trans_type == HDC_TRANS_USE_PCIE) {
    if (g_hdcConfig.h2d_type == HDC_TRANS_USE_UB) {
        ret = hdc_ub_connect(peer_devid, pHead, peer_pid, &p_client_session->session);
    } else if (g_hdcConfig.h2d_type == HDC_TRANS_USE_PCIE) {
        ret = hdc_pcie_connect(g_hdcConfig.pcie_handle, peer_devid, ...);
    }
    if (ret != 0) {
        drv_hdc_client_free_session(pHead, p_client_session);   // 失败要归还槽位
        if (ret == -HDCDRV_PEER_REBOOT)        return DRV_ERROR_DEV_PROCESS_HANG;
        if (ret == -HDCDRV_CONNECT_TIMEOUT)    return DRV_ERROR_WAIT_TIMEOUT;
        ...
    }
}
```

注意两点工程细节：① 失败路径一定记得 `drv_hdc_client_free_session` 归还刚占的槽，否则会泄漏；② 内核侧返回的是负的 `HDCDRV_*` 码，用户态这里把它们**逐条映射**成对外的 `DRV_ERROR_*`，让上层看到统一的错误语义。

**关闭会话** —— [hdc_client.c:418-469](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L418-L469)：按链路调 `hdc_ub_session_close` / `hdc_pcie_close` / `shutdown+close`，再归还槽位。

#### 4.2.4 代码实践

**实践目标**：理解「槽位分配」的并发安全。

1. 阅读 [drv_hdc_client_alloc_session](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L204-L244) 与 [drv_hdc_client_free_session](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L259-L268)。
2. 假设两个线程同时调用 `halHdcSessionConnectEx`，在纸上推演：谁先拿到 mutex，谁就先占到槽；第二个线程拿锁后看到的 `alloc` 已经是 `true`，于是跳过该槽找下一个。

**需要观察的现象**：`alloc` 标志的读写永远在 `mmMutexLock/UnLock` 之间，因此即使 N 个线程并发也不会分配到同一个槽。

**预期结果**：每次成功连接对应一个唯一槽位；若 N 个槽全占满，`drv_hdc_client_alloc_session` 返回 false，`halHdcSessionConnectEx` 返回 `DRV_ERROR_OVER_LIMIT`。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `drvHdcClientDestroy` 在还有 session 存活时要返回 `DRV_ERROR_CLIENT_BUSY` 而不是强制释放？
  - **答案**：每条活着的 session 在内核侧都占着资源（会话表项、可能还有未完成的收发）。强删 client 会让这些 session 成为孤儿，导致内核侧资源泄漏或悬空访问。所以 HDC 强制要求「先关全部 session，再销毁 client」。
- **练习 2**：`halHdcSessionConnectEx` 里的 `peer_node` 参数为什么注释要求「固定传 0」？
  - **答案**：当前实现只支持单节点（本机）的 Host-Device 通信，不支持跨节点远端设备，所以 `peer_node` 必须为 0；[drv_hdc_connect_para_check](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L270-L295) 里会显式校验 `peer_node != 0` 即报错。

---

### 4.3 hdc_server：监听与接受连接的服务端

#### 4.3.1 概念说明

server 是通信的**被动方**：它绑定到某个设备（`devid`）和某种业务（`serviceType`），在内核侧「挂牌监听」，然后阻塞或轮询地等待对端来连。当对端（通常是 Device 侧主动上报，或另一侧 Host 进程）发起连接时，server 用 `drvHdcSessionAccept` 接受它，得到一条新的 `HDC_SESSION`。

> 谁是 client 谁是 server？取决于数据流方向。Host 进程要给 Device 下发任务时，Host 是 client；当 Device 要把日志/性能数据主动推回 Host 时，Host 侧就改扮 server 监听对应业务端口。`serviceType`（FRAMEWORK/TDT/LOG/PROF/DVPP/RDMA/…）正是用来区分这些不同业务的「频道」。

#### 4.3.2 核心流程

```text
drvHdcServerCreate(devid, serviceType, &server)
        │  → drv_hdc_pcie_server_create：
        │      1) hdc_pcie_set_service_level  (按业务定优先级 LEVEL_0/LEVEL_1)
        │      2) hdc_pcie_create_bind_fd     (打开 PCIe 设备 fd)
        │      3) hdc_pcie_server_create / hdc_ub_server_create
        │         (ioctl HDCDRV_SERVER_CREATE：通知内核在该 device 上开始监听)
        ▼
drvHdcSessionAccept(server, &session)        // 阻塞等待对端连接
        │  → drv_hdc_pcie_session_accept：
        │      hdc_pcie_accept / hdc_ub_accept (ioctl HDCDRV_ACCEPT)
        │  成功：session.type=HDC_SESSION_SERVER，server->session_num++
        ▼
（用 session 做 halHdcSend / halHdcRecv）
        ▼
drv_hdc_server_session_close / drvHdcServerDestroy
```

#### 4.3.3 源码精读

**创建 server（含业务优先级）** —— [hdc_server.c:248-283](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L248-L283) 调用 [drv_hdc_pcie_server_create](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L36-L110)。后者有一个值得注意的细节——**业务优先级**：

```c
if (g_hdcConfig.h2d_type == HDC_TRANS_USE_PCIE) {
    ret = hdc_pcie_set_service_level(g_hdcConfig.pcie_handle, serviceType);  // 见 pcie_drv.c:176
}
```

而 `hdc_pcie_set_service_level`（[hdc_pcie_drv.c:176-203](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L176-L203)）把 FRAMEWORK/TDT/TSD 三类核心业务设为 `HDC_SERVICE_LEVEL_0`（高优先级），其余业务设为 `HDC_SERVICE_LEVEL_1`。也就是说，HDC 在内核侧对计算主链路（FRAMEWORK）给了更高带宽/调度优先级——这是驱动栈里很典型的「QoS 分级」。

**接受连接** —— [drvHdcSessionAccept](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L416-L468) → [drv_hdc_pcie_session_accept](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L148-L210)。核心是 `hdc_pcie_accept` / `hdc_ub_accept`（下发 `HDCDRV_ACCEPT` ioctl），并把对端「重启/未就绪」等错误映射好：

```c
if (ret == -HDCDRV_PEER_REBOOT)        return DRV_ERROR_DEV_PROCESS_HANG;   // 对端复位
if (ret == -HDCDRV_DEVICE_RESET)       return DRV_ERROR_DEVICE_NOT_READY;  // 本端复位
if (ret == -HDCDRV_DEVICE_NOT_READY)   ...                                  // 设备未就绪
```

成功后 `pServ->session_num++`（仍在 mutex 内），保证并发 accept 时计数准确。

**销毁 server 的安全闸** —— [drvHdcServerDestroy](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L300-L374)：与 client 对称，若 `session_num > 0` 直接返回 `DRV_ERROR_SERVER_BUSY`，拒绝销毁。

#### 4.3.4 代码实践

**实践目标**：对比 client 与 server 的对称设计。

1. 把 [drvHdcSessionAccept](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L416-L468) 和 [halHdcSessionConnectEx](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L298-L378) 并排阅读。
2. 列一张「client vs server」对照表：谁分配 session 槽（client 预分配 N 个 / server 多为按需 malloc）、谁主动谁被动、错误码映射是否相似。

**需要观察的现象**：两者都遵循「校验 → 分发到链路后端 → 错误码映射」三段式；差别只在「连接方向」与「session 来源」。

**预期结果**：你会得出结论——HDC 的 client/server 是同一套通信原语的两个朝向，理解了一个就理解了另一个。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `hdc_pcie_set_service_level` 要把 FRAMEWORK/TDT/TSD 单独设为 LEVEL_0？
  - **答案**：这三类是计算与数据搬运的主链路（框架下发任务、TDT 数据传输、TSD 任务调度），对时延和带宽敏感，需要在内核通信调度里享受更高优先级；日志、性能采集等运维类业务用 LEVEL_1 即可，避免与主链路抢资源。
- **练习 2**：server 的 `listenFd` 字段在 PCIe 模式下存的是什么？
  - **答案**：[drv_hdc_pcie_server_create](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_server.c#L36-L110) 里 `pHead->listenFd = devid;`——它存的是监听的目标设备号，而不是真正的 socket 监听 fd。这又是 HDC「借用 socket 命名、实为设备抽象」的体现。

---

### 4.4 hdc_core：初始化、消息描述符与阻塞式收发

#### 4.4.1 概念说明

core 是 HDC 的「心脏」，承担三件事：

1. **库的自动初始化**：用 GCC 构造函数属性，在 `libascend_hal.so` 被加载时（甚至早于 `main`）自动读配置、打开设备、填好 `g_hdcConfig`。
2. **消息描述符（msg）管理**：HDC 不让调用方直接丢裸 buffer，而是要求先组装一个 `drvHdcMsg` 描述符（含 buffer 指针与长度），再交给收发接口。
3. **阻塞式收发**：`halHdcSend` / `halHdcRecv` 是同步调用——发完/收完才返回（或超时）。

#### 4.4.2 核心流程

**库加载初始化链路**：

```text
（进程加载 libascend_hal.so）
   ▼  __attribute__((constructor))   drv_hdc_init()   [hdc_core.c:2517]
   ▼  hdc_init(&g_hdcConfig)         [hdc_core.c:545]
        │  读 /etc/hdcBasic.cfg（不存在则按平台默认：RC→SOCKET，否则→PCIE）
        │  若 PCIE：hdc_get_h2d_type()  问 devmng 当前是 PCIE 还是 UB
        │           hdc_pcie_init()     [hdc_core.c:466]
        │              ├─ 等 /dev 设备可访问（驱动已加载）
        │              ├─ hdc_phandle_get → hdc_pcie_open()/hdc_ub_open() → 拿到 pcie_handle
        │              ├─ drv_hdc_get_page_size
        │              └─ hdc_pcie_config (ioctl HDCDRV_HDCDRV_CONFIG)
   ▼  g_hdcConfig 就绪，后续 client/server/epoll 都读它
```

**一次阻塞式发送的链路**：

```text
应用：drvHdcAllocMsg → drvHdcAddMsgBuffer(buf, len)   组装消息描述符
应用：halHdcSend(session, pMsg, flag, timeout)        [hdc_core.c:2040]
        │  drv_hdc_send_check（magic / sockfd / buf 校验）
        │  按 h2d_type 分发：
        │     hdc_pcie_send  →  hdc_ioctl(HDCDRV_SEND)   →  mmIoctl  →  内核 drv_hdcdrv.ko  →  Device
        │     hdc_ub_send    →  UB 后端
        │     drv_hdc_socket_send → TCP socket
        ▼  返回 DRV_ERROR_NONE（成功）或映射后的错误码
```

#### 4.4.3 源码精读

**构造函数自动初始化** —— [hdc_core.c:2517-2531](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L2517-L2531)：

```c
signed int __attribute__((constructor)) drv_hdc_init(void) {
    (void)mmMutexInit(&g_mem_fd_mng.mutex);
    drv_hdc_trans_type_mutex_init();
    ret = (signed int)hdc_init(&g_hdcConfig);          // 整个初始化的入口
#ifdef CFG_FEATURE_SUPPORT_UB
    if (ret==0 && trans_type==PCIE && h2d_type==UB) {
        (void)hdc_ub_init(&g_hdcConfig);               // UB 额外初始化
    }
#endif
}
```

`__attribute__((constructor))` 是关键：它让 `drv_hdc_init` 在动态库被 `dlopen`/程序启动加载时**自动执行**，所以上层模块（acl/Runtime）根本不需要显式「初始化 HDC」——库一加载，`g_hdcConfig` 就已经填好了。这是 HDC 作为「底座」对上层「零侵入」的体现。

**配置读取与平台默认** —— [hdc_init](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L545-L590)。注意它对配置文件缺失的容错：找不到 `/etc/hdcBasic.cfg` 时，按 `CFG_SOC_PLATFORM_RC`（Device 侧 SoC）默认走 SOCKET，否则走 PCIE——也就是说同一份代码既能跑在 Host 卡上，也能跑在设备 SoC 上。

**PCIE 后端初始化** —— [hdc_pcie_init](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L466-L513)：循环等待 `PCIE_DEV_NAME` 可访问（即内核驱动已加载），再 [hdc_phandle_get](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L426-L464) 打开设备拿到 `pcie_handle`。这里有重试逻辑（`hdc_get_handle_count` 次），应对「库先加载、驱动后加载」的时序问题。

**消息描述符组装** —— 收发前要先造 msg：
- [drvHdcAllocMsg](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1140-L1185)：分配 `struct hdc_msg_head`，内含一个 `drvHdcMsg`（`count` + `bufList[]`）。目前强制 `count==1`（单 buffer）。
- [drvHdcAddMsgBuffer](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1313-L1357)：把用户 buffer 挂到 `bufList[0]`。

```c
p_msg_buf[count].pBuf = pBuf;   // 挂上用户 buffer 指针
p_msg_buf[count].len = len;
```

**阻塞发送** —— [halHdcSend](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L2040-L2099)。校验后按链路分发；PCIE 路径最终落到 [hdc_pcie_send](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L330-L377)，它把 `pMsg->bufList[0]` 拆成 `src_buf/len` 填进 `union hdcdrv_cmd`，再 `hdc_ioctl(HDCDRV_SEND)`：

```c
hdcCmd.send.session = pSession->sockfd;      // 内核会话编号
hdcCmd.send.src_buf = pMsg->bufList[0].pBuf; // 用户 buffer 地址
hdcCmd.send.len     = pMsg->bufList[0].len;
hdcCmd.send.wait_flag = wait;                // WAIT_ALWAYS / NOWAIT / WAIT_TIMEOUT
hdcCmd.send.timeout   = timeout;
ret = hdc_ioctl(handle, HDCDRV_SEND, &hdcCmd);
```

**ioctl 桥梁** —— [hdc_ioctl](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L119-L140)：把 `union hdcdrv_cmd` 同时作为 in/out 缓冲，调 `mmIoctl`（对 `pcie_handle` 这个 fd），并在 `EINTR`（被信号打断）时自动重试。这是用户态→内核态的**唯一过河通道**——所有 HDC 操作最终都收敛到这一个函数。

**阻塞接收** —— [drv_hdc_recv_msg_len](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1409-L1481)（先 peek 报文长度）+ [drv_hdc_recv_msg_body](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1538-L1573)（再收实际数据），两步合起来等价于 socket 的「先读长度再读体」。`msg_len==0` 被特别解释为「对端已关闭会话」。

#### 4.4.4 代码实践

**实践目标**：跟踪「用户 buffer 如何穿过用户态到达内核」。

1. 从 [halHdcSend](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L2040-L2099) 出发，跳到 [hdc_pcie_send](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L330-L377)，再跳到 [hdc_ioctl](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L119-L140)。
2. 在 [hdc_cmn.h:240-281](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/inc/hdc_cmn.h#L240-L281) 找到所有 `HDCDRV_*` ioctl 命令码宏（`HDCDRV_SEND`、`HDCDRV_RECV`、`HDCDRV_CONNECT`、`HDCDRV_ACCEPT` 等），数一数 HDC 一共定义了多少种内核命令。

**需要观察的现象**：用户态的 `pBuf` 指针被原样塞进 `hdcCmd.send.src_buf`，由内核驱动在内核态完成「用户空间→设备」的 DMA 搬运——用户态本身不拷贝数据。

**预期结果**：你会看到 HDC 把约 20+ 种操作（连接/收发/内存映射/epoll）全部复用到同一个 `hdc_ioctl` + `union hdcdrv_cmd` 的模式上。

> 真正在 NPU 设备上运行上述调用、抓取 `ioctl` 返回值，需要先按 `u1-l2` 编译部署驱动并有一张昇腾卡，本步骤的运行结果为**待本地验证**；源码跟踪与命令码统计无需硬件即可完成。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 HDC 用 `__attribute__((constructor))` 而不是让上层显式调用 `hdc_init`？
  - **答案**：HDC 是被 acl/Runtime/DSMI 等多个上层模块隐式依赖的底座。用构造函数后，只要进程加载了 `libascend_hal.so`，HDC 就自动就绪，上层无需关心初始化顺序，也避免了「忘记初始化」类 bug。
- **练习 2**：`drvHdcAllocMsg` 为什么目前强制 `count == 1`（只支持单 buffer）？
  - **答案**：注释写明「for future feature」——接口预留了多 buffer（scatter-gather）能力，但当前实现只放开单 buffer，以降低首版复杂度；`bufList[1]` 里那个 `1` 也是为了消除静态检查告警。这是大型驱动「接口先行、实现渐进」的常见做法。

---

### 4.5 hdc_epoll：事件驱动收发与多链路抽象

#### 4.5.1 概念说明

阻塞式的 `halHdcSend/halHdcRecv` 简单直观，但有一个致命缺点：**一条 session 占一个线程**。如果一个进程要同时服务几百条会话（比如一个 Device 同时对接多个业务频道），线程数会爆炸。epoll 解决的就是这个问题——**一个线程同时盯很多 session，谁来数据就处理谁**。

HDC 没有直接用 Linux 原生 `epoll`，而是做了一层抽象：定义 `struct hdc_epoll_ops` 虚函数表，再为 PCIe / Socket / UB 三种链路各实现一份。这样上层代码（`drvHdcEpollCreate/Ctl/Wait`）对三种链路完全一致。

#### 4.5.2 核心流程

```text
drvHdcEpollCreate(size, &epoll)                   创建一个 epoll 实例
        │  drv_hdc_epoll_get_ops(&g_hdcConfig)    按链路选后端 ops
        │  ops->hdc_epoll_create                  （PCIE: ioctl HDCDRV_EPOLL_ALLOC_FD）
        ▼
drvHdcEpollCtl(epoll, ADD, session, &event)       把某条 session 注册进来
        │  event.events = HDC_EPOLL_DATA_IN | HDC_EPOLL_SESSION_CLOSE | HDC_EPOLL_CONN_IN ...
        │  ops->hdc_epoll_ctl                     （PCIE: ioctl HDCDRV_EPOLL_CTL）
        ▼
drvHdcEpollWait(epoll, events, maxevents, timeout, &eventnum)   阻塞等事件
        │  ops->hdc_epoll_wait                    （PCIE: ioctl HDCDRV_EPOLL_WAIT）
        ▼
返回就绪事件 → 应用对就绪 session 调 halHdcRecv 读数据
```

关键认知：**epoll 本身不搬数据，它只负责「通知哪条 session 就绪」**。真正的数据搬运仍由 4.4 节的 `halHdcRecv` 完成。epoll 是「事件源」，recv 是「取数据」。

#### 4.5.3 源码精读

**虚函数表定义** —— [hdc_epoll.h:18-29](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.h#L18-L29)：

```c
struct hdc_epoll_ops {
    drvError_t (*hdc_epoll_create)(struct hdc_epoll_head *epoll_head, signed int size);
    drvError_t (*hdc_epoll_ctl)(struct hdc_epoll_head *epoll_head, signed int op, void *target,
                                const struct drvHdcEvent *event);
    drvError_t (*hdc_epoll_wait)(const struct hdc_epoll_head *epoll_head, struct drvHdcEvent *events,
                                 signed int maxevents, signed int timeout, signed int *eventnum);
    drvError_t (*hdc_epoll_close)(struct hdc_epoll_head *epoll_head);
};
```

这是经典的「C 风格多态」：四个函数指针 = 四个「方法」。三种链路各提供一个 `drv_get_hdc_*_epoll_ops()` 返回自己的实例。

**按链路选后端** —— [drv_hdc_epoll_get_ops](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L19-L46)：

```c
switch (conf->trans_type) {
    case HDC_TRANS_USE_PCIE:
        if (conf->h2d_type == HDC_TRANS_USE_UB)        ops = drv_get_hdc_ub_epoll_ops();
        else if (conf->h2d_type == HDC_TRANS_USE_PCIE) ops = drv_get_hdc_pcie_epoll_ops();
        break;
    case HDC_TRANS_USE_SOCKET:
        ops = drv_get_hdc_sock_epoll_ops();
        break;
}
```

**事件类型与互斥校验** —— [drv_hdc_epoll_ctl_para_check](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L127-L169)。HDC 定义了四种事件：

| 事件宏 | 含义 |
| --- | --- |
| `HDC_EPOLL_CONN_IN` | 有新的连接请求到达（server 侧用） |
| `HDC_EPOLL_DATA_IN` | 有普通数据可读 |
| `HDC_EPOLL_FAST_DATA_IN` | 有「快速通道」数据可读 |
| `HDC_EPOLL_SESSION_CLOSE` | 对端关闭了会话 |

校验里有一条重要约束（[hdc_epoll.c:147-151](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L147-L151)）：`CONN_IN` 不能与 `DATA_IN/FAST_DATA_IN/SESSION_CLOSE` 同时设置——因为「连接到来」与「数据到来」是两类互斥的事件，混在一起会让等待逻辑歧义。

**等待事件** —— [drvHdcEpollWait](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L242-L261)：校验后调 `ops->hdc_epoll_wait`，把就绪事件填进调用方提供的 `events[]`，并写回 `eventnum`。和原生 `epoll_wait` 的契约一致。

#### 4.5.4 代码实践

**实践目标**：理解 epoll 的「通知—取数据」两段式。

1. 阅读 [drvHdcEpollCtl](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L171-L206) 与 [drvHdcEpollWait](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_epoll.c#L242-L261)。
2. 在纸上画出一个事件循环：

   ```text
   while (true) {
       drvHdcEpollWait(epoll, events, N, timeout, &num);   // 阻塞等
       for (i in 0..num) {
           if (events[i] & HDC_EPOLL_CONN_IN)      → drvHdcSessionAccept(...)   // 接新连接
           if (events[i] & HDC_EPOLL_DATA_IN)      → halHdcRecv(...)            // 读数据
           if (events[i] & HDC_EPOLL_SESSION_CLOSE)→ 关闭该 session
       }
   }
   ```

   （上面这段循环为说明用**示例代码**，项目里它由 acl/Runtime 等上层模块各自实现。）

**需要观察的现象**：一个线程 + 一个 epoll 就能同时处理「新连接 + 多条会话数据 + 对端关闭」三类事件，无需每条 session 起一个线程。

**预期结果**：理解 HDC 的事件模型与 Linux epoll 几乎同构，差别只是 fd 换成了「session/连接」、底层系统调用换成了 ioctl。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 HDC 要把 epoll 包一层 `hdc_epoll_ops` 虚表，而不直接调 `epoll_create/epoll_ctl`？
  - **答案**：因为 PCIe 与 UB 链路下，「fd」其实是内核 HDC 驱动分配的资源，原生 epoll 无法直接 watch 它们。虚表让 PCIe 后端用 ioctl（`HDCDRV_EPOLL_*`）、Socket 后端用原生 epoll、UB 后端用自己的事件机制，而上层 API 完全统一。
- **练习 2**：`HDC_EPOLL_CONN_IN` 与 `HDC_EPOLL_DATA_IN` 为什么不能同时注册？
  - **答案**：前者表示「有新连接需要 accept」（面向 server 的监听端），后者表示「已有会话有数据可读」（面向已建立 session）。两者发生在通信的不同阶段，混设会让 `wait` 返回时无法判断该 accept 还是 recv，所以校验阶段就拒绝。

---

## 5. 综合实践：画出 HDC 客户端-服务端交互时序草图

本任务把四个模块串起来，是本讲的核心实践。**无需 NPU 硬件即可完成源码跟踪与绘图部分**。

### 5.1 实践目标

用一张时序图说清「主机侧发起的一次请求，如何经由 client 封装、core 处理、epoll 收发到达设备侧」。

### 5.2 操作步骤

1. **选定一条最常见链路**：Host 进程作为 client，走 PCIE（`trans_type=PCIE, h2d_type=PCIE`），向 Device 的 FRAMEWORK 业务发起请求并等回应。
2. **按顺序跟踪下列源码点**，每一步记下「函数名、文件:行号、它做了什么」：
   - 建链：`drvHdcClientCreate`（[hdc_client.c:68](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L68)）→ `halHdcSessionConnectEx`（[hdc_client.c:298](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_client.c#L298)）→ `hdc_pcie_connect` → `hdc_ioctl(HDCDRV_CONNECT)`。
   - 组装请求：`drvHdcAllocMsg`（[hdc_core.c:1140](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1140)）+ `drvHdcAddMsgBuffer`（[hdc_core.c:1313](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1313)）。
   - 发送：`halHdcSend`（[hdc_core.c:2040](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L2040)）→ `hdc_pcie_send`（[hdc_pcie_drv.c:330](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/pcie/hdc_pcie_drv.c#L330)）→ `hdc_ioctl(HDCDRV_SEND)` → 内核 → Device。
   - 接收回应：`drvHdcRecvPeek`（[hdc_core.c:1590](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L1590)）/ `halHdcRecv`（[hdc_core.c:2281](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/hdc/common/hdc_core.c#L2281)）→ `hdc_pcie_recv_peek`/`hdc_pcie_recv` → `hdc_ioctl(HDCDRV_RECV_PEEK/RECV)`。
3. **画出时序图**（手绘或工具均可），纵向四个泳道：`Host 应用` / `hdc_client+core（用户态）` / `hdc_pcie_drv → hdc_ioctl（用户态/内核边界）` / `内核 drv_hdcdrv.ko → Device`。横向按时间排列上述调用，用箭头标出「 ioctl 陷入内核」这一关键跨越。
4. **在图上额外标出 epoll 版本**：把第 4 步「接收回应」改成「先 `drvHdcEpollWait` 收到 `HDC_EPOLL_DATA_IN`，再 `halHdcRecv`」，体会事件驱动的差别。

### 5.3 需要观察的现象

- 用户态的调用链始终在 `common/`（client/core/epoll）与 `pcie/`（pcie_drv）两个目录间跳转；前者链路无关，后者是「翻译成 ioctl」的链路相关层。
- 无论哪种操作，最终都汇聚到 `hdc_ioctl` 这一个函数进入内核。

### 5.4 预期结果

得到一张清晰的时序草图，能向别人讲明白：**HDC 用 client/server 管理会话生命周期、用 core 做收发与初始化、用 epoll 做事件多路复用，三者通过 `g_hdcConfig` 的链路类型统一分发到 PCIe/UB/Socket 后端，再由 `hdc_ioctl` 陷入内核驱动到达 NPU 设备。** 运行期在真实设备上抓包/打日志验证调用顺序为**待本地验证**。

---

## 6. 本讲小结

- **HDC 是 HAL/SDK 各模块共用的通信底座**：DSMI、DMC（设备监控）、prof（性能采集）、Runtime 等都构建在它之上，一套 API 同时支持 PCIe、Socket、UB 三种链路。
- **client/server/core 三件套分工**：client/server 管「会话生命周期」（建链、接受、关闭），core 管「初始化 + 消息描述符 + 阻塞收发」；两者通过对称的 `hdc_*_head` 容器结构与 magic 校验保证健壮性。
- **链路可插拔靠 `g_hdcConfig` 分发**：`trans_type` + `h2d_type` 两个开关决定每个操作走 `hdc_pcie_*` / `hdc_ub_*` / `drv_hdc_socket_*` 哪个后端。
- **所有用户态操作收敛到 `hdc_ioctl`**：它把 `union hdcdrv_cmd` 同时当入参/出参，经 `mmIoctl` 陷入内核 `drv_hdcdrv.ko`，这是用户态→设备的唯一通道。
- **epoll 提供事件驱动收发**：用 `hdc_epoll_ops` 虚表屏蔽链路差异，让一个线程服务大量 session；epoll 只通知就绪，真正取数据仍靠 `halHdcRecv`。
- **库加载即就绪**：`__attribute__((constructor))` 让 HDC 在 `libascend_hal.so` 加载时自动初始化，上层零感知。

## 7. 下一步学习建议

- 本讲把 HDC 当作「黑盒通信管道」来用。若想知道管道更深处的「统一设备接入抽象」，请进入 **`u3-l3` PBL 基础库：UDA 统一设备接入**——UDA 屏蔽了底层设备打开/访问差异，是 HDC 与硬件之间更靠下的一层。
- 想了解请求如何被「分发到合适的处理路径」以及驱动内部的错误码体系，继续看 **`u3-l4` PBL：URD 请求转发与 commlib 公共函数**。
- 如果你对「内核侧如何响应这些 ioctl、如何管理中断与预留内存」更感兴趣，可以提前跳到单元 6 的 **`u6-l1` SDK-driver 层总览与 kernel_adapt 内核适配**，把用户态/内核态的两端对齐来看。
