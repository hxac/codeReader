# RoCE 模块：RDMA Lite 用户态驱动与数据面

## 1. 本讲目标

本讲聚焦昇腾 HAL 层中的 **RoCE（RDMA over Converged Ethernet）** 模块，讲解它如何基于开源 [rdma-core](https://github.com/linux-rdma/rdma-core) 框架做「裁剪式」定制，得到一套称为 **RDMA Lite** 的用户态驱动。

读完本讲，你应当能够：

- 说清 RoCE Lite 把通信切成「控制面」与「数据面」两部分的动机，以及它为什么能比标准 verbs 性能更高。
- 解释控制面 lite 接口如何把 Device 侧已经建好的队列/门铃内存「映射回 Host」，从而在 Host 侧重建上下文。
- 跟踪数据面 `rdma_lite_post_send` 一条 Work Request（WR）从用户结构体、写成 WQE、推进队列、敲 doorbell，直到 `rdma_lite_poll_cq` 轮询完成的全过程。
- 理解 `roce_hal_api` 如何申请 Device 内存、如何把 RoCE 物理端口号翻译成 HAL 逻辑 `devid`，并与前序讲义（HAL 公共接口 u3-l1、SVM 初始化 u4-l1）串联。

---

## 2. 前置知识

本讲默认你已经读过：

- **u3-l1 HAL 层总览**：知道 HAL 编译为用户态动态库 `libascend_hal.so`，对外暴露 `hal*` 接口，返回 `drvError_t`（成功即 0）。
- **u4-l1 SVM 模块初始化**：知道「先预留虚拟地址、再 mmap、再进内核申请物理页」的两阶段内存套路。

下面几个 RDMA 术语会在文中反复出现，先用一句话建立直觉：

| 术语 | 直觉解释 |
|------|----------|
| **RDMA** | Remote Direct Memory Access，远程直接内存访问。一端 CPU 不参与搬运，网卡硬件直接读写对端内存，延迟极低。 |
| **RoCE** | RDMA over Converged Ethernet，在以太网上跑 RDMA 的协议。 |
| **QP（Queue Pair）** | 队列对，RDMA 通信的端点。每个 QP 含一个发送队列 **SQ** 和一个接收队列 **RQ**。 |
| **WQ / WQE** | Work Queue / Work Queue Element。存放任务的环形队列 / 队列里的一个任务条目。 |
| **WR（Work Request）** | 用户提交的一次工作请求（如「发一段数据」「RDMA 写对端」）。一个 WR 会被翻译成一个 WQE 写进 WQ。 |
| **CQ / CQE** | Completion Queue / Completion Queue Element。硬件完成一个任务后往 CQ 里塞一个 CQE，CPU 轮询 CQ 就知道任务做完了。 |
| **doorbell（门铃）** | 一小块寄存器/内存。CPU 把新任务塞进队列后，写一下 doorbell「通知」硬件有活干了。 |
| **verbs** | libibverbs/librdmacm 提供的标准 RDMA 编程接口（`ibv_post_send` 等），rdma-core 是其开源实现。 |

标准 verbs 的问题是：每一次 `ibv_post_send` 都要走 rdma-core 用户态库的逻辑、甚至要陷入内核去敲 doorbell，高频小消息（比如 AI 集合通信里的 allreduce/allgather）会被这层开销吃掉不少性能。RoCE Lite 的思路是——既然 Device 侧的队列内存和 doorbell 内存都可以映射到 Host 用户态，那干脆让 Host **直接写队列、直接敲门铃**，把数据面路径压到最短。

> 名词提示：本讲里 **DVA** 指 Device 侧虚拟地址（Device Virtual Address），**HVA** 指 Host 侧虚拟地址（Host Virtual Address）。Lite 驱动的核心工作之一，就是在这两种地址之间搭桥。

---

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `src/ascend_hal/roce/` 下：

| 文件 | 角色 |
|------|------|
| [src/ascend_hal/roce/README.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md) | RoCE 模块总览，讲清控制面/数据面划分与应用场景。 |
| [src/ascend_hal/roce/host_lite/rdma_lite.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h) | Lite 公共接口声明、WR/WC/CQ 等用户结构体、`rdma_lite_ops` 函数指针表。 |
| [src/ascend_hal/roce/host_lite/rdma_lite.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.c) | 对外门面：每个 `rdma_lite_*` 函数都只做一次「查 ops 表 → 转发」。 |
| [src/ascend_hal/roce/host_lite/hns_roce_lite.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.h) | HNS RoCE 硬件相关的私有结构体（QP/CQ/WQE/CQE 硬件布局）与字段位宏。 |
| [src/ascend_hal/roce/host_lite/hns_roce_lite.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c) | Lite 驱动核心实现：上下文/CQ/QP 创建、`post_send`/`post_recv`、`poll_cq`。 |
| [src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c) | 控制面支撑：把 Device DVA 映射成 Host HVA（`halHostRegister`）、doorbell 去重、QP 属性拷贝。 |
| [src/ascend_hal/roce/roce_hal_api/hns_roce_hal.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.h) | Device 内存申请接口声明与预留内存 ioctl 命令定义。 |
| [src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c) | Device 内存申请实现：NUMA 感知 mmap、`phy_id→devid` 翻译、预留内存两阶段 populate。 |

> 目录约定：`host_lite/` 是「RDMA Lite 用户态驱动」，`roce_hal_api/` 是「Device 内存申请」。README 明确指出这两部分才是 driver 仓里的代码，其余 RDMA 能力由设备侧固件与 rdma-core 提供。

---

## 4. 核心概念与源码讲解

### 4.1 RoCE Lite 模块总览与设计动机

#### 4.1.1 概念说明

标准 RDMA 编程模型（verbs）把一次发送分成两个角色：

- **控制面**：建 QP、建 CQ、修改 QP 状态、注册内存……这些操作低频但重，要经过 rdma-core 与内核驱动协商，硬件才认。
- **数据面**：`ibv_post_send` 下发 WR、`ibv_poll_cq` 收完成……这些操作高频，每条消息都要做。

昇腾 RoCE 的定制想法是：**Device 侧（NPU 上的 RoCE 硬件 + 固件）有能力自己把控制面建好**——QP 的 WQE 环形缓冲、doorbell 寄存器、CQ 缓冲都已在 Device 内存里分配妥当。那么 Host 侧不必再走一遍标准 verbs 的建链流程，只需把这些 Device 内存「映射回 Host 用户态」，就能得到一个可以直接读写硬件队列的「影子上下文」，这就是 **Lite 上下文重建**。

数据面因此收益巨大：Host 用户态直接把 WR 翻译成 WQE 写进映射过来的队列缓冲，再写一下映射过来的 doorbell 内存，硬件就收到任务了——**全程不陷内核、不经 rdma-core 的重路径**。README 用一句话点明了两面：

[README.md:5-8](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md#L5-L8) 把控制面定位为「将 Device 内存映射到 Host 内存，支持在 Host 侧重建对应的上下文」，把数据面定位为「基于重建的上下文直接在 Host 侧下发数据面操作，提升 WR 下发及轮询 CQ 的性能」。

#### 4.1.2 核心流程

整个 Lite 驱动的使用流程可以画成两条带：

```text
【控制面 · 一次性】
  Device 侧固件已建好 QP/CQ/mem，把它们的「属性快照」(qpn、队列深度、缓冲 DVA、doorbell DVA...)带回 Host
        │
        ▼
  Host: rdma_lite_alloc_context   ── 建 Lite 上下文，把 phy_id 翻成 devid
        rdma_lite_init_mem_pool    ── （可选）注册一段 Device 内存做内存池
        rdma_lite_create_cq        ── 把 CQ 缓冲 / sw doorbell 缓冲 mmap 回 Host
        rdma_lite_create_qp        ── 把 QP 的 WQE 缓冲 / SQ doorbell / RQ doorbell mmap 回 Host
        │  ← 至此 Host 拿到了可直接读写的「硬件队列影子」
        ▼
【数据面 · 高频循环】
  rdma_lite_post_send(wr)  ── WR → 翻译成硬件 WQE → 写入映射的 SQE 缓冲 → 推进 head → 写 doorbell
          │
  （硬件搬数）             ── RoCE 网卡硬件直接 DMA 搬运，CPU 不参与
          │
  rdma_lite_poll_cq(wc)    ── 读映射的 CQE 缓冲 → 解析状态/opcode → 推进 cons_index → 回写 sw doorbell
```

注意「属性快照」是理解控制面的钥匙：Device 侧把已建好的资源用一个 `rdma_lite_device_*_attr` 结构体描述（见 [rdma_lite_common.h:65-74](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/inc_extend/private/network/rdma_lite_common.h#L65-L74) 的 `rdma_lite_device_qp_attr`，里面有 `qpn`、`qp_buf`、`sq`/`rq` 的 `db_buf` 等），Host 拿到后据此重建。

#### 4.1.3 源码精读

README 还给出了数据面的典型调用，与上层集合通信的关联：

- [README.md:16-18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md#L16-L18)：driver 仓里只实现两块——`host_lite/`（RDMA Lite 用户态驱动）与 `roce_hal_api/`（Device 内存申请）。
- [README.md:22](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md#L22)：给出样例调用 `rdma_lite_post_send(lite_qp, lite_send_wr, &lite_send_bad_wr, attr, &resp);`，强调「在 Host 侧下发 WR，达到直接将 WR 写入 Device 侧队列的目的」。
- [README.md:30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md#L30)：上层集合通信库组合下发 WR（如 RDMA Write）实现 allgather 等高级算子。

构建上，整组文件编成一个独立动态库 `libascend_rdma_lite.so`（CMakeLists 里 `add_library(ascend_rdma_lite SHARED ...)`，见 [host_lite/CMakeLists.txt:21-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/CMakeLists.txt#L21-L24)），并链接 `ascend_hal`（见 [host_lite/CMakeLists.txt:64-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/CMakeLists.txt#L64-L71)）——这说明 Lite 驱动是「架在 HAL 之上」的一层，所有设备访问最终都落到 HAL 的 `hal*` 接口。

#### 4.1.4 代码实践

**实践目标**：建立 RoCE Lite 的全局心智模型，把 README 里的概念落到具体目录与调用。

**操作步骤**：

1. 阅读 [README.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/README.md) 与同目录的两张图（`docs/zh/figures/RoCE_functional_framework.png`、`RoCE_data_plane_delivers_WR.png`）。
2. 在源码中确认 README 说的「两块代码」：`src/ascend_hal/roce/host_lite/` 与 `src/ascend_hal/roce/roce_hal_api/`。
3. 对照 4.1.2 的流程图，给「控制面四步」各找出一个对应函数名（`rdma_lite_alloc_context` / `rdma_lite_init_mem_pool` / `rdma_lite_create_cq` / `rdma_lite_create_qp`）。

**需要观察的现象 / 预期结果**：能用自己的话说出「为什么 Lite 比标准 verbs 快」——因为数据面把 WR 直接写进映射到 Host 的 Device 队列缓冲、直接写映射到 Host 的 doorbell，省掉了 verbs 库与内核介入。

> 本实践为源码阅读型，无需运行硬件。

#### 4.1.5 小练习与答案

**练习 1**：标准 verbs 的 `ibv_post_send` 与 RoCE Lite 的 `rdma_lite_post_send` 最大的路径差异在哪？

**参考答案**：标准 verbs 要经 rdma-core 用户态库的完整逻辑、且通常要陷入内核去敲 doorbell；Lite 则因为队列缓冲与 doorbell 都被 mmap 到了 Host 用户态，WR 翻译后直接写在用户态内存里、doorbell 也写在用户态内存里，全程不陷内核。

**练习 2**：为什么 Lite 驱动需要链接 `ascend_hal`（`libascend_hal.so`）？

**参考答案**：Lite 把 Device 内存映射回 Host、申请 Device 内存、翻译设备号等底层操作，都不是自己实现的，而是调用 HAL 的 `halHostRegister`、`halBuffAllocAlignEx`、`drvDeviceGetIndexByPhyId` 等接口，所以必须链接 HAL。

---

### 4.2 rdma_lite 门面：ops 函数指针表与 API 版本

#### 4.2.1 概念说明

`rdma_lite.c` 是 Lite 驱动对外的「门面（facade）」。它的设计极其规整：每一个对外导出的 `rdma_lite_*` 函数都遵循同一个模板——取出一张函数指针表 `g_hns_roce_lite_ops`，调用表里同名的实现函数，做一点点公共善后（比如回填上下文指针）就返回。

为什么要这样设计？因为 Lite 想把「公共接口契约」与「具体硬件实现」解耦：

- **接口层**（`rdma_lite.h` / `rdma_lite.c`）定义稳定的对外 API，上层只认 `rdma_lite_post_send` 这类符号。
- **实现层**（`hns_roce_lite.c`）针对 HNS RoCE 这一种硬件写具体逻辑。
- 二者之间用 `struct rdma_lite_ops` 这张表桥接。将来换一种 RoCE 硬件，只需再写一张 ops 表，门面层一行都不用改。

这与 u3-l1 讲过的「表驱动」、u3-l5 讲过的「函数指针表＋枚举下标」是同一种工程手法。另外，门面还管理 API 版本号，方便上层做兼容判断。

#### 4.2.2 核心流程

门面的转发模板（以 `rdma_lite_post_send` 为例）：

```text
rdma_lite_post_send(qp, wr, bad_wr, attr, resp)
        │  get_hns_roce_lite_ops()  ← 取全局 ops 表指针
        ▼
   ops->rdma_lite_post_send(qp, wr, bad_wr, attr, resp)   ← 转给 hns_roce_lite_post_send
```

API 版本号用三个段拼成一个 24 位整数：

\[
\text{LITE\_API\_VERSION} = (\text{major} \ll 16) \;|\; (\text{minor} \ll 8) \;|\; \text{patch}
\]

规则（见 [rdma_lite.h:17-26](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L17-L26)）：删 API 或改 API 名才升 major；新增 API 升 minor；改枚举/结构体字段升 patch。

#### 4.2.3 源码精读

门面取表与转发的核心代码极短：

- [rdma_lite.c:14-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.c#L14-L17)：`get_hns_roce_lite_ops()` 直接返回全局表 `g_hns_roce_lite_ops` 的地址。
- [rdma_lite.c:106-112](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.c#L106-L112)：`rdma_lite_post_send` 标准三行——取表、调表里的 `post_send`、返回。这就是全部门面逻辑。
- [rdma_lite.c:64-82](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.c#L64-L82)：`rdma_lite_create_qp` 是少数带「公共善后」的门面——转发后回填 `ctx`、`send_cq`、`recv_cq`、`qp_type`、`qp_state`，让上层拿到的 `rdma_lite_qp` 是个完整对象。

ops 表本身与全局实例的定义在头文件里：

- [rdma_lite.h:314-343](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L314-L343)：`struct rdma_lite_ops`，每个字段都是一个函数指针，与对外 API 一一对应。
- [rdma_lite.h:345-347](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L345-L347)：`extern struct rdma_lite_ops g_hns_roce_lite_ops;` 声明全局实例，`get_hns_roce_lite_ops` 返回它。真正的实例化在 [hns_roce_lite.c:1371-1387](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1371-L1387)，用 C99 指定初始化器把每个字段指到 `hns_roce_lite_*` 实现函数。

对外导出用 `LITE_ATTRI_VISI_DEF` 宏（[rdma_lite.h:29](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L29)），它展开为 `__attribute__((visibility("default")))`，配合编译选项 `-fvisibility=hidden`（见 host_lite CMakeLists）实现「只导出 `rdma_lite_*` 公共符号、其余函数对外不可见」。

#### 4.2.4 代码实践

**实践目标**：验证「门面 = 转发」的结构，并理解 ops 表如何把接口与实现解耦。

**操作步骤**：

1. 在 [rdma_lite.h:314-343](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L314-L343) 的 `struct rdma_lite_ops` 中逐字段看，例如 `rdma_lite_post_send`、`rdma_lite_poll_cq`、`rdma_lite_create_qp`。
2. 在 [hns_roce_lite.c:1371-1387](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1371-L1387) 的 `g_hns_roce_lite_ops` 实例里，确认每个字段都指向同名的 `hns_roce_lite_*` 函数。
3. 在 [rdma_lite.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.c) 中任选一个门面函数（如 `rdma_lite_poll_cq`，91-96 行），核对其逻辑确实只是「取表 + 转发」。

**需要观察的现象 / 预期结果**：门面层不含任何硬件相关分支；所有硬件差异都被封装在 ops 表背后。

> 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果要把 Lite 驱动移植到另一种（非 HNS）RoCE 硬件，按现有架构需要改哪些地方？

**参考答案**：新写一组 `xxx_roce_lite_*` 实现函数并实例化一张新的 `struct rdma_lite_ops` 表，再让 `get_hns_roce_lite_ops`（或一个新取表函数）返回新表即可；门面层 `rdma_lite.c` 与公共头 `rdma_lite.h` 完全不用动。

**练习 2**：调用 `rdma_lite_get_api_version()` 返回 `0x000001`，对照 [rdma_lite.h:23-26](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L23-L26) 解释其含义。

**参考答案**：major=0、minor=0、patch=1，表示当前是初版 API（未发生删除/改名，故 major 仍为 0；patch=1 说明结构体/枚举字段有过一次兼容性微调）。

---

### 4.3 hns_roce_lite 控制面：Device→Host 上下文重建

#### 4.3.1 概念说明

本模块讲控制面的核心动作：**重建上下文**。「重建」不是新建硬件资源，而是把 Device 侧已存在的队列缓冲、doorbell 缓冲**映射（mmap）到 Host 进程地址空间**，让 Host 拿到能直接读写的指针（HVA）。

这件事的关键底座是 HAL 的 `halHostRegister`（[ascend_hal_base.h:2586](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2586)）：给它一个 Device 侧虚拟地址（DVA）、长度、标志（`DEV_MEM_MAP_HOST | MEM_REGISTER_HCCP_PROC_TYPE`）和 `devid`，它返回一个 Host 侧虚拟地址（HVA），此后 Host 读写 HVA 就等于读写对应的 Device 内存。这正好承接 u3-l1（HAL 提供 `hal*` 内存接口）与 u4-l1（Host↔Device 经字符设备/内存映射交互）。

控制面还有两个工程细节值得注意：

1. **doorbell 去重**：多个 QP/CQ 可能共享同一页 doorbell 内存，Lite 用引用计数链表避免重复 mmap / 误释放。
2. **大页内存池**：当 `page_size` 不是 4KB（典型为 2MB 大页）时，Lite 不为每个小缓冲单独 mmap（开销大），而是先注册一整段 Device 内存做「池」，后续小缓冲从池里切地址。

#### 4.3.2 核心流程

重建一个 QP 上下文的过程（`rdma_lite_create_qp` → `hns_roce_lite_create_qp`）：

```text
1. 校验 attr（必须是 RC 类型，见 hns_roce_verify_lite_qp）
2. calloc 一个 hns_roce_lite_qp
3. hns_roce_lite_create_qp_init:
   a. hns_roce_set_lite_qp_attr   ── 把 device_*_attr 快照拷进 qp 的 sq/rq/sge 字段
   b. hns_roce_init_lite_qp_indices ── head=tail=0
   c. mmap qp->buf  (QP 的 WQE 环形缓冲 DVA → HVA)
   d. mmap qp->sdb_buf (SQ doorbell DVA → HVA，走去重表)
   e. mmap qp->rdb_buf (RQ doorbell DVA → HVA，走去重表)
   f. 把 qp 按 qpn 存进 ctx->qp_table（供 poll_cq 反查）
4. 返回 &qp->lite_qp
```

CQ 的重建类似，只是要映射两块：CQ 的 CQE 缓冲（`cq_buf`）和「软件 doorbell」缓冲（`swdb_buf`，Host 把消费进度 cons_index 写进去告诉硬件「我读到哪了」）。

#### 4.3.3 源码精读

**把 DVA 映射成 HVA 的唯一入口**——`hns_roce_lite_mmap_host_va`：

- [hns_roce_lite_stdio.c:145-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L145-L163)：先把 DVA 按 `page_size` 向下对齐、长度向上对齐（mmap 必须页对齐），再调 `halHostRegister` 拿到对齐后的 HVA，最后用 `(align_va ^ device_va) + dst_addr` 把页内偏移补回去。这就是「Device 内存映射到 Host」的全部魔法，底层是 HAL。

**doorbell 去重**：

- [hns_roce_lite_stdio.c:183-223](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L183-L223)：`hns_roce_lite_mmap_db` 遍历 `ctx->db_list`，若某节点 `db_align_dva` 命中，就复用其 `hva` 并 `ref_cnt++` 直接返回；否则才真正 mmap 一次并挂进链表。对应的解注册 [hns_roce_lite_stdio.c:225-253](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L225-L253) 在 `ref_cnt` 归零时才真正 `halHostUnregisterEx`。

**QP 上下文重建**：

- [hns_roce_lite.c:371-459](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L371-L459)：`hns_roce_lite_create_qp_init` 顺序 mmap 三块（QP buf、SQ doorbell、RQ doorbell），失败则按 `goto` 标签逆序回滚（与 u4-l2 讲的「申请逆序释放」同理——先建的后拆）。其中 `qp->buf` 走 `hns_roce_lite_mmap_hva`（大页时从内存池切地址，4KB 时直接 mmap，见 [hns_roce_lite_stdio.c:255-294](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L255-L294)），doorbell 走 `hns_roce_lite_mmap_hdb`（带去重）。
- [hns_roce_lite_stdio.c:39-78](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L39-L78)：`hns_roce_set_lite_qp_attr` 把 `device_qp_attr` 里的 `wqe_shift`、`wqe_cnt`、`offset`、`max_gs` 等字段逐个拷进 Host 侧 `qp` 结构——这就是「快照落地」，Host 从此掌握了硬件队列的几何尺寸。

**CQ 上下文重建**：

- [hns_roce_lite.c:221-279](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L221-L279)：`hns_roce_lite_init_cq` 校验「必须支持 Record DB」（[hns_roce_lite.c:233-236](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L233-L236)，Lite 只支持软件记录 doorbell 模式），随后分别 mmap `cq_buf`（CQE 缓冲）和 `swdb_buf`（软件 doorbell）。

**容器技巧**：[hns_roce_lite.h:307-320](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.h#L307-L320) 用 `container_of` 在「公共基类 `rdma_lite_qp`」与「硬件私有 `hns_roce_lite_qp`」之间互转，正是门面层只认公共类型、实现层用私有类型的纽带。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「DVA → HVA」映射，确认它最终落到 HAL 的 `halHostRegister`。

**操作步骤**：

1. 从 [hns_roce_lite.c:390](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L390) 的 `hns_roce_lite_mmap_hva(&attr->device_qp_attr.qp_buf, &qp->buf, context)` 出发。
2. 进入 [hns_roce_lite_stdio.c:255-294](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L255-L294)，看 4KB 分支调用 `hns_roce_lite_mmap_host_va`。
3. 进入 [hns_roce_lite_stdio.c:145-163](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite_stdio.c#L145-L163)，看到它调用 [ascend_hal_base.h:2586](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2586) 的 `halHostRegister`。
4. 在一处加一行（仅用于阅读理解，**不要真的修改提交**）：在 `hns_roce_lite_mmap_host_va` 返回前，`roce_err` 打印 `device_va` 与返回的 `dst_addr`，观察映射关系。

**需要观察的现象 / 预期结果**：每次 `rdma_lite_create_qp` 成功后，`qp->buf.hva`、`qp->sdb_buf.hva` 都是非 NULL 的 Host 可写地址；之后对它们的读写等同于操作 Device 队列/doorbell。

> 实际运行需要 NPU + RoCE 硬件与已建好的 Device 侧资源，运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 doorbell 映射要走 `db_list` 引用计数去重，而普通 QP buf 不用？

**参考答案**：多个 QP/CQ 的 doorbell 常落在同一物理页内，重复 `halHostRegister` 同一页会浪费资源且释放时容易误释放共享页；去重 + 引用计数保证「同一对齐页只映射一次，最后一个使用者释放时才真正解注册」。QP buf 是每个 QP 独有的大块队列缓冲，不存在共享，故无需去重。

**练习 2**：`hns_roce_lite_create_qp_init` 里三块映射（buf/sdb/rdb）如果第三块失败，前两块会怎样？

**参考答案**：按 `goto` 标签 `mmap_rdb_buf_err → mmap_sdb_buf_err → alloc_wr_id_err` 逆序回滚，已映射的 sdb、buf 会被对应 `unmmap`，`wrid` 会被 free，保证不泄漏。

---

### 4.4 hns_roce_lite 数据面：WR 下发与 CQ 轮询

#### 4.4.1 概念说明

数据面是 Lite 驱动性能收益的兑现处。两个高频函数：

- **`rdma_lite_post_send`**：用户提交一条链表形式的 WR，驱动把它翻译成硬件认识的 WQE 写进 SQ 环形缓冲，推进队列 head 指针，再写 doorbell 通知硬件。
- **`rdma_lite_poll_cq`**：用户轮询 CQ，驱动读 CQE、解析出完成状态与 opcode、推进消费索引 `cons_index`，并把新的消费进度回写进软件 doorbell（`swdb_buf`）告诉硬件。

关键点：**SQE 缓冲、doorbell、CQE 缓冲都已经在 4.3 里映射成了 Host HVA**，所以这两个函数全程在用户态读写内存，不陷内核。这正是「数据面 lite」的全部含义。

WR 与 WQE 是两种东西：WR 是面向用户的结构体 [rdma_lite.h:240-250](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/rdma_lite.h#L240-L250)（含 opcode、sg_list、remote_addr、rkey 等）；WQE 是面向硬件的二进制结构 [hns_roce_lite.h:274-286](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.h#L274-L286)（`byte_4`/`msg_len`/`rkey`/`va` 等，字段按硬件手册的位布局填）。post_send 的核心就是把前者翻译成后者。

#### 4.4.2 核心流程

**post_send 一条 WR 的旅程**（[hns_roce_lite.c:1178-1237](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1178-L1237)）：

```text
持 sq.lock
for 链表上每条 wr:
    1. check_send_wr     ── 队列是否溢出、sge 数是否超限
    2. wqe_idx = (head + nreq) & (wqe_cnt - 1)        ── 算环形槽位
    3. wqe = qp->buf.hva + sq.offset + (wqe_idx << wqe_shift)  ── 直接算 HVA
    4. wrid[wqe_idx] = wr->wr_id                       ── 存用户上下文，poll 时回带
    5. set_wqe(wqe, ...)  ── 把 WR 翻译成硬件 WQE，memcpy 进映射缓冲
head += nreq
if SQ_RECORD_DB:  *(u32*)qp->sdb_buf.hva = head & 0xffff   ── 敲门铃！
get_rsp(qp, resp)  ── 把 doorbell 内容回填给调用者（部分 QP 模式由上层自己敲）
释放 sq.lock
```

doorbell 内容由 [hns_roce_lite.c:1165-1176](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1165-L1176) 组装（仅 OP/GDR_ASYN/OP_EXT 模式）：

\[
\text{lite\_db\_info} = (\text{sl} \ll 48)\;|\;(\text{pi} \ll 32)\;|\;(0 \ll 24)\;|\;\text{qpn}
\]

其中 `pi = head & ((wqe_cnt << 1) - 1)` 是生产者索引。

**poll_cq 取一个完成**（[hns_roce_lite.c:777-831](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L777-L831)）：靠硬件维护的 **owner bit**（所有权位）区分「新 CQE」与「已读旧 CQE」。判定式见 [hns_roce_lite.c:546-551](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L546-L551)：当 CQE 里的 owner 位与「期望值」不一致时表示无新完成，返回 `CQ_EMPTY`；一致则消费它，`cons_index++`。循环结束后把 `cons_index` 回写进 `swdb_buf`（[hns_roce_lite.c:858-860](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L858-L860)）。

#### 4.4.3 源码精读

**WR → WQE 翻译主函数**：

- [hns_roce_lite.c:1084-1145](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1084-L1145)：`hns_roce_lite_set_rc_wqe` 先 `calloc` 一块临时 WQE，调 `check_rc_opcode` 填 opcode/地址/rkey/信号位，调 `set_sge` 填数据段，最后 `memcpy_s` 拷进映射的 SQE 缓冲 `send_wqe`，并翻转 owner 位（[hns_roce_lite.c:1140](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1140)——硬件约定「写完后把 owner 位取反表示软件已写完」）。

**opcode 映射表**：

- [hns_roce_lite.h:322-348](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.h#L322-L348)：`hns_roce_lite_op_code[]` 把面向用户的 `RDMA_LITE_WR_*` 映射成硬件 `HNS_ROCE_WQE_OP_*`，`to_hr_lite_opcode` 用下标 O(1) 查表。注意它支持 `REDUCE_WRITE`/`WRITE_WITH_NOTIFY`/`ATOMIC_WRITE` 等面向集合通信的扩展 opcode，这正是 README 所说「组合下发 WR 实现高级集合算子」的落点。

**直接写队列缓冲**：

- [hns_roce_lite.c:957-960](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L957-L960)：`get_send_lite_wqe` 直接用 `qp->buf.hva + sq.offset + (n << wqe_shift)` 算出 WQE 的 Host 地址——没有任何系统调用，纯指针运算。这就是「直接写入 Device 侧队列」在代码层面的样子。

**敲 doorbell**：

- [hns_roce_lite.c:1228-1230](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1228-L1230)：`*(u32 *)qp->sdb_buf.hva = qp->sq.head & 0xffff;`——一句内存写就完成了「通知硬件」。`sdb_buf.hva` 是 4.3 里映射好的 SQ doorbell。

**poll_cq 错误处理**：

- [hns_roce_lite.c:603-639](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L603-L639)：`hns_roce_lite_handle_error_cqe` 用一张 `{cqe_status, wc_status}` 表把硬件错误码翻译成对外的 `RDMA_LITE_WC_*`，找不到则归为 `RDMA_LITE_WC_GENERAL_ERR`。

**v2 版完成**：[hns_roce_lite.c:867-906](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L867-L906) 的 `poll_cq_v2` 额外带回 `rdma_lite_wc_ext`（含 imm_data / invalidated_rkey），并把 `ext->version` 置为 `LITE_WC_EXT_VERSION`，是结构体演进的典型做法。

#### 4.4.4 代码实践

**实践目标**：手动算出一个 WQE 落点与一个 doorbell 值，确认你读懂了位运算。

**操作步骤**：

1. 假设某 QP 的 `sq.wqe_cnt=128`、`sq.wqe_shift=6`（即每个 WQE 64 字节）、`sq.offset` 为某值、当前 `head=5`，下发第 `nreq=0` 条 WR。按 [hns_roce_lite.c:1213-1214](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1213-L1214) 与 [957-960](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L957-L960)，手算 `wqe_idx` 与 WQE 相对 `buf.hva` 的偏移。
2. 假设该 QP 处于 OP 模式（`gdr_enabled == HNS_ROCE_QP_AI_MODE_OP`）、`sl=0`、`qpn=0x10`，`head` 下发后变 6。按 [hns_roce_lite.c:1165-1176](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1165-L1176) 手算 `pi` 与 `lite_db_info`，再与代码逻辑核对。

**需要观察的现象 / 预期结果**：

- `wqe_idx = (5+0) & (128-1) = 5`；WQE 偏移 = `sq.offset + (5 << 6) = sq.offset + 320`。
- `pi = 6 & ((128<<1)-1) = 6`；`lite_db_info = (0<<48)|(6<<32)|(0<<24)|0x10`。

> 计算可在纸面完成；实际在硬件上观察 doorbell 寄存器变化需要专用工具，运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：post_send 里 `wrid[wqe_idx] = wr->wr_id` 这一行存的东西，poll_cq 时怎么用？

**参考答案**：poll_cq 在 `hns_roce_lite_poll_one_set_wc`（[hns_roce_lite.c:571-601](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L571-L601)）里用 `tail` 索引 `wrid[]` 取回当初存的 `wr_id`，填进 `lite_wc->wr_id` 返回给用户，从而让用户把「某个完成」与「当初提交的某个请求」对应起来（典型用法：把 buffer 指针编码进 wr_id）。

**练习 2**：poll_cq 为什么用 owner 位而不是简单看「CQE 是否非空」？

**参考答案**：CQ 是环形缓冲，CQE 槽位会被反复复用，硬件无法「清空」一个 CQE，只能翻转其中的 owner 位表示「这一格我又写了新数据」。软件据此与自己记录的 `cons_index` 奇偶性比较，才能区分「新完成」与「上轮已读的旧数据」。

---

### 4.5 roce_hal_api：Device 内存申请与设备号桥接

#### 4.5.1 概念说明

`roce_hal_api/` 提供两类能力：

1. **为 RoCE 申请内存**：包括 Host 侧 NUMA 感知的普通缓冲（给用户接收/发送用）、Device 侧 AI 缓冲（给 NPU 用）、以及「预留内存（resv_mem）」——后者专门支撑 4.3 里大页内存池场景，用两阶段 populate 建立映射。
2. **设备号桥接**：把 RoCE 物理端口号 `phy_id` 翻译成 HAL 全局逻辑 `devid`。

这个模块之所以重要，是因为 Lite 驱动自己不直接碰字符设备与物理内存，它复用 HAL 既有设施。其中 `hns_roce_hal_get_dev_id` 用到的 `halGetDeviceInfo` / `drvGetDevNum` / `drvGetDeviceLocalIDs` 正是 u3-l1/u3-l3 讲过的 HAL 设备信息查询与 UDA 设备号体系——这里是 RoCE 视角接入 UDA 的具体落点。

> 该目录的 CMakeLists 仅在 `BUILD_COMPONENT == DRIVER_COMPAT` 时才编译（见 [roce/CMakeLists.txt:11-14](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/CMakeLists.txt#L11-L14)），说明它属于兼容性构建的一部分。

#### 4.5.2 核心流程

**NUMA 感知申请 Host 缓冲**（[hns_roce_hal.c:26-112](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L26-L112)）：

```text
1. halMemGetInfo(dev_id, MEM_INFO_TYPE_BAR_NUMA_INFO, &info)  ── 查 NPU 所在的 NUMA 节点
2. 由 info.numa_info.node_id[] 构造 node_mask 位图
3. mmap一段 MAP_ANONYMOUS 内存（2MB 页时加 MAP_HUGETLB）
4. syscall(__NR_mbind, ..., MPOL_BIND, node_mask, ...)  ── 把内存绑定到 NPU 同侧 NUMA 节点
5. memset_s 清零
```

思路：让 Host 缓冲与 NPU 落在同一 NUMA 节点，避免跨节点访问的延迟——这是高性能 RDMA 的常见优化。

**预留内存两阶段 populate**（[hns_roce_hal.c:207-258](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L207-L258)）：先 `mmap` 把 `cmd_fd` 的 `RESV_MEM_MMAP_PGOFF` 段映射成 Host VA（仅占地址空间），再 `ioctl(HNS_ROCE_AI_RESV_MEM_POPULATE)` 让内核真正建立 Device 物理页映射。这与 u4-l1/u4-l2 讲的 SVM「先预留 VA、再 populate 物理页」是同一个两阶段思想。

**phy_id → devid**（[hns_roce_hal.c:114-173](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L114-L173)）：枚举所有设备，逐个 `halGetDeviceInfo(... INFO_TYPE_PHY_CHIP_ID / INFO_TYPE_PHY_DIE_ID ...)`，找到 `(chip_id, die_id)` 匹配的那台，返回其 `devid`。

#### 4.5.3 源码精读

**ioctl 命令定义**（[hns_roce_hal.h:19-42](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.h#L19-L42)）：用 `_IOWR('A', 0xD/0xE, struct)` 定义 populate/depopulate 两个命令号，`RESV_MEM_MMAP_PGOFF = 5120 << 12` 是 mmap 的偏移（页偏移）。

**NUMA 绑定的细节坑**（[hns_roce_hal.c:78-79](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L78-L79)）：`max_node += 2` 注释「avoid kernel get_nodes maxnode param high-order truncation bug」——这是个真实的历史坑：`mbind` 的 `maxnode` 参数高位会被内核截断，多加 2 规避。这类注释是源码阅读的宝藏，反映了工程上的踩坑经验。

**Device AI 缓冲申请**（[hns_roce_hal.c:175-200](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L175-L200)）：`hns_roce_hal_alloc_ai_buf` 把 `dev_id` 编码进 flag（`(devid << BUFF_FLAGS_DEVID_OFFSET) | BUFF_SP_SVM`），调用 [ascend_hal_base.h:4137](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L4137) 的 `halBuffAllocAlignEx` 申请——这把 RoCE 的内存需求接到了 HAL 的 buff（共享内存）子系统，与 u4-l4 的 SVM/buff 体系呼应。

#### 4.5.4 代码实践

**实践目标**：理解 RoCE 如何复用 HAL 申请内存、并比较「预留内存两阶段」与 SVM 的同构性。

**操作步骤**：

1. 阅读 [hns_roce_hal.c:207-258](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/roce_hal_api/hns_roce_hal.c#L207-L258)，识别「阶段一 mmap（占地址）」与「阶段二 ioctl populate（建物理映射）」两步。
2. 对照 u4-l2 讲的 `halMemAlloc` 的 `VA_ONLY` / `POPULATE_ONLY` 两阶段，写出二者的同构对应关系。
3. 跟踪 [hns_roce_lite.c:55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L55) 的 `drvDeviceGetIndexByPhyId(phy_id, &ctx->dev_id)`，结合 4.5.2 理解 phy_id 到 devid 的桥接意义。

**需要观察的现象 / 预期结果**：能说出「resv_mem 的 mmap + populate」与「SVM 的 reserve VA + populate 物理页」是同一种「先借地址、再填实」的模式，只是 ioctl 号与字符设备不同。

> 本实践为源码阅读型，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：`hns_roce_hal_alloc_buf` 为什么要 `mbind` 把内存绑到某个 NUMA 节点？

**参考答案**：因为 NPU 挂载在特定的 NUMA 节点上，RDMA 数据搬运若跨 NUMA 节点访问 Host 内存会有额外延迟；用 `MPOL_BIND` 把缓冲绑定到 NPU 同侧节点，能保证 DMA 路径最短，这是 RDMA 性能调优的基本操作。

**练习 2**：`hns_roce_hal_get_dev_id` 与 `drvDeviceGetIndexByPhyId`（[hns_roce_lite.c:55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L55)）解决的是同一个问题吗？

**参考答案**：是同一类问题——都是把「RoCE/芯片的物理标识」翻译成「HAL 全局逻辑 devid」。前者用 `(chip_id, die_id)` 遍历匹配，后者直接用 `phy_id` 查表；二者都依赖 HAL/UDA 的设备号体系（u3-l3），目的都是让后续 `halHostRegister` 等 `hal*` 调用知道该操作哪台 NPU。

---

## 5. 综合实践

**任务**：还原一次完整的「RDMA Write」端到端数据面调用，把本讲四个模块串起来。

请按顺序回答 / 跟踪：

1. **建上下文**（模块 4.2 + 4.5）：上层拿到一个 RoCE 物理端口 `phy_id`，调用 `rdma_lite_alloc_context`。跟踪到 [hns_roce_lite.c:55](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L55)，说明它如何把 `phy_id` 变成 `ctx->dev_id`，这个 `dev_id` 后续会传给哪个 HAL 函数。

2. **重建 QP**（模块 4.3）：调用 `rdma_lite_create_qp`，Device 侧已通过「属性快照」给出了 `qpn` 与 `qp_buf`/`sq.db_buf` 的 DVA。说明 [hns_roce_lite.c:390-408](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L390-L408) 把这三块 DVA 分别映射成哪些 HVA、最终落到 [ascend_hal_base.h:2586](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2586) 的哪个 HAL 接口。

3. **下发一条 RDMA Write**（模块 4.4）：构造一条 `opcode = RDMA_LITE_WR_RDMA_WRITE`、带一个 sge、`send_flags = RDMA_LITE_SEND_SIGNALED` 的 WR，调用 `rdma_lite_post_send`。说明：
   - WR 在 [hns_roce_lite.c:1084-1145](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1084-L1145) 被翻译成硬件 WQE 的哪几个字段；
   - WQE 被写进哪个 HVA（[hns_roce_lite.c:957-960](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L957-L960)）；
   - doorbell 在 [hns_roce_lite.c:1228-1230](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L1228-L1230) 怎么被敲响。

4. **收完成**（模块 4.4）：调用 `rdma_lite_poll_cq`。说明 [hns_roce_lite.c:777-831](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/roce/host_lite/hns_roce_lite.c#L777-L831) 如何靠 owner 位发现新 CQE、如何把当初的 `wr_id` 还给用户。

5. **画一张图**：把上述四步画成时序图，标出「Host 用户态」「映射的 Device 内存（HVA）」「HAL/内核」「RoCE 硬件」四个泳道，重点标出哪几处是「纯用户态内存读写、不陷内核」。

**预期产出**：一张时序图 + 一段文字说明。如果你能指出「除控制面建上下文时调过 `halHostRegister` 外，数据面 post_send/poll_cq 全程没有系统调用」，就说明你真正理解了 Lite 的性能模型。

---

## 6. 本讲小结

- RoCE Lite 把通信切成**控制面**（把 Device 已建好的队列/门铃/CQ 内存映射回 Host，重建上下文）与**数据面**（Host 直接写队列、直接敲门铃、直接轮询 CQ），从而省掉标准 verbs 与内核的开销。
- `rdma_lite.c` 是纯转发的**门面**，靠 `struct rdma_lite_ops` 函数指针表把公共 API 与 HNS 硬件实现解耦；换硬件只需换一张 ops 表。
- 控制面重建的关键是 `halHostRegister`（HAL），把 Device DVA 映射成 Host HVA；doorbell 走引用计数去重，大页场景走内存池切地址。
- 数据面 `post_send` 把 WR 翻译成硬件 WQE、按环形槽位写入映射缓冲、推进 head、写 doorbell；`poll_cq` 靠 owner 位识别新 CQE、推进 cons_index、回写软件 doorbell——全程用户态内存读写。
- Lite 驱动编译为 `libascend_rdma_lite.so`，链接 `libascend_hal.so`；所有设备访问（内存映射、内存申请、设备号翻译）都复用 HAL 的 `hal*` 接口。
- `roce_hal_api` 提供 NUMA 感知 Host 缓冲、Device AI 缓冲、预留内存两阶段 populate，以及 `phy_id → devid` 桥接，是 RoCE 接入 HAL/UDA 体系的具体落点。
- 集合通信（如 allgather）通过组合下发 WR（含 `REDUCE_WRITE`/`WRITE_WITH_NOTIFY` 等扩展 opcode）实现高级算子，这是 Lite 的核心应用场景。

---

## 7. 下一步学习建议

- **横向对照 SVM 的两阶段内存模型**：本讲 4.5 的「resv_mem mmap + populate」与 u4-l2/u4-l3 的 SVM `VA_ONLY`/`POPULATE_ONLY` 是同构的，建议重读 [u4-l2](u4-l2-svm-alloc-free.md) 加深对「先借地址、再填物理页」这一通用模式的理解。
- **纵向深入 NDA/灵渠 RDMA 扩展**：u7-l4 会讲到 `nda/ibv_extend` 的 V3 接口与高阶 RoCE 协商能力、lag port 接口，那是 RoCE 在控制面/协商面的另一条定制线，与本讲的「数据面 lite」互补。
- **回到 HAL 通信底座**：本讲多次提到 `halHostRegister`、`halBuffAllocAlignEx`、`halGetDeviceInfo`，如果想看这些接口的声明全貌与分类，重读 [u3-l1](u3-l1-hal-overview-and-api.md)；想理解 Host↔Device 内存映射的底层机制，可继续看 HDC（u3-l2）与 SVM（单元 4）。
- **阅读设备侧 rdma-core 扩展头**：`src/ascend_hal/roce/inc_extend/hns_roce_user/` 下的 `verbs_exp.h`、`peer_ops.h` 给出了扩展 verbs 接口，是理解「Device 侧如何建好上下文、再把属性快照传给 Host」的另一半拼图，建议作为进阶阅读。
