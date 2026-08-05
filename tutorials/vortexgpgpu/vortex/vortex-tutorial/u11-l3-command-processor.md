# 命令处理器与 KMU

## 1. 本讲目标

本讲把前面几讲里反复出现的「主机写一组 DCR、然后启动内核」这件事，从黑盒彻底打开。读完本讲，你应当能够：

- 说清 **命令处理器（Command Processor，CP）** 的整体结构：AXI-Lite 寄存器接口、主机驻留命令环（command ring）、命令格式，以及它把每条命令分流到「KMU / DMA / DCR / EVENT」四个共享资源的机制。
- 画出每队列引擎（CPE）的状态机 `IDLE→DECODE→BID→WAIT_DONE→RETIRE`，并解释完成号（seqnum）如何安全地回写主机。
- 解释 **KMU（Kernel Management Unit，内核管理单元）** 如何从一次 `CMD_LAUNCH` 进展到「脉冲 start → 等 busy 拉高 → 等 busy 拉低（drain）」的完整生命周期。
- 看懂 KMU 如何根据 `grid/block/cluster` 维度把一个网格遍历成一串 CTA 请求，并理解 `cta_id`、`block_idx`、`is_first_of_cluster` 这些字段的来源。
- 对照 RTL CP（`hw/rtl/cp/`）与仿真 CP（`sim/common/cmd_processor.cpp`）两套实现，知道它们的对应关系与当前已知的分歧点（VM、QMD launch 等）。

本讲承接 u3-l4（主机如何加载 `.vxbin` 并写 KMU DCR）与 u6-l1（CTA 如何被拆成 warp），把二者之间那条「命令处理器 → KMU → CTA」的控制链路补齐。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**（1）主机从不直接写设备内存，也从不直接启动核。** 回顾 u3-l2：主机与设备之间唯一的控制通路是 **CP（命令处理器）**，唯一的搬运工是 **CP 的 DMA 引擎**。主机做的事只有两类：往 CP 的 AXI-Lite 寄存器里写控制字、往「CP 可见的主机内存」里写命令环。真正去戳设备 DCR、去搬数据、去拉起内核的，是 CP 自己。所以「启动一次内核」本质上是「主机往环里写一串命令，再敲一下门铃（doorbell）」。

**（2）命令环在主机内存里，不在设备里。** 这是一个工程取舍：环放在主机 pin 住的内存里，CP 通过它独占的 `axi_host` 端口去取（一次取一个 64 字节 cache line）。这样做的好处是主机追加命令就是普通的 `memcpy`，不需要每条命令都做一次 MMIO；坏处是 CP 必须有读主机内存的能力。这条决策记录在设计文档里（见 [command_processor.md:427-432](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L427-L432)，明确说「设备内存环 + 每命令 DMA」的旧方案被废弃）。

**（3）CTA 是 GPU 的调度单位。** 一个 CTA（Cooperative Thread Array，对应 CUDA 的 thread block）是一组会共享本地内存（LMEM）和屏障的线程。主机用 `grid_dim × block_dim` 描述一次 launch：`block_dim` 是一个 CTA 内的线程数，`grid_dim` 是 CTA 的个数。KMU 的职责就是把 `grid` 这个三维计数空间**展开成一串 CTA 请求**，每个 CTA 带着自己的 `block_idx`。注意本讲里的 **cluster**（`cluster_dim`）是 **CTA 簇**——一组协作的 CTA，用于 DXA 多播与跨 CTA 屏障（参见 u9-l2），**不要**和 u7-l1/u8-l1 里「cluster 共享 L2」的内存层次 cluster 混淆，两者同名但含义不同。

| 术语 | 全称 | 含义 |
|---|---|---|
| CP | Command Processor | 设备侧命令处理器，唯一控制通路 |
| CPE | CP Engine | 每队列一个的命令引擎 |
| KMU | Kernel Management Unit | 内核管理单元，展开 grid 为 CTA |
| CTA | Cooperative Thread Array | 线程块，GPU 调度单位 |
| DCR | Device Control Register | 设备控制寄存器 |
| doorbell | — | 主机写 `Q_TAIL` 通知 CP「有新命令了」 |
| seqnum | sequence number | 每条命令退休时递增的完成号 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hw/rtl/cp/VX_cp_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv) | RTL CP 顶层：寄存器堆 + N×(fetch+engine) + 4 个仲裁器 + 5 个资源单元 + 双 AXI xbar |
| [hw/rtl/cp/VX_cp_pkg.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv) | CP 的公共定义：opcode、`cmd_t` 结构、资源枚举、命令字节长度表 |
| [hw/rtl/cp/VX_cp_engine.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv) | 每队列引擎 FSM：分类 opcode→资源、竞标、等完成、退休 |
| [hw/rtl/cp/VX_cp_launch.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv) | KMU 启动包装器：脉冲 start、握 busy、drain 后发 done |
| [sim/simx/kmu/kmu.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp) | SimX 的 KMU 模型：保存内核描述符、遍历 grid 产出 CTA |
| [sim/simx/kmu/kmu.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h) | `kmu_req_t` 结构与 `Kmu` 类声明 |
| [sim/common/cmd_processor.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp) | 仿真 CP 的 C++ 功能模型，RTL CP 的功能孪生 |
| [sim/simx/cta_dispatcher.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp) | 每核 CTA 分派器：从 KMU 拉 CTA、切成 warp |
| [sw/runtime/common/queue.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp) | 主机运行时：把一次 launch 编码成一串 `CMD_DCR_WRITE` + `CMD_LAUNCH` |
| [docs/designs/command_processor.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md) | CP 设计文档（架构、寄存器图、已知 gap） |

---

## 4. 核心概念与源码讲解

### 4.1 CP 顶层架构与命令格式

#### 4.1.1 概念说明

CP 是 Vortex 的**单一控制平面**：主机提交给 GPU 的所有工作——内存搬运、DCR 编程、内核启动、栅栏、事件、缓存维护——都走这一条路。在 FPGA 目标（XRT / OPAE）上它甚至是**唯一**的 launch/DCR 通路，旧的 AP_CTRL 启动状态机已被移除（见 [command_processor.md:17-21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L17-L21)）。

CP 的设计可以用一句话概括：**N 个并行命令引擎（每队列一个），喂给 4 个轮转仲裁器，仲裁器串行化对 4 个共享资源的访问**——内核管理单元（KMU 启动）、DMA 引擎、DCR 总线、事件单元。命令环在主机内存里；CP 用独占的 AXI 主端口去取环、去写完成号。

#### 4.1.2 核心流程

主机侧一次「提交命令」的全流程：

1. 运行时的每队列 worker 线程把若干命令编码进**一个 64 字节 cache line**，`memcpy` 进主机驻留的环缓冲。
2. 主机写 `Q_TAIL_LO` 暂存、写 `Q_TAIL_HI` **原子提交**（doorbell）。
3. CP 的 `VX_cp_fetch` 发现 `head < tail`，通过 `axi_host` 取回一个 64B line。
4. `VX_cp_unpack` 按 opcode 的字节长度逐条解出 `cmd_t`。
5. 该队列的 `VX_cp_engine` 把每条命令分类到一个资源，向对应仲裁器竞标（bid）。
6. 仲裁器轮转授权一个引擎，资源单元执行命令。
7. 命令完成后，`VX_cp_completion` 把 8 字节 seqnum 写回主机驻留的 `cmpl_addr`，seqnum 递增。
8. 主机 busy-poll `Q_SEQNUM`，看到它前进就知道命令已完成。

#### 4.1.3 源码精读

**顶层 `VX_cp_core`** 把所有零件拼起来。它的端口里最关键的是两个 AXI 主端口——`axi_host`（到主机内存）和 `axi_dev`（到设备内存），以及一个 AXI-Lite 从端口（到主机的 MMIO 寄存器）：

- 顶层模块声明与双 AXI 端口：[VX_cp_core.sv:77-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L77-L98)——说明 CP 对外就这三条路：一条 Lite 接收主机寄存器写、两条 AXI 取环/搬数据。
- 唯一的 AXI-Lite 从模块 `VX_cp_axil_regfile`：[VX_cp_core.sv:135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L135)——全局寄存器 + 每队列块都在这里，doorbell 在 `Q_TAIL_HI` 写时原子提交。
- 每队列一对 `VX_cp_fetch` + `VX_cp_engine`：[VX_cp_core.sv:186-197](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L186-L197)。
- 四个资源各一个轮转仲裁器：[VX_cp_core.sv:276-291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L276-L291)。
- 双 AXI xbar：host xbar（fetch+completion+DMA(host)→`axi_host`）在 [VX_cp_core.sv:389](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L389)，device xbar（DMA(dev)+event→`axi_dev`）在 [VX_cp_core.sv:440](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L440)。

**命令格式**定义在 `VX_cp_pkg.sv`。一条命令 = 4 字节头 + opcode 特定载荷，打包进 64 字节 cache line（每行最多 5 条，零头终止本行）：

- opcode 枚举：[VX_cp_pkg.sv:63-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L63-L75)——`CMD_LAUNCH = 0x06` 是本讲主角。
- 4 字节头 `{reserved[16], flags[8], opcode[8]}`：[VX_cp_pkg.sv:84-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L84-L88)。
- 解码后的命令记录 `cmd_t`（头 + 三个 64 位 arg + 可选 profile 槽）：[VX_cp_pkg.sv:97-103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L97-L103)。
- 每个 opcode 的在线字节长度表：[VX_cp_pkg.sv:183-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L183-L201)——注意 `CMD_LAUNCH` 只有 12 字节（头 + 一个 8B arg），这是它「轻量脉冲」性质的体现：真正的内核参数已经通过前面的 `CMD_DCR_WRITE` 写进 KMU 寄存器了，`CMD_LAUNCH` 只负责「点火」。

> **为何 launch 只有 12 字节？** 因为当前架构下，一次 launch 被编码成「约 18 条 `CMD_DCR_WRITE`（写满 KMU 的 STARTUP_ADDR/KERNEL_ENTRY/ARG/grid/block/cluster 等 DCR）+ 1 条 `CMD_LAUNCH`」。`CMD_LAUNCH` 自身不带参数，只是脉冲。设计文档把「用一条带内联参数的原子 `CMD_LAUNCH_QMD` 替代这 18 条 DCR 写」列为未来改进（见 4.5 节与 [command_processor.md:397-400](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L397-L400)）。

#### 4.1.4 代码实践

**目标**：建立「命令 = 头 + 载荷、打包进 64B 行」的直觉，并验证 RTL 与仿真两边用的是同一张命令表。

**操作步骤**：

1. 打开 [VX_cp_pkg.sv:183-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L183-L201) 的 `cmd_size_bytes` 表，记下每个 opcode 的字节数。
2. 打开仿真侧的对应实现 [cmd_processor.cpp:241-256](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L241-L256) 的 `decode_cmd_bytes` 里的 `switch (out.opcode)`。
3. 逐行对照两张表。

**需要观察的现象**：两边的字节数应当**逐 opcode 一致**（NOP=4、LAUNCH=12、FENCE=8、DCR_*=20、MEM_*/EVENT_WAIT=28）。仿真侧多了 `OP_LAUNCH_QMD`(0x0B) 和 `OP_DRAW`(0x0C) 两个 opcode（都是 12 字节），这是仿真 CP 已实现、RTL CP 尚未镜像的功能（见 4.5）。

**预期结果**：你能填出下面这张对照表，并指出仿真侧多出哪两个 opcode。

| opcode | RTL 字节 | 仿真字节 |
|---|---|---|
| CMD_LAUNCH | 12 | 12 |
| CMD_DCR_WRITE | ? | ? |
| CMD_MEM_WRITE | ? | ? |

（待本地验证：你填的 `?` 应分别是 20、28。）

#### 4.1.5 小练习与答案

**练习 1**：为什么命令环要放在主机内存而不是设备内存？如果放在设备内存，主机追加命令的开销会变成什么？

> **参考答案**：环放主机内存时，主机追加命令是一次普通 `memcpy`，零 MMIO；CP 用 `axi_host` 主动取。若放设备内存，主机每追加一条命令都要做一次 MMIO/DMA 写设备，开销高且 CP 仍要一段设备可见缓冲。设计文档明确废弃了「设备内存环 + 每命令 DMA」的旧方案（[command_processor.md:428-429](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L428-L429)）。

**练习 2**：一条 64 字节的 cache line 最多能装几条 `CMD_LAUNCH`？装几条 `CMD_MEM_WRITE`？

> **参考答案**：`CMD_LAUNCH` = 12 字节，\( \lfloor 64/12 \rfloor = 5 \) 条，正好是 `MAX_CMDS_PER_CL`；`CMD_MEM_WRITE` = 28 字节，\( \lfloor 64/28 \rfloor = 2 \) 条（剩余 8 字节放不下第三条，会被零头终止或填 NOP）。

---

### 4.2 引擎 FSM、资源仲裁与完成回写

#### 4.2.1 概念说明

每个队列有一个 **CPE（CP Engine）**，它是「消费命令、竞标资源、等完成、退休」的状态机。CP 之所以能支持多队列并发，是因为多个 CPE 可以同时手里各攥一条命令；而它之所以不会在共享资源上打架，是因为每个资源前面挡着一个**轮转仲裁器**——同一时刻只授权一个 CPE 使用该资源。

命令「做完」的标志不是 CPE 自己说了算，而是**资源单元**发回一个 `done` 脉冲。退休（retire）时，CPE 通过 valid/ready 握手把 seqnum 交给 `VX_cp_completion`，由后者统一写回主机——这个握手保证多个 CPE 同周期退休时不会丢号。

#### 4.2.2 核心流程

CPE 的状态机 `IDLE→DECODE→BID→WAIT_DONE→RETIRE`：

```
IDLE      --cmd_in_valid-->  拿到一条解码后的 cmd
DECODE    --classify-->      把 opcode 映射到 RES_KMU/DMA/DCR/EVT（或标记 no_resource）
BID       --grant-->         对所选资源抬 bid，等仲裁器授权
WAIT_DONE --done pulse-->    等资源单元发回 done
RETIRE    --retire_ready-->  握手把 seqnum 送出，seqnum+1，回 IDLE
```

opcode → 资源的映射是确定的（组合逻辑）：

| opcode | 资源 |
|---|---|
| `CMD_LAUNCH` | RES_KMU |
| `CMD_DCR_WRITE/READ`、`CMD_CACHE_FLUSH` | RES_DCR |
| `CMD_MEM_WRITE/READ/COPY` | RES_DMA |
| `CMD_EVENT_SIGNAL/WAIT` | RES_EVT |
| `CMD_NOP`、`CMD_FENCE` | 无资源（直接退休） |

#### 4.2.3 源码精读

**`VX_cp_engine` 的状态机**：[VX_cp_engine.sv:80-86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L80-L86) 定义五态；[VX_cp_engine.sv:126-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L126-L185) 是 FSM 主体。注意 `S_BID` 里按 `cur_res` 选四条 bid 线之一等授权，`S_WAIT_DONE` 里按 `cur_res` 选四个 done 脉冲之一：

```systemverilog
S_BID: begin
  case (cur_res)
    RES_KMU:   if (bid_kmu.grant)   fsm <= S_WAIT_DONE;
    RES_DMA:   if (bid_dma.grant)   fsm <= S_WAIT_DONE;
    RES_DCR:   if (bid_dcr.grant)   fsm <= S_WAIT_DONE;
    RES_EVT:   if (bid_event.grant) fsm <= S_WAIT_DONE;
    ...
```

**opcode→资源分类函数 `classify`**：[VX_cp_engine.sv:97-114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L97-L114)——未知 opcode 置 `skip=1`，直接走退休（当 NOP 处理）。

**完成回写的握手**——这是防丢号的关键：[VX_cp_engine.sv:173-181](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L173-L181)。`S_RETIRE` 会一直停在那里、持续抬 `retire_evt`，直到 `retire_ready_i` 被观测到才让 `seqnum_r` 自增并回 IDLE。这样即使两个 CPE 同周期想退休，`VX_cp_completion` 的 per-source 锁存也能逐个接住（参见设计文档 [command_processor.md:247-250](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L247-L250)）。

**资源枚举与 done 脉冲广播**：`cp_resource_e` 定义在 [VX_cp_pkg.sv:149-154](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L149-L154)。注释里有一句很关键的说明（[VX_cp_engine.sv:116-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L116-L120)）：done 脉冲是**广播**给所有 CPE 的，但因为「仲裁器同一时刻只授权一个 CPE、资源一次只处理一条命令」，所以只有那个正在 `S_WAIT_DONE` 的 CPE 会消费它，其余 CPE 忽略。

> **单队列退化**：默认 `VX_CP_NUM_QUEUES=1`（[VX_cp_pkg.sv:31-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L31-L33)），此时唯一的 CPE 永远赢仲裁，FSM 退化为顺序执行。仿真 CP 的 `tick_engine` 也注释了这一点（[cmd_processor.cpp:438-439](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L438-L439)：「Single-queue means we always win the arbiter」）。多队列并发是未来工作。

#### 4.2.4 代码实践

**目标**：在仿真侧跟踪一条 `CMD_LAUNCH` 走完引擎 FSM 的全过程，验证它与 RTL 的五态一一对应。

**操作步骤**：

1. 读 [cmd_processor.cpp:389-588](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L389-L588) 的 `tick_engine`，对照 `EngState` 枚举 [cmd_processor.h:126-127](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.h#L126-L127)。
2. 找到 `case EngState::Bid` 里对 `OP_LAUNCH` 的处理：[cmd_processor.cpp:440-448](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L440-L448)——它把 launch 子状态机置为 `PulseStart`，然后转 `WaitDone`。
3. 找到 `case EngState::WaitDone`：[cmd_processor.cpp:550-555](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L550-L555)——等 launch 子 FSM 回 Idle。
4. 找到 `case EngState::Retire`：[cmd_processor.cpp:581-586](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L581-L586)——`seqnum+1`、`publish_completion()`、回 Idle。

**需要观察的现象**：仿真侧的 `Idle→Decode→Bid→WaitDone→Retire` 与 RTL 的 `S_IDLE→S_DECODE→S_BID→S_WAIT_DONE→S_RETIRE` 是逐态对应的；唯一差别是仿真侧把 launch 的「脉冲/等 busy/等 drain」拆进了一个独立的 `LaunchState` 子 FSM（4.3 节精读），而 RTL 把它拆进了 `VX_cp_launch` 模块。

**预期结果**：你能画出仿真侧 `EngState` × `LaunchState` 两层 FSM 的状态转移图。

#### 4.2.5 小练习与答案

**练习 1**：`CMD_NOP` 和 `CMD_FENCE` 为什么不竞标任何资源？

> **参考答案**：它们不需要动 KMU/DMA/DCR/EVENT 任何一个。`classify` 对它们之外的已知 opcode 才映射资源；仿真侧在 `load_next_cmd` 里把 NOP/FENCE 标记为 `cur_is_no_resource_`（[cmd_processor.cpp:409-412](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L409-L412)），引擎直接跳到 Retire。注意 RTL 当前把 FENCE 当 NOP 处理，真正的栅栏语义是已知 gap（[command_processor.md:388-390](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L388-L390)）。

**练习 2**：如果两个 CPE 同周期都到了 `S_RETIRE`，seqnum 会不会丢？

> **参考答案**：不会。`S_RETIRE` 用 valid/ready 握手（`retire_evt` 持续抬起直到 `retire_ready_i`），`VX_cp_completion` 有 per-source 1 深锁存 + 共享排空 FIFO（[command_processor.md:247-250](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L247-L250)）。seqnum 只在真正离开 `S_RETIRE` 那拍自增，所以每个 CPE 呈现的号是稳定值，不会被覆盖。

---

### 4.3 KMU launch 包装器：start / busy / drain

#### 4.3.1 概念说明

`CMD_LAUNCH` 被分类到 `RES_KMU` 资源。这个资源对应的单元是 `VX_cp_launch`——它本身**不做网格遍历**（那是 KMU 内核描述符的事），它只是一个**启动握手包装器**：脉冲一下 `start`，然后握住 `busy` 信号直到内核跑完（drain）。它的存在让 CPE 的「等 done」语义变成「等这次启动彻底排空」。

为什么要「彻底排空」？因为当前实现里，一个队列**串行化自己的 launch**：`VX_cp_launch` 抓到授权后会一直占着 KMU 仲裁器的 bid，直到 `busy` 落下才释放，下一个 CPE 才能轮到。这保证了同队列前后两次 launch 不会重叠。

#### 4.3.2 核心流程

`VX_cp_launch` 的四态 FSM：

```
IDLE         --grant-->        刚拿到 KMU 仲裁器授权
PULSE_START  --(一拍)-->       抬 start 一拍，通知 Vortex 启动
WAIT_BUSY    --gpu_busy=1-->   等 Vortex 真的把 busy 拉高（内核开始执行）
WAIT_DRAIN   --gpu_busy=0-->   等 Vortex 把 busy 拉低（内核结束）→ 发 done，回 IDLE
```

关键时序细节：`start` 脉冲后的**下一拍** Vortex 才可能抬 busy，所以需要单独的 `WAIT_BUSY` 态等这个上升沿，而不是一脉冲完就直接等下降。

#### 4.3.3 源码精读

整个模块很短，是本讲最值得逐行读的 RTL：

- 模块端口与注释：[VX_cp_launch.sv:23-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv#L23-L31)——输入 `grant`（来自 KMU 仲裁器）、`gpu_busy`（来自 Vortex）；输出 `start`（脉冲给 Vortex）、`done`（回给引擎）。
- 四态 FSM 主体：[VX_cp_launch.sv:42-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv#L42-L64)。注意 `S_WAIT_BUSY` 等的是 `gpu_busy` 为真，`S_WAIT_DRAIN` 等的是 `gpu_busy` 为假：

```systemverilog
S_WAIT_BUSY: begin
  // Vortex's busy might rise the next cycle after `start` fires;
  // we wait for that rising edge.
  if (gpu_busy) state <= S_WAIT_DRAIN;
end
S_WAIT_DRAIN: begin
  if (!gpu_busy) state <= S_IDLE;
end
```

- 组合输出：[VX_cp_launch.sv:66-69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv#L66-L69)——`start = (state == S_PULSE_START)`，`done = (state == S_WAIT_DRAIN) && !gpu_busy`。

**仿真侧的孪生**是 `CommandProcessor::tick_launch`：[cmd_processor.cpp:370-387](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L370-L387)，状态枚举 `LaunchState { Idle, PulseStart, WaitBusy, WaitDrain }` 在 [cmd_processor.h:130](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.h#L130)。两者逐态对应：`PulseStart` 调 `hooks_.vortex_start()`、`WaitBusy` 等 `vortex_busy()` 为真、`WaitDrain` 等它为假。

> **`busy` 来自哪里？** 在 SimX 里，`hooks_.vortex_busy` 最终查的是 `Processor::any_running()`（u5-l2 讲过：所有 core 不运行且无在途 channel 包时为假）。也就是说，`done` 拉高 ⇔ 整个 GPU 空闲且所有访存包落地——这正是「drain」的严格含义，保证主机读回结果前所有写已落地。

#### 4.3.4 代码实践

**目标**：在仿真侧观察一次 launch 的四态时序，理解 `WAIT_BUSY` 那一拍的存在意义。

**操作步骤**：

1. 在一个已 configure 好的 build 目录跑 demo：`./ci/blackbox.sh --driver=simx --app=demo --debug=2`（`--debug` 生成运行时 trace，参见 u13-l2）。
2. 在生成的 trace 里定位 KMU/launch 相关的事件行（待本地验证具体 trace 字段名；可参考 `ci/trace_csv.py`）。
3. 对照 [cmd_processor.cpp:370-387](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L370-L387) 的四态，确认 `PulseStart` 与 `WaitBusy` 之间确实隔了至少一拍。

**需要观察的现象**：`start` 脉冲之后，`busy` 不是同拍拉高，而是下一拍（或更晚）才高；`done` 只在 `busy` 完全落下后出现。

**预期结果**：你能从 trace 数出 `PulseStart→WaitBusy→WaitDrain` 各占多少周期。若无法运行 trace，明确标注「待本地验证」，转而用源码阅读法：解释为何 `S_PULSE_START` 之后不能直接合并进 `S_WAIT_DRAIN`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `VX_cp_launch` 要占着 KMU 仲裁器的 bid 直到 drain，而不是脉冲完 `start` 就放手？

> **参考答案**：因为「放手」意味着下一个 CPE 可以立刻发起下一次 launch，两次 launch 会重叠。当前架构要求同队列 launch 串行（一次跑完再跑下一次），所以授权必须握到 `busy` 落下。注释里写得很明白（[VX_cp_launch.sv:9-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv#L9-L17)）：「KMU arbitration holds for the entire duration of a launch」。

**练习 2**：把 `S_WAIT_BUSY` 态删掉、直接从 `S_PULSE_START` 跳到 `S_WAIT_DRAIN`，会有什么风险？

> **参考答案**：`start` 脉冲当拍 `gpu_busy` 还没来得及拉高，`S_WAIT_DRAIN` 的退出条件是 `!gpu_busy`——若直接进入，会在 `busy` 还没起来的瞬间看到 `!gpu_busy` 为真，立刻误判「内核已结束」并发 `done`，于是内核实际还没开始就被宣告完成。`S_WAIT_BUSY` 就是用来锁死这个上升沿窗口的。

---

### 4.4 KMU 网格遍历器：从 DCR 到 CTA 派发

#### 4.4.1 概念说明

到这里，「点火」已经讲完。但 GPU 不会只跑一个 CTA——主机声明的 `grid_dim` 可能是成百上千个 CTA。把这些 CTA 实际**产生出来**并派发到各核的，是 **KMU 内核管理单元**。可以把 KMU 想成一个「展开器」：主机给它一组维度参数（写进它的 DCR），它每次被 `step()` 调一次就吐出下一个 CTA 的完整描述（`kmu_req_t`），直到网格走完。

KMU 的状态分两部分：
- **内核描述符**（`PC_`、`entry_`、`param_`、`block_dim_`、`grid_dim_`、`cluster_dim_` 等）：由主机在 launch 前通过 `CMD_DCR_WRITE` 写入，**跨 reset 保留**。
- **遍历游标**（`cta_id_`、`group_origin_`、`intra_offset_`）：由 `start()` 清零、由 `step()` 推进，reset 时清零。

每个核有一个 `CtaDispatcher`，它主动调 `kmu->step()` 拉下一个 CTA（KMU 是所有核共享的单例），再把该 CTA 按 `block_size/num_threads` 切成若干 warp——这条「CTA→warp」的切分在 u6-l1 已讲过，本讲只关心「grid→CTA」这一段。

#### 4.4.2 核心流程

**写描述符**（主机侧，launch 前）：主机用 `CMD_DCR_WRITE` 往一组 `VX_DCR_KMU_*` 寄存器里写值，KMU 的 `dcr_write` 按 addr 分流存进对应字段。

**启动**（`CMD_LAUNCH` 触发）：`start()` 检查 `block_size_/grid_dim_/cluster_dim_` 都大于 0，置 `running_=true`，清零游标。

**遍历**（每个核的 dispatcher 调 `step()`）：
```
block_idx[axis] = group_origin_[axis] + intra_offset_[axis]   // 实际块坐标
填 req 的所有字段（PC/entry/param/cta_id/block_idx/grid_dim/...）
按 (X, Y, Z) 顺序推进 intra_offset_：
  先走 cluster 内偏移（填满一个 cluster），
  cluster 满了再推进 group_origin_（跳到下一个 cluster 起点），
  三轴都走完 → running_=false。
```

这里的关键是**两层嵌套计数器**：`intra_offset_` 在一个 cluster 内走（`cluster_dim` 步），`group_origin_` 标记当前 cluster 的起点。这样 CTA 的发射顺序天然按 cluster 分组——同一个 cluster 的 CTA 占连续的 `cta_id`，这正是 DXA 多播（u9-l2）和跨 CTA 屏障（u4-l3）所需要的「共驻连续槽位」。

#### 4.4.3 源码精读

**`kmu_req_t`——一个 CTA 的完整描述**：[kmu.h:22-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h#L22-L35)。注意它携带：`PC`（镜像基址，每个 warp 都从这里开始执行 `__vx_cta_entry`）、`entry`（kernel 入口 PC）、`param`（参数指针，进 a0）、`cta_id`、`block_idx[3]`、`block_dim/grid_dim/cluster_dim[3]`，以及 `is_first_of_cluster`（标记一个 cluster 的第一个 CTA，供 dispatcher 预留连续槽位）。

**`dcr_write`——主机写描述符**：[kmu.cpp:47-71](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L47-L71)。注意 64 位字段（`PC_`、`entry_`、`param_`）都拆成 `*0/*1` 两个 32 位 DCR 写（低字、高字），这是 32 位 DCR 总线的必然结果：

```cpp
case VX_DCR_KMU_STARTUP_ADDR0: PC_ = (PC_ & ~uint64_t(0xFFFFFFFF)) | value; break;
case VX_DCR_KMU_STARTUP_ADDR1: PC_ = (PC_ &  uint64_t(0xFFFFFFFF)) | (uint64_t(value) << 32); break;
```

**DCR 路由**——`Processor::dcr_write` 把 KMU DCR 与其它 DCR 分开：[processor.cpp:286-299](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L286-L299)。落在 `[VX_DCR_KMU_STATE_BEGIN, VX_DCR_KMU_STATE_END)` 区间的只发给 KMU，不广播给核；其余 DCR 广播给所有 cluster。这正是 u3-l4 提到的「主机写 KMU DCR」的落点。

**`start()`——armed 检查 + 清零游标**：[kmu.cpp:73-86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L73-L86)。`running_` 仅当 `block_size_>0` 且三轴 `grid_dim_/cluster_dim_` 都 `>0` 时才置真。

**`step()`——网格遍历核心**：[kmu.cpp:88-166](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L88-L166)。三个关键点：

1. **实际块坐标 = cluster 起点 + cluster 内偏移**（[kmu.cpp:91-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L91-L96)）：`block_idx = group_origin_ + intra_offset_`。
2. **`is_first_of_cluster`**（[kmu.cpp:119-121](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L119-L121)）：当三轴 `intra_offset_` 全为 0 时为真。
3. **嵌套推进**（[kmu.cpp:125-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L125-L163)）：先 `++cta_id_`，再按 X→Y→Z 顺序推进 `intra_offset_`；cluster 走完后按 X→Y→Z 推进 `group_origin_`（每次加 `cluster_dim`）；三轴 `group_origin_` 都归零时 `running_=false`。

CTA 总数与 cluster 总数：

\[
\text{CTA 总数} = \text{grid\_dim}_x \times \text{grid\_dim}_y \times \text{grid\_dim}_z
\]

\[
\text{cluster 总数} = \prod_{a\in\{x,y,z\}} \left\lceil \frac{\text{grid\_dim}_a}{\text{cluster\_dim}_a} \right\rceil
\]

**dispatcher 消费 `step()`**：[cta_dispatcher.cpp:72-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L72-L119)。第 76 行 `kmu_->step(&pending_cta_)` 拉 CTA；第 100-109 行，当 `is_first_of_cluster` 时一次预留 `cluster_dim_x*cluster_dim_y*cluster_dim_z` 个连续槽位——这就是「cluster 成员占连续槽以支撑多播」的落地（u9-l2 已展开）。注意 `step()` 是所有核共享同一个 KMU 调用的，所以 `cta_id` 是全局单调递增的。

> **delegated launch（图形委托启动）**：当三轴 `grid_dim` 全为 0 时，`launch_delegated()` 为真（[kmu.h:53-55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h#L53-L55)），KMU 不产 CTA，而是把帧踢（frame_kick）转发给光栅引擎（[processor.cpp:251-259](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L251-L259)）。这是图形 draw 路径的特殊情况，计算 launch 不会走这里。

#### 4.4.4 代码实践

**目标**：用一组小维度手算 KMU 的 CTA 发射顺序，再用源码验证。

**操作步骤**：

1. 假设 `grid_dim=(2,2,1)`、`cluster_dim=(2,1,1)`。手算 `step()` 会产出几个 CTA、各自的 `cta_id`、`block_idx`、`is_first_of_cluster`。
2. 用公式算：CTA 总数 = \(2\times2\times1=4\)；cluster 总数 = \(\lceil2/2\rceil\times\lceil2/1\rceil\times1 = 1\times2\times1=2\)。
3. 对照 [kmu.cpp:125-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L125-L163) 的推进逻辑，逐 CTA 填下表。

**需要观察的现象**：CTA 的发射顺序应是「先把一个 cluster 的成员连续发完，再发下一个 cluster」。第 1 个 CTA `is_first_of_cluster=true`；cluster 切换处（第 3 个 CTA）再次 `is_first_of_cluster=true`。

**预期结果**（待本地用源码核对）：

| cta_id | group_origin | intra_offset | block_idx | is_first_of_cluster |
|---|---|---|---|---|
| 0 | (0,0,0) | (0,0,0) | (0,0,0) | true |
| 1 | (0,0,0) | (1,0,0) | (1,0,0) | false |
| 2 | (2,0,0) | (0,0,0) | (2,0,0) | true |
| 3 | (2,0,0) | (1,0,0) | (3,0,0) | false |

#### 4.4.5 小练习与答案

**练习 1**：为什么 `block_idx` 要用 `group_origin + intra_offset` 两层相加，而不是直接用一个游标？

> **参考答案**：因为 cluster 是网格的一个子块，`group_origin` 标记当前 cluster 在网格里的起点，`intra_offset` 标记 CTA 在 cluster 内的位置。两层相加既得到全局 `block_idx`，又天然让同一 cluster 的 CTA 连续发射（`intra_offset` 先走完），满足 DXA 多播与跨 CTA 屏障对「共驻连续槽位」的要求（u9-l2）。

**练习 2**：`PC_`（STARTUP_ADDR）和 `entry_`（KERNEL_ENTRY）有什么区别？分别被设备侧的什么机制消费？

> **参考答案**：`PC_` 是程序镜像基址，每个 warp 都从这里开始执行统一的 CTA 入口 `__vx_cta_entry`（u4-l1）；`entry_` 是本次要派发的 kernel 入口 PC。设备侧 prologue 从 `VX_CSR_CTA_ENTRY` 取 `entry_`、从 `VX_CSR_MSCRATCH` 取 `param_` 来派发到具体 kernel（u4-l1、u3-l4）。`startup_pc()` 还会被 fragment 工作分发器复用为注入 fragment warp 的启动 PC（[kmu.h:57-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h#L57-L60)）。

---

### 4.5 主机启动编码与 SimX/RTL 模型对应

#### 4.5.1 概念说明

最后把整条链路的首尾接上。主机运行时如何把一次 `vx_start`（或异步的 `vx_enqueue_launch`）变成环里的命令？答案在 `queue.cpp`：它用一组 `CMD_DCR_WRITE` 把 KMU 的所有描述符字段写满，最后跟一条 `CMD_LAUNCH` 点火。

更重要的是，CP 有**两套实现**：RTL CP（`hw/rtl/cp/`）是上 FPGA 的硬件；仿真 CP（`sim/common/cmd_processor.cpp`）是 simx/rtlsim/gem5 共用的 C++ 功能模型。两者必须保持行为一致（这是 u7-l4 model_parity 主线的一部分），但目前存在若干**已知且被记录的分歧**——仿真 CP 领先于 RTL CP（VM 感知 DMA、QMD launch、device-orchestrated draw 已在仿真侧实现，RTL 侧尚在路线图上）。

#### 4.5.2 核心流程

主机 launch 编码（`queue.cpp`）：

```
1. （可选）把 kernel-args blob 暂存进设备 scratch 槽，得到 args_addr
2. 一串 CMD_DCR_WRITE（经 cp_submit_dcr_write 进环）：
     STARTUP_ADDR0/1  = program_pc      （镜像基址）
     KERNEL_ENTRY0/1  = kernel_pc       （kernel 入口）
     STARTUP_ARG0/1   = args_addr       （参数指针）
     BLOCK_DIM_X/Y/Z, GRID_DIM_X/Y/Z, LMEM_SIZE, BLOCK_SIZE,
     WARP_STEP_X/Y/Z, CLUSTER_DIM_X/Y/Z
3. cp_submit_launch()  →  发 CMD_LAUNCH + 轮询 Q_SEQNUM 直到退休
```

仿真 CP 的 `tick_engine` 收到 `CMD_LAUNCH` 后（[cmd_processor.cpp:440-448](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L440-L448)）驱动 `LaunchState` 子 FSM，最终调 `hooks_.vortex_start()`——这个钩子在 simx 后端里触发 `Processor::start_kmu()` → `kmu_->start()`，于是 4.4 节的网格遍历开始。

#### 4.5.3 源码精读

**主机侧 KMU DCR 编程**：[queue.cpp:401-427](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L401-L427)。`WR` 宏（[queue.cpp:391-400](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L391-L400)）把每条 `CMD_DCR_WRITE` 提交进环。可以清楚看到 `program_pc`→`STARTUP_ADDR`、`kernel_pc`→`KERNEL_ENTRY`、`args_addr`→`STARTUP_ARG`、`eff_block`→`BLOCK_DIM`、`grid_in`→`GRID_DIM`、`lg_in`→`CLUSTER_DIM` 的映射——这些字段与 `kmu_req_t`（4.4 节）一一对应。最后 [queue.cpp:435](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L435) 的 `cp_submit_launch()` 发 `CMD_LAUNCH` 并轮询 `Q_SEQNUM`。

**CP 队列初始化**：[device.cpp:256-264](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L256-L264)——主机在 `cp_init` 时写 `Q_RING_BASE/HEAD_ADDR/CMPL_ADDR/RING_SIZE_LOG2/CONTROL` 和全局 `CP_REG_CTRL=1`，把环与完成槽的地址告诉 CP。

**仿真 CP 的 MMIO 面**：[cmd_processor.cpp:67-100](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L67-L100)（写）与 [:102-151](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L102-L151)（读）。注意 [:90-94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L90-L94) 的 doorbell 原子提交：写 `Q_TAIL_HI` 时把暂存的 `tail_lo_staging` 拼成完整 64 位 tail——与 RTL regfile 的 `Q_TAIL_HI` 提交语义一致（[command_processor.md:193-194](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L193-L194)）。

**两套实现的已知分歧**（设计文档 §8、§10）——这是本讲必须如实交代的「未完成项」：

| 分歧点 | 仿真 CP | RTL CP | 来源 |
|---|---|---|---|
| VM 感知 DMA（CP_SATP + 页表遍历） | 已实现（[cmd_processor.cpp:157-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L157-L201)） | 无 CP_SATP 寄存器，DMA 不翻译 | [command_processor.md:197-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L197-L201) |
| `CMD_LAUNCH_QMD`（带内联参数的原子 launch） | 已实现（[cmd_processor.cpp:308-322](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L308-L322)） | 未实现（仍走 ~18 条 DCR 写） | [command_processor.md:115-122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L115-L122) |
| `CMD_DRAW`（设备编排的 draw） | 已实现，`SUPPORTS_DRAW=1` | 未镜像，`SUPPORTS_DRAW=0`，运行时回退到逐命令环批 | [command_processor.md:223-228](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L223-L228) |
| DMA 字节精确性 | 字节精确 | 按 64B 向上取整（可能多写） | [command_processor.md:376-382](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L376-L382) |

> **能力寄存器统一**：尽管有上述分歧，设备/ISA 能力是通过 CP 的只读寄存器 `GPU_DEV_CAPS/GPU_ISA_CAPS`（仿真侧 [:27-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L27-L46)、`:126-129`）单一来源暴露的，运行时在所有后端上用同一份 `decode_caps()` 解码（[command_processor.md:205-221](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L205-L221)）。`SUPPORTS_DRAW` 这一位就是运行时决定「发一条 CMD_DRAW 还是发等价的多命令环批」的依据。

#### 4.5.4 代码实践

**目标**：把「主机写 DCR → CP 解包 → KMU 派发 CTA」这条完整控制流画出来，并标注每个维度的来源。

**操作步骤**：

1. 读 [queue.cpp:401-427](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L401-L427)，列出每个 `VX_DCR_KMU_*` 寄存器对应的主机变量（`program_pc`/`kernel_pc`/`args_addr`/`eff_block`/`grid_in`/`lg_in` 等）。
2. 读 [kmu.cpp:47-71](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L47-L71) 的 `dcr_write`，确认每个 DCR 落进 KMU 的哪个字段。
3. 读 [kmu.cpp:88-121](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L88-L121) 的 `step()`，确认这些字段如何填进 `kmu_req_t`。
4. 画一张控制流图：`vx_start` → `cp_submit_dcr_write ×N` → 环 → `tick_engine` 解码 → `dcr_write` 写 KMU → `cp_submit_launch` → `CMD_LAUNCH` → `tick_launch` 脉冲 start → `kmu_->start()` → 各核 `step()` 产 CTA。

**需要观察的现象**：`grid/block/cluster` 三个维度全部源自主机运行时（`queue.cpp` 的 `grid_in`、`eff_block`、`lg_in`），经过环命令、CP 解码、DCR 路由，最终落到 KMU 的 `grid_dim_/block_dim_/cluster_dim_` 字段，再由 `step()` 展开成每个 CTA 的 `block_idx`。

**预期结果**：你能指着图说出「`block_idx` 的 X 分量来自主机 `grid_in[0]` 与 `eff_block[0]` 共同决定的网格，`is_first_of_cluster` 来自主机 `lg_in`（cluster_dim）」。若某段无法在源码里定位，标注「待确认」。

#### 4.5.5 小练习与答案

**练习 1**：为什么运行时在所有后端上都用同一份 `decode_caps()` 读能力，而不是各后端各自硬编码？

> **参考答案**：因为能力是「单一真相来源」——通过 CP 的只读寄存器暴露（[command_processor.md:205-209](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L205-L209)）。这样 config-agnostic 的 `libvortex.so` 能在 open 时发现 VM 是否启用、是否支持 draw 等，而不必为每个后端编一份。这也呼应 u2-l3 的边界隔离纪律。

**练习 2**：`CMD_LAUNCH_QMD` 相比当前的「18 条 DCR 写 + CMD_LAUNCH」有什么本质优势？为什么它是实现多队列并发的前提？

> **参考答案**：当前 launch 要占环里 ~18 条命令的位置，且这 18 条 DCR 写与那条 launch 之间必须保序，导致一次 launch 在环里是一个长序列，多队列难以真正并发。QMD 把整组参数压成「一条命令 + 一个内存里的描述符」，CP 设备侧一次原子读取并回放（[cmd_processor.cpp:308-322](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L308-L322)），把 launch 在环里的足迹缩到 1 条命令，这是真正 per-queue 并发的前提（[command_processor.md:397-400](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md#L397-L400)）。

---

## 5. 综合实践

**任务**：跟踪一次真实的 `vx_start`，把「主机 → CP → KMU → CTA → warp」的完整控制流画成一张端到端时序图，并解释每个维度参数的来源与流向。

**建议步骤**：

1. 选一个最小例子：`tests/regression/demo`（向量加，u1-l4 跑过）或 `tests/regression/sgemm`。在已 configure 的 build 目录用 `./ci/blackbox.sh --driver=simx --app=demo --cores=2 --warps=4 --threads=4 --debug=2` 跑一次，生成 trace。
2. **主机段**：对照 [queue.cpp:401-435](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L401-L435)，列出这次 launch 写了哪些 KMU DCR、各自的值。注意 `--cores=2` 不会直接进 KMU DCR（核数是设备查询的），但会影响主机算出的 `grid_in`。
3. **CP 段**：对照 [cmd_processor.cpp:389-588](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L389-L588)，追踪这串命令如何被 `tick_engine` 逐条退休，以及最后的 `CMD_LAUNCH` 如何驱动 `tick_launch`（[:370-387](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L370-L387)）。
4. **KMU 段**：对照 [kmu.cpp:88-166](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L88-L166)，列出 `step()` 产出的 CTA 序列（参考 4.4.4 的表格式）。
5. **warp 段**：对照 [cta_dispatcher.cpp:72-139](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L72-L139)，说明每个 CTA 如何被切成 `block_size/num_threads` 个 warp（这一步的细节在 u6-l1）。

**交付物**：一张图 + 一张表。图含五个泳道（主机运行时 / CP 引擎 / CP launch / KMU / CTA dispatcher），箭头标出 `CMD_DCR_WRITE`、doorbell、`CMD_LAUNCH`、`vortex_start()`、`kmu->step()`、`activate_warp` 等事件。表里标注 `grid_dim/block_dim/cluster_dim` 各自从哪个主机变量流到 KMU 的哪个字段、再到 `kmu_req_t` 的哪个成员。

**若无法运行**：转为纯源码阅读型实践——不运行 blackbox，而是手工「执行」一遍 `step()`（像 4.4.4 那样填表），并把「待本地验证」明确标注在 trace 相关步骤上。

## 6. 本讲小结

- **CP 是单一控制平面**：主机所有工作（搬运/DCR/launch/栅栏/事件/缓存维护）都走命令环；环在主机内存，CP 用 `axi_host` 取、用 AXI-Lite 收 doorbell。顶层结构是「N 个引擎 + 4 个轮转仲裁器 + 5 个资源单元 + 双 AXI xbar」（[VX_cp_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv)）。
- **命令 = 4B 头 + opcode 载荷**，打包进 64B cache line；`CMD_LAUNCH` 只有 12B，因为内核参数已由前面的 `CMD_DCR_WRITE` 写进 KMU，它只负责点火（[VX_cp_pkg.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv)）。
- **引擎 FSM** `IDLE→DECODE→BID→WAIT_DONE→RETIRE` 把 opcode 分类到 KMU/DMA/DCR/EVENT 四资源；退休用 valid/ready 握手防丢号（[VX_cp_engine.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv)）。
- **`VX_cp_launch`** 是 start/busy/drain 的四态握手包装器，占着 KMU 仲裁器直到内核排空，保证同队列 launch 串行（[VX_cp_launch.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv)）。
- **KMU 是网格展开器**：用 `group_origin + intra_offset` 两层游标按 cluster 分组发射 CTA，`is_first_of_cluster` 让 cluster 成员占连续槽以支撑 DXA 多播（[kmu.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp)）。
- **RTL CP 与仿真 CP 主体一致，但仿真侧领先**：VM 感知 DMA、QMD launch、CMD_DRAW 已在仿真侧实现，RTL 侧是已知 gap；能力寄存器是单一来源，让运行时按位回退（[command_processor.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md) §8/§10）。

## 7. 下一步学习建议

- **向下接 u6-l1**：本讲止步于「CTA 请求产出」，CTA 如何被切成 warp、warp 如何拿到 PC/tmask 并进入流水线，是 u6-l1（scheduler/cta_dispatcher/barrier）的主题。建议接着读 `cta_dispatcher.cpp` 的 `activate_warp` 之后的部分。
- **横向接 u11-l1/u11-l2**：本讲的 CP DMA 是「搬运工」，而它搬的数据落进虚拟内存子系统（u11-l1）和原子操作通路（u11-l2）。特别地，CP DMA 的 VM 感知（仿真侧 `cp_translate`）与核内 MMU 是同一个 Sv32/Sv39 页表，理解 u11-l1 后再回头看 [cmd_processor.cpp:157-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L157-L201) 会更顺。
- **向上接 u10-l2/u9-l2**：`CMD_DRAW` 与 `launch_delegated` 把图形 draw 与 CP 绑在一起（u10-l2），`cluster_dim` 与 `is_first_of_cluster` 是 DXA 多播的基础（u9-l2）。读完这两讲再回看 KMU 的 cluster 遍历，能理解「为什么 CTA 必须按 cluster 连续发射」。
- **工程化接 u14-l1**：CP 在 FPGA 上是唯一 launch/DCR 通路，XRT/OPAE AFU 外壳如何把 AXI-Lite 与 `axi_host` 接到 PCIe，是 u14-l1 的内容。
- **继续阅读的源码**：`hw/rtl/cp/VX_cp_completion.sv`（完成回写的 per-source 锁存）、`VX_cp_dcr_proxy.sv`（DCR 代理 + cache flush 扫描）、`VX_cp_axil_regfile.sv`（寄存器图与 doorbell 原子提交），以及设计文档 [command_processor.md §10](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md) 列出的全部「已提案未实现」项——它们是参与 CP 后续开发的路标。
