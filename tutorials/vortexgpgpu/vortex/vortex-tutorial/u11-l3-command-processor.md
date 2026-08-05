# 命令处理器与 KMU

## 1. 本讲目标

本讲把 u3-l4（主机→设备启动流程）留下的「命令如何送达设备」与 u6-l1（warp 调度器、CTA 派发）入口处的「第一个 warp 从哪来」两端接起来，讲清横亘其间的**命令处理器（Command Processor，CP）**与**内核管理单元（Kernel Management Unit，KMU）**。

学完后你应当能够：

- 说清一次 `vx_start`/`vx_enqueue_launch` 引发的完整控制流：主机写 DCR → CP 解包 → KMU 派发 CTA；
- 掌握 CP 的 AXI-Lite 寄存器接口、命令格式与「引擎 FSM + 四资源仲裁」的执行模型；
- 理解 KMU 如何用 `group_origin + intra_offset` 的双层计数器遍历 grid、按 cluster_dim 分组产出 CTA；
- 区分 RTL CP（`VX_cp_core.sv`/`VX_cp_launch.sv`）与仿真 CP（`cmd_processor.cpp`/`kmu.cpp`）这两套 model parity 的对应实现。

---

## 2. 前置知识

- **CTA 与 grid/block**：Vortex 沿用 CUDA 术语，一个 kernel 启动一个 grid，grid 切成若干 block（Vortex 称 CTA，Cooperative Thread Array），每个 CTA 含若干 warp，每个 warp 含 `NUM_THREADS` 个 thread。这是 u6-l1 已建立的层次。
- **DCR（Device Control Register）**：设备控制寄存器，主机用来配置/查询设备的统一通道。KMU 有专属的 `VX_DCR_KMU_*` 地址段（如 `STARTUP_ADDR`、`KERNEL_ENTRY`、`GRID_DIM_X` 等），见 u3-l4。
- **AXI-Lite / AXI4**：ARM 总线协议。AXI-Lite 是轻量控制通路（一次一字），AXI4 是带突发（burst）的数据通路。CP 用 AXI-Lite 作控制面、AXI4 作数据面。
- **model parity**：SimX 与 RTL 必须功能与时序一致（u7-l4）。CP 也不例外——RTL CP 与仿真 CP 是同一架构的两种实现。
- **主机运行时的 stub 分发**（u3-l3）：`libvortex.so` 按 `$VORTEX_DRIVER` 加载后端，但 CP 之上的命令编码、队列、事件都是后端无关的 `common/` 代码。

> 一句话定位：**CP 是主机提交工作到 GPU 的唯一控制平面；KMU 是 CP 内部专门负责「把一次 kernel launch 展开成一串 CTA」的状态机。** CP 是「邮局」，KMU 是邮局里专管「分拣包裹（CTA）派给各邮递员（core）」的柜台。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [`hw/rtl/cp/VX_cp_core.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv) | RTL CP 顶层：寄存器堆 + N 个引擎 + 4 个仲裁器 + 5 个资源单元 + 双 AXI xbar |
| [`hw/rtl/cp/VX_cp_launch.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv) | RTL 中 KMU 资源的 start/busy 握手包装器 |
| [`hw/rtl/cp/VX_cp_engine.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv) | RTL 每队列引擎 FSM，opcode→资源分类 |
| [`hw/rtl/cp/VX_cp_pkg.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv) | opcode、`cmd_t`、`cpe_state_t`、资源枚举等共享定义 |
| [`sim/simx/kmu/kmu.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp) / [`kmu.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h) | SimX 的 KMU：持有内核描述符、遍历 grid 逐拍产 CTA |
| [`sim/common/cmd_processor.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp) | 仿真 CP 的功能 C++ 孪生体（simx/rtlsim/gem5 共用） |
| [`sw/runtime/common/queue.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp) | 主机侧 launch 编码：算 warp_step、发 ~18 条 CMD_DCR_WRITE + CMD_LAUNCH |
| [`sw/runtime/common/device.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp) | `cp_init`/`cp_submit_*`：把命令打包进环、敲门铃、轮询 seqnum |
| [`docs/designs/command_processor.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md) | CP 设计总文档（架构、命令格式、寄存器表、未竟事项） |

---

## 4. 核心概念与源码讲解

### 4.1 CP 总体架构：唯一控制平面

#### 4.1.1 概念说明

CP 是主机向 GPU 提交工作的**唯一控制平面**：内存搬运、DCR 编程、内核启动、栅栏、事件、缓存维护全都经它。在 FPGA 目标（XRT/OPAE）上，它更是**唯一**的 launch/DCR 路径——旧的 AP_CTRL 启动状态机已被移除（见 command_processor.md 开头）。

CP 的核心结构是「**N 个并行命令引擎**（每队列一个）喂给**四个轮转仲裁器**，仲裁器串行化对四个共享资源的访问」：

- **KMU launch**：内核启动；
- **DMA**：主机↔设备、设备↔设备拷贝；
- **DCR bus**：DCR 读写（含 cache flush 扫描）；
- **event unit**：事件计数器 signal/wait。

命令环（command ring）放在**主机内存**里；CP 用一个专用 AXI 主端口去取指（一次取一个 64B cache line），完成后把序号（seqnum）写回主机内存的完成槽。主机侧 therefore 只需「往环里写命令 → 敲门铃（写 tail） → 轮询 seqnum」。

默认配置 `NUM_QUEUES=1`、环大小 64 KiB（`RING_SIZE_LOG2=16`）、每个 64B 行最多 5 条命令。

#### 4.1.2 核心流程

CP 一拍的宏观流程（以一条 CMD_LAUNCH 为例）：

```
主机 runtime
  │ 1. 把命令打包进 64B CL，memcpy 进主机环
  │ 2. 写 Q_TAIL_LO/HI 敲门铃（HI 字原子提交）
  ▼
VX_cp_axil_regfile  ←─── AXI-Lite 从机（唯一控制口）
  │  更新该队列的 tail
  ▼
VX_cp_fetch（每队列一个环游走器）
  │  AR 一条 64B CL：ring_base + (head & mask)
  ▼
VX_cp_unpack（按偏移逐条解包命令）
  ▼
VX_cp_engine FSM：IDLE→DECODE→BID→WAIT_DONE→RETIRE
  │  classify(LAUNCH) = RES_KMU → 向 KMU 仲裁器出价
  ▼
VX_cp_arbiter(RES_KMU)（轮转，单队列时必胜）
  ▼
VX_cp_launch：脉冲 start，等 busy 起、等 busy 落 → done
  ▼
VX_cp_completion：写 8B seqnum 到 cmpl_addr → 主机轮询 Q_SEQNUM 命中
```

#### 4.1.3 源码精读

RTL 顶层 `VX_cp_core` 的端口与数据面注释一上来就点明了「双数据面」设计：

[hw/rtl/cp/VX_cp_core.sv:L89-L107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L89) — `axil_s`（控制面 AXI-Lite 从机）、`axi_host`（到主机内存的 AXI4 主机，命令环与每次上传/下载的主机端）、`axi_dev`（到设备内存的 AXI4 主机）、`gpu_if`（面向 Vortex 的 DCR + start/busy 握手）。注释说明 DMA 引擎跨在两个 xbar 上，按 opcode 选择读源/写目的端口。

顶层把零件拼成「regfile + N×(fetch+engine) + 4 仲裁器 + 5 资源单元 + 双 xbar」：

[hw/rtl/cp/VX_cp_core.sv:L184-L229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L184) — generate 循环为每个队列实例化一个 `VX_cp_fetch`（带内嵌 unpack）和一个 `VX_cp_engine`。注意 L209-215：四个 `*_done` 脉冲是**广播**给所有 CPE 的，只有正处在 `S_WAIT_DONE` 且持有授权的那个 CPE 才会接收——这是单资源一次只服务一个命令的硬件体现。

四个轮转仲裁器各管一个资源：

[hw/rtl/cp/VX_cp_core.sv:L276-L291](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L276) — `u_arb_kmu/dma/dcr/event`，每个 `VX_cp_arbiter #(.N(NUM_QUEUES))`。设计文档指出 `bid_priority` 输入目前**未使用**（command_processor.md §4），即纯轮转。

`cp_busy` 聚合给主机一个总状态位：

[hw/rtl/cp/VX_cp_core.sv:L482-L490](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_core.sv#L482) — 任意 CPE 有命令在途，或任意资源被授权，`cp_busy` 即拉高。

#### 4.1.4 代码实践

**实践目标**：在 RTL 顶层建立「数据面/控制面/资源面」三视图。

**操作步骤**：

1. 打开 `VX_cp_core.sv`，定位三个 AXI 接口（`axil_s`/`axi_host`/`axi_dev`）与一个 GPU 接口（`gpu_if`）。
2. 找到 L388-478 的两个 xbar（host xbar、dev xbar），数清各自的源（host xbar = fetch[N] + completion + DMA(host)；dev xbar = DMA(dev) + event）。
3. 在文件里数 `VX_cp_*` 子模块实例化次数，确认是「1 regfile + N×(fetch+engine) + 4 仲裁器 + launch/dcr/dma/event/completion 五资源单元」。

**需要观察的现象**：fetch 是每个 CPE 里**唯一**的 AXI 用户（L179-181 注释），其余资源（DMA/event/completion）各自挂在自己的 xbar 源槽上。

**预期结果**：你能画出一张「左侧 AXI-Lite 控制口 → 中间引擎阵列 → 右侧四资源 → 上下两个 AXI 数据主口」的方框图。

#### 4.1.5 小练习与答案

**练习 1**：为什么命令环放在主机内存而不是设备内存？
**答**：主机 runtime 可以直接通过环的 host 指针 `memcpy` 写命令，无需逐条 DMA（`cp_init` 注释 device.cpp:233）；CP 只需一个 AXI 主端口取指、一个写完成槽，硬件路径最简。这是对早期「设备内存环 + 逐命令 DMA」方案的取代（command_processor.md §11）。

**练习 2**：四个仲裁器为什么要分开，而不是一个大仲裁器？
**答**：因为四个资源（KMU/DMA/DCR/EVT）相互独立，一个队列等 KMU 时不该阻塞另一个队列发 DMA。分资源仲裁让不同资源可并行被授权（command_processor.md §1）。

---

### 4.2 命令格式与 AXI-Lite 寄存器接口

#### 4.2.1 概念说明

主机与 CP 之间有两层契约：

1. **控制面**（AXI-Lite 寄存器）：主机写「环基址/大小/head/cmpl 地址、tail 门铃、使能位」，读「状态、能力寄存器、seqnum、DCR 读回值」。
2. **数据面**（命令环）：每条命令是「4 字节头 + opcode 特定载荷」，打包进 64B cache line（最多 5 条/行，零头作终止哨兵）。

命令头是 `{reserved[15:0], flags[7:0], opcode[7:0]}`。关键 opcode 与在线尺寸：`CMD_DCR_WRITE`(0x04, 20B)、`CMD_LAUNCH`(0x06, 12B)、`CMD_MEM_WRITE`(0x01, 28B)、`CMD_FENCE`(0x07, 8B)、`CMD_CACHE_FLUSH`(0x0A, 12B) 等。

#### 4.2.2 核心流程

寄存器地图（AXI-Lite）：

- **全局** `0x000–0x024`：`CP_CTRL`、`CP_STATUS`、只读能力寄存器 `GPU_DEV_CAPS`/`GPU_ISA_CAPS`。
- **每队列块** 在 `0x100 + qid*0x40`：`Q_RING_BASE`、`Q_HEAD_ADDR`、`Q_CMPL_ADDR`、`Q_RING_SIZE_LOG2`、`Q_CONTROL`、门铃 `Q_TAIL_LO/HI`、`Q_SEQNUM`、`Q_LAST_DCR_RSP`。

门铃的原子语义：写 `Q_TAIL_LO` 只是暂存，**写 `Q_TAIL_HI` 才原子提交**整个 64 位 tail——这避免了主机更新 tail 时 CP 看到半新半旧的撕裂值。

命令解包：`VX_cp_unpack` 按偏移逐条解码，靠 `cmd_size_bytes(opcode)` 跳过已消费命令，遇到零头（opcode=0 && flags=0）停止本行。

#### 4.2.3 源码精读

命令头与解码记录的定义：

[hw/rtl/cp/VX_cp_pkg.sv:L84-L103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L84) — `cmd_header_t`（4B）与 `cmd_t`（头 + arg0/arg1/arg2 各 8B + 可选 8B profile_slot）。最坏载荷 28B（`CMD_MEM_*`/`CMD_EVENT_WAIT`/`CMD_DCR_READ`）。

每队列持久状态（主机通过 AXI-Lite 写入这些字段）：

[hw/rtl/cp/VX_cp_pkg.sv:L130-L141](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L130) — `cpe_state_t` 含 `ring_base`、`ring_size_mask`、`head_addr`、`cmpl_addr`、`tail`、`head`、`seqnum`、`prio`、`enabled`、`profile_en`。一个实例住进每个 `VX_cp_engine`。

仿真 CP 的门铃原子提交逻辑（与 RTL regfile 对应）：

[sim/common/cmd_processor.cpp:L79-L98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L79) — 队列 0 偏移 `0x20` 写 `tail_lo_staging`，偏移 `0x24` 写 HI 字时执行 `tail = (HI<<32) | staging` 完成原子提交。`Q_SEQNUM`/`Q_ERROR` 是只读。

主机侧 `cp_init` 把这些寄存器编程好：

[sw/runtime/common/device.cpp:L232-L265](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L232) — 分配环/head/cmpl 三块 CP 可见主机内存，写它们的地址、`Q_RING_SIZE_LOG2`、`Q_CONTROL=0x1`、`CP_REG_CTRL=0x1`。

#### 4.2.4 代码实践

**实践目标**：手工对齐「主机打包 → 在线布局 → CP 解码」三处对命令格式的描述。

**操作步骤**：

1. 读 `cp_submit_dcr_write`（device.cpp:489-499）：`p32[0]=opcode(0x04)`、`p32[1]=addr`（arg0）、`p32[3]=value`（arg1，注意 p32[2] 是 arg0 的高 32 位，故 value 落在字节 16-19）。
2. 对照 `cmd_processor.cpp` 的 `decode_cmd_bytes`（L223-257），看它如何从 `off+0` 读 opcode、`off+4` 读 arg0、`off+12` 读 arg1、`off+20` 读 arg2，并按 opcode 返回尺寸（`OP_DCR_WRITE` 返回 20）。
3. 验证 `CMD_LAUNCH` 三处一致：device.cpp:503-507（opcode 0x06，仅头 12B）、`decode_cmd_bytes` 的 `OP_LAUNCH` 返回 12、`VX_cp_pkg.sv` 的尺寸表。

**需要观察的现象**：CL 剩余字节填零，零头就是 unpacker 的终止哨兵（cmd_processor.cpp:268）。

**预期结果**：你能默写出 CMD_DCR_WRITE 与 CMD_LAUNCH 的字节布局。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Q_TAIL_HI` 写入才提交，而不是 `Q_TAIL_LO`？
**答**：多字节值跨多次 MMIO 写传送，必须有一个「最终提交点」。把 HI 字定为提交点，保证 CP 看到的 tail 是完整的 64 位值，不会在小端序下先看到低 32 位更新而误以为有新命令（cmd_processor.cpp:90-94）。

**练习 2**：能力寄存器 `GPU_DEV_CAPS`/`GPU_ISA_CAPS` 为什么放在 CP 里？
**答**：让每个后端通过**同一份** CP regfile 暴露设备/ISA 能力，运行时在 `cp_init` 后用统一的 `decode_caps()` 读取（command_processor.md §6），消除了各 AFU shell 里重复的能力块。

---

### 4.3 引擎 FSM 与四资源仲裁

#### 4.3.1 概念说明

每个队列的命令由一个 `VX_cp_engine` 驱动，它跑一个五态 FSM，并把每条命令**分类**到四个资源之一，向对应仲裁器出价（bid）。这套机制把「取指解包」与「执行资源」解耦：引擎只管按序把命令递给资源，资源干完回一个 done 脉冲，引擎再退休。

分类规则（`classify`）：

- `CMD_LAUNCH` → `RES_KMU`
- `CMD_DCR_WRITE/READ`、`CMD_CACHE_FLUSH` → `RES_DCR`
- `CMD_MEM_WRITE/READ/COPY` → `RES_DMA`
- `CMD_EVENT_SIGNAL/WAIT` → `RES_EVT`
- `CMD_NOP`/`CMD_FENCE` → 跳过仲裁，直接退休

#### 4.3.2 核心流程

引擎 FSM（RTL `VX_cp_engine` 与仿真 `tick_engine` 逐态对应）：

```
IDLE      ──cmd_in_valid──▶ DECODE
DECODE    ──classify──▶ skip? RETIRE : BID
BID       ──对应资源 grant──▶ WAIT_DONE
WAIT_DONE ──对应资源 done 脉冲──▶ RETIRE
RETIRE    ──retire_ready 握手──▶ seqnum+1, 回 IDLE
```

退休要经 `VX_cp_completion` 的 valid/ready 握手，保证同一拍多个引擎同时退休时不丢 seqnum。

#### 4.3.3 源码精读

资源枚举：

[hw/rtl/cp/VX_cp_pkg.sv:L149-L154](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_pkg.sv#L149) — `RES_KMU/RES_DMA/RES_DCR/RES_EVT`。

分类函数与 FSM：

[hw/rtl/cp/VX_cp_engine.sv:L97-L114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L97) — `classify` 把 opcode 映射到资源，`skip` 标记 NOP/FENCE。

[hw/rtl/cp/VX_cp_engine.sv:L153-L172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_engine.sv#L153) — `S_BID` 等本资源授权，`S_WAIT_DONE` 等对应资源 done 脉冲（`kmu_done_i`/`dma_done_i`/`dcr_done_i`/`event_done_i`）。

仿真 CP 的等价 FSM：

[sim/common/cmd_processor.cpp:L422-L556](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L422) — `EngState::Idle/Decode/Bid/WaitDone/Retire`。单队列时总赢仲裁，故 `Bid` 直接处理。`OP_LAUNCH`/`OP_LAUNCH_QMD` 进入 `launch_state_` 子状态机并转 `WaitDone`；`OP_DCR_WRITE` 立即调 `vortex_dcr_write` hook 后退休。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 CMD_DCR_WRITE 走完引擎 FSM。

**操作步骤**：

1. 在 `VX_cp_engine.sv` L136-185 模拟：`S_IDLE` 收到 `cmd_in` → `S_DECODE`。
2. `classify(CMD_DCR_WRITE)` 返回 `RES_DCR`，`skip=0` → `S_BID`。
3. L203-205：`bid_dcr.valid=1`，等 `bid_dcr.grant` → `S_WAIT_DONE`。
4. `VX_cp_dcr_proxy` 执行写后发 `dcr_done` → L168 `S_RETIRE` → `retire_ready` 时 `seqnum+1`。

**需要观察的现象**：`S_RETIRE` 会**停留**直到 `retire_ready_i`（L177），保证 completion 模块收到。

**预期结果**：理解「引擎只出价与等 done，不亲自执行」的解耦。

#### 4.3.5 小练习与答案

**练习 1**：done 脉冲是广播的，为什么只有授权的 CPE 会响应？
**答**：只有赢得仲裁、处在 `S_WAIT_DONE` 的那个 CPE 才在 case 语句里匹配对应资源的 done；非授权 CPE 此刻不在 `S_WAIT_DONE`，忽略该脉冲（VX_cp_engine.sv:116-120 注释）。

**练习 2**：`CMD_FENCE` 当前如何处理？有何隐患？
**答**：当前被 `classify` 当 NOP 直接退休（command_processor.md §10 第 3 条），并未真正实施 `FENCE_DMA_BIT`/`FENCE_GPU_BIT` 的排序语义——这是一个已知的待修复项。

---

### 4.4 VX_cp_launch：KMU 启动握手

#### 4.4.1 概念说明

KMU 资源单元的执行件是 `VX_cp_launch`——一个极小的四态状态机，专门处理「启动一次内核」的 start/busy 握手。它的核心职责是：**在内核整个运行期间持续占用 KMU 仲裁器**，直到内核跑完（drain），这保证一个队列串行化自己的 launch。

它不负责展开 CTA——那是 KMU 自己的事。它只是「按一下启动按钮，然后等 GPU 说忙完了」。

#### 4.4.2 核心流程

```
IDLE         ──grant──▶ 获得仲裁
PULSE_START  ──▶ 给 gpu_if.start 一个一周期脉冲
WAIT_BUSY    ──gpu_busy↑──▶ 等 GPU 真正启动
WAIT_DRAIN   ──gpu_busy↓──▶ 内核跑完，发 done，回 IDLE（释放仲裁）
```

`grant` 是 KMU 仲裁器各 CPE 授权的 OR；`done` 释放出价，让下一个 CTE 轮到。

#### 4.4.3 源码精读

[hw/rtl/cp/VX_cp_launch.sv:L33-L69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cp/VX_cp_launch.sv#L33) — 四态枚举 `S_IDLE/S_PULSE_START/S_WAIT_BUSY/S_WAIT_DRAIN`。L50-51 `PULSE_START` 仅一拍；L53-57 等 `gpu_busy` 上升沿（注释指出 start 后 busy 可能下一拍才起）；L58-59 等 `busy` 落下。L66-69 组合输出 `start = (state==PULSE_START)`、`done = (state==WAIT_DRAIN) && !gpu_busy`。

仿真 CP 有完全对应的子状态机：

[sim/common/cmd_processor.cpp:L370-L387](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L370) — `LaunchState::PulseStart`（调 `hooks_.vortex_start`）/`WaitBusy`（等 `vortex_busy()` 为真）/`WaitDrain`（等其为假）。

#### 4.4.4 代码实践

**实践目标**：理解 start/busy 的异步握手时序。

**操作步骤**：

1. 读 `VX_cp_launch.sv` 的注释 L9-21，注意「CPE 在所有四态里都保持出价」。
2. 思考：为什么需要 `WAIT_BUSY` 这一步，而不是 `PULSE_START` 后直接进 `WAIT_DRAIN`？

**需要观察的现象**：`start` 是单周期脉冲，但 `busy` 可能滞后一拍才升高，所以中间必须插一个 `WAIT_BUSY` 等上升沿，否则 `WAIT_DRAIN` 会因为 `!gpu_busy` 立刻误判完成（L54-56 注释）。

**预期结果**：说清「start 脉冲 → busy 迟一拍升高 → 跑完 busy 落下 → done」的四拍关系。

**待本地验证**：在 SimX trace 里观察一次 launch 前后 `gpu_if.start`/`gpu_if.busy` 的逐拍跳变，确认上述时序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 launch 要「持有仲裁器直到 drain」，而不能启动后立刻释放？
**答**：若启动后立刻释放，同一队列的下一条命令（可能是下一次 launch 或相关 DCR 写）会抢在本次内核跑完前执行，破坏顺序语义。持有到 drain 等价于「launch 是一个隐式栅栏」（command_processor.md §7）。

**练习 2**：`VX_cp_launch` 与 KMU 的 CTA 展开是什么关系？
**答**：`VX_cp_launch` 只发 start 信号并等整个内核 drain；CTA 的具体展开由 KMU（RTL 侧的 Vortex KMU / SimX 的 `Kmu`）在收到 start 后自行完成。本模块是「按钮」，KMU 是「分拣柜台」。

---

### 4.5 KMU：内核描述符与 CTA 网格遍历

#### 4.5.1 概念说明

KMU（Kernel Management Unit）持有一份「在飞内核描述符」（kernel descriptor），并根据 grid/block/cluster 维度逐拍产出 CTA。在 SimX 里它就是 `Kmu` 类（`sim/simx/kmu/kmu.cpp`）；在 RTL 里它住进 Vortex 顶层（由 `gpu_if.start` 触发）。两边 model parity。

内核描述符由一组 `VX_DCR_KMU_*` DCR 装载：

- `STARTUP_ADDR`（PC）：程序镜像基址，每个 warp 从 `__vx_cta_entry` 开始（即 u4-l1 的统一 prologue）；
- `KERNEL_ENTRY`：实际 kernel 入口 PC；
- `STARTUP_ARG`（param）：参数指针，送入设备侧 `a0`/`VX_CSR_MSCRATCH`；
- `BLOCK_DIM_X/Y/Z`、`GRID_DIM_X/Y/Z`、`CLUSTER_DIM_X/Y/Z`：网格几何；
- `BLOCK_SIZE`（CTA 内线程总数）、`LMEM_SIZE`（每 CTA 本地内存大小）、`WARP_STEP_X/Y/Z`（warp 内线程到 threadIdx 坐标的步长）。

KMU 用一个**双层嵌套计数器**遍历 grid：`block_idx = group_origin + intra_offset`。`intra_offset` 在一个 cluster 内走（步长 1），走完一个 cluster 后 `group_origin` 按步长 `cluster_dim` 推进。这种「先填满 cluster，再跳到下一个 cluster」的遍历顺序，是 u9-l2 DXA 多播要求 cluster 成员**连续驻留**的数学基础。

#### 4.5.2 核心流程

CTA 总数为：

\[
\text{num\_ctas} = \text{grid\_dim}_x \cdot \text{grid\_dim}_y \cdot \text{grid\_dim}_z
\]

KMU 的遍历等价于两层嵌套循环（按 X、Y、Z 内层到外层）：

```
for oz in 步长 cluster_dim_z 遍历 grid_z:        # group_origin 推进
  for oy ... grid_y:
    for ox ... grid_x:
      for iz in [0, cluster_dim_z):              # intra_offset 填 cluster
        for iy in [0, cluster_dim_y):
          for ix in [0, cluster_dim_x):
            block_idx = (group_origin + intra_offset)
            emit CTA(block_idx, is_first_of_cluster=(intra_offset==0))
```

每拍 `step()` 填一个 `kmu_req_t` 并自增计数器，grid 耗尽时 `running_=false`。`is_first_of_cluster` 在 `intra_offset` 全零时拉高，告诉下游 CtaDispatcher「为这个 cluster 预留 K 个连续 LMEM 槽位」。

#### 4.5.3 源码精读

`kmu_req_t` 载荷——一个 CTA 的全部上下文：

[sim/simx/kmu/kmu.h:L22-L35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h#L22) — 含 `PC/entry/param`（每 CTA 都带，供 prologue 取入口与参数）、`cta_id`、`block_idx[3]`、`block_dim[3]`、`grid_dim[3]`、`lmem_size`、`block_size`、`warp_step[3]`、`cluster_dim[3]`、`is_first_of_cluster`。这正是设备侧 `__vx_cta_entry` 经 CSR 取到的全部信息。

DCR 装载描述符：

[sim/simx/kmu/kmu.cpp:L47-L71](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L47) — 每个 `VX_DCR_KMU_*` case 把 value 装进对应字段；64 位字段（PC/entry/param）用 LO/HI 两次写拼装。注意 `on_reset()`（L36-45）刻意**不**清描述符，只清运行进度——因为描述符在 `start()` 之前由 DCR 写好，须跨 reset 存活。

启动判定：

[sim/simx/kmu/kmu.cpp:L73-L86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L73) — `start()` 要求 `block_size>0` 且 grid、cluster 各维都 >0 才置 `running_=true`，并复位进度计数器。`launch_delegated()`（kmu.h:53-55）则相反：grid 全零时这是图形 draw launch，KMU 不产 CTA，由处理器转交 raster 引擎。

逐拍遍历——核心嵌套计数：

[sim/simx/kmu/kmu.cpp:L88-L166](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L88) — `step()` 先把当前 `block_idx = group_origin + intra_offset` 装进 req（L91-121），再推进计数器。L126-128 先自增 `intra_offset[0]`，x 包卷则进 y、y 包卷则进 z；z 也包卷（L136-140）说明一个 cluster 走完，于是 L142-160 按 `cluster_dim` 步长推进 `group_origin`（X→Y→Z），全部 grid 走完时 `running_=false`（L151）。`is_first_of_cluster` 在 L119-121 计算。

KMU DCR 的路由——只进 KMU，不广播给核：

[sim/simx/processor.cpp:L286-L299](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L286) — `addr` 落在 `[VX_DCR_KMU_STATE_BEGIN, VX_DCR_KMU_STATE_END)` 时只调 `kmu_->dcr_write()`，不转发 cluster 树。这正是 CP 的 `CMD_DCR_WRITE` 经 `vortex_dcr_write` hook 抵达 KMU 的落点。

下游消费 CTA：

[sim/simx/cta_dispatcher.cpp:L72-L104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L72) — `CtaDispatcher::step` 在无待处理 CTA 时调 `kmu_->step(&pending_cta_)`（L76），按 `lmem_size` 步长做固定槽分配；`is_first_of_cluster` 为真时（L101）预留 `cluster_dim` 连乘个连续槽位（L102-113），随后成员落进预留槽。这里把 KMU 的几何输出转成 u6-l1 调度器能消费的 warp。

#### 4.5.4 代码实践

**实践目标**：用一组小维度手算 KMU 的遍历顺序。

**操作步骤**：

设 `grid_dim = (2,1,1)`、`cluster_dim = (2,1,1)`、`block_dim = (4,1,1)`、`NUM_THREADS=2`（故每 CTA 2 个 warp）。

1. 推演 `step()` 序列：
   - 第 1 拍：`group_origin=(0,0,0)`、`intra_offset=(0,0,0)` → `block_idx=(0,0,0)`，`is_first_of_cluster=true`。
   - 第 2 拍：`intra_offset=(1,0,0)` → `block_idx=(1,0,0)`，`is_first_of_cluster=false`；intra x 包卷（1+1==cluster_dim[0]=2）→ cluster 完，`group_origin` 推进到 (2,0,0)？但 `grid_dim[0]=2`，故 `ox=0+2==2==grid_dim[0]` → 全部走完。
2. 验证：共 2 个 CTA，`cta_id` 从 0 到 1，第 2 个 CTA 后 `running_=false`。

**需要观察的现象**：两个 CTA 同属一个 cluster（因 cluster_dim[0]=2 且 grid_dim[0]=2），第 1 个 CTA 带 `is_first_of_cluster=true`，CtaDispatcher 会为它预留 2 个连续 LMEM 槽。

**预期结果**：你画出的 `block_idx` 序列是 `(0,0,0) → (1,0,0)`，与「先填 cluster 再跳」一致。

**待本地验证**：在 SimX 加一行日志打印 `kmu_->step` 每拍的 `block_idx` 与 `is_first_of_cluster`（见综合实践），用 `--cores=1 --warps=2 --threads=2` 跑一个 grid=(2,1,1) 的小 kernel 对照。

#### 4.5.5 小练习与答案

**练习 1**：`WARP_STEP_X/Y/Z` 是怎么算出来的？它影响什么？
**答**：主机侧 queue.cpp 由 `num_threads(tpw)` 与 `block_dim` 算：`ws_x = tpw % bx`、`ws_y = (tpw/bx) % by`、`ws_z = (tpw/(bx*by)) % bz`（queue.cpp:354-360）。它定义 warp 内一个 lane（线程）映射到哪个 `threadIdx` 坐标，是 CTA→warp 切片（u6-l1）的坐标步长。

**练习 2**：为什么 grid 全零被当成「delegated draw launch」？
**答**：图形 draw 不走 CTA 模型，而是由 raster 引擎自驱动（u10-l1）。KMU 用 grid 全零作哨兵，`launch_delegated()` 返回真，处理器便把 frame kick 转发各 cluster 的 raster_core 而不产 CTA（processor.cpp:251-259、kmu.h:53-55）。

---

### 4.6 主机→CP→KMU→CTA 的完整启动路径

#### 4.6.1 概念说明

把前五个模块串成一条端到端的路径，并看清 RTL CP 与仿真 CP 在 model parity 下的对应关系。一次 `vx_start`/`vx_enqueue_launch` 实际引发的不是「一条命令」，而是「约 18 条 `CMD_DCR_WRITE`（编程 KMU 描述符）+ 1 条 `CMD_LAUNCH`（触发）」的命令批。

#### 4.6.2 核心流程

完整路径（标注 grid/block/cluster 维度的来源）：

```
① 主机 vx_enqueue_launch（queue.cpp）
   │  查询 NUM_THREADS/NUM_WARPS → 算 warp_step
   │  暂存参数 blob 到设备 scratch
   │  WR() 宏发 ~18 条 CMD_DCR_WRITE：
   │     STARTUP_ADDR0/1  ← program_pc（.vxbin 基址，来自 Module 镜像）
   │     KERNEL_ENTRY0/1   ← kernel_pc（来自 Kernel 名字→PC，u3-l4）
   │     STARTUP_ARG0/1    ← args_addr（参数 blob 地址）
   │     BLOCK_DIM_X/Y/Z   ← eff_block（主机传入的 block_in）
   │     GRID_DIM_X/Y/Z    ← grid_in（主机传入）
   │     LMEM_SIZE/BLOCK_SIZE/WARP_STEP_X/Y/Z
   │     CLUSTER_DIM_X/Y/Z ← lg_in（主机传入的 cluster 维度）
   │  cp_submit_launch()：1 条 CMD_LAUNCH + CMD_CACHE_FLUSH
   ▼
② device.cpp：cp_submit_dcr_write / cp_submit_launch
   │  打包 CL，memcpy 进环，写 Q_TAIL_LO/HI 门铃，轮询 Q_SEQNUM
   ▼
③ CP（RTL VX_cp_core / 仿真 cmd_processor.cpp）
   │  fetch CL → unpack → engine FSM
   │  CMD_DCR_WRITE → RES_DCR → dcr_proxy → DCR 总线
   │  CMD_LAUNCH   → RES_KMU → VX_cp_launch → 脉冲 start
   ▼
④ KMU（仿真 Kmu / RTL Vortex KMU）
   │  dcr_write() 已装好描述符；start() 武装
   │  step() 按 grid×cluster 遍历产 CTA
   ▼
⑤ CtaDispatcher → Scheduler（u6-l1）
   │  每 CTA 切成 ceil(block_size/NUM_THREADS) 个 warp
   │  分 LMEM 槽、装 warp 状态、派发
   ▼
⑥ 内核 drain（busy 落）→ launch done → engine 退休 → seqnum 写回 → 主机轮询命中
```

维度来源小结：

| 维度 | 来源 |
|---|---|
| `STARTUP_ADDR` (PC) | `.vxbin` 镜像基址 `program_pc`（module.cpp 加载） |
| `KERNEL_ENTRY` | kernel 名字经 `vx_module_get_kernel` 解析出的 PC（u3-l4） |
| `STARTUP_ARG` | 主机暂存的参数 blob 设备地址 |
| `BLOCK_DIM` | 主机 `vx_enqueue_launch` 的 `block_in` 参数 |
| `GRID_DIM` | 主机的 `grid_in` 参数 |
| `CLUSTER_DIM` | 主机的 `lg_in`（cluster group）参数 |
| `WARP_STEP` | 运行时按 `NUM_THREADS`/`NUM_WARPS` 与 block_dim 推导 |
| `BLOCK_SIZE` | block_dim 各维连乘 |
| `LMEM_SIZE` | kernel 编译期上报的本地内存需求 |

#### 4.6.3 源码精读

主机侧的 launch 编码——WR 宏与全部 DCR：

[sw/runtime/common/queue.cpp:L391-L428](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L391) — `WR(addr, val)` 展开为 `cp_submit_dcr_write`；依次写 PC/entry/arg/block/grid/lmem/block_size/warp_step/cluster_dim，末尾 `cp_submit_launch()`。

CMD_LAUNCH 的打包与尾部一致性：

[sw/runtime/common/device.cpp:L502-L520](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L502) — 仅写 opcode 0x06，随后强制跟一条 `CMD_CACHE_FLUSH`（ACQUIRE_MEM 模型，保证主机读到一致的内核结果），再排空 COUT 环。

仿真后端把 CP 的 start hook 接到处理器的 `run()`：

[sw/runtime/simx/vortex.cpp:L144-L160](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/simx/vortex.cpp#L144) — `vortex_dcr_write` 调 `processor_.dcr_write`（→ KMU 装描述符）；`vortex_start` 用 `std::async` 跑 `processor_.run()`（→ `reset()` + `kmu_->start()` + tick 循环）；`vortex_busy` 查 future 是否就绪。这就是 CP launch 到 KMU start 的桥。

`ProcessorImpl::run()` 武装 KMU 并跑到 drain：

[sim/simx/processor.cpp:L223-L249](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L223) — `reset()` → `kmu_->start()` → `forward_delegated_launch()` → 循环 `tick()` 直到「所有 cluster 不 running 且无在途 channel 包」。结束条件里的 `inflight_count()==0` 保证所有写已落地，主机读回的结果一致（与 u5-l2 一致）。

#### 4.6.4 代码实践：跟踪一次真实 launch

**实践目标**：在一个真实 demo 上画出从主机到 CTA 的完整控制流。

**操作步骤**：

1. 用 blackbox（u1-l4）跑一个小 kernel：
   ```
   ./ci/blackbox.sh --driver=simx --app=demo --cores=1 --warps=2 --threads=4
   ```
2. 在 `sim/simx/kmu/kmu.cpp` 的 `step()` 入口临时加一行打印（**示例代码，仅供阅读型实践，不要提交**）：
   ```cpp
   // 示例代码：在 step() 开头打印
   printf("[KMU] cta_id=%u block_idx=(%u,%u,%u) first_of_cluster=%d\n",
          cta_id_, block_idx[0], block_idx[1], block_idx[2],
          (int)req->is_first_of_cluster);
   ```
3. 在 `sw/runtime/common/queue.cpp` 的 WR 宏处（L401 起）按 `addr` 名字加打印，观察 ~18 条 DCR 写的顺序与值。
4. 重跑，收集两份日志。

**需要观察的现象**：
- WR 日志显示 `GRID_DIM_X/Y/Z`、`BLOCK_DIM_X/Y/Z`、`CLUSTER_DIM_X/Y/Z` 的值——这就是「grid/block/cluster 维度的来源」；
- KMU 日志显示 CTA 的产出顺序，验证 4.5.4 的遍历规则。

**预期结果**：你能把 ①主机 WR 的维度 → ②KMU step 的 block_idx → ③CtaDispatcher 的 warp 切片三层对上号，画出完整的控制流图。

**待本地验证**：实际 `block_idx` 序列与 CTA 总数取决于 demo kernel 的启动维度；若不便改源码，可改为用 `--debug` 生成 trace 后用 `ci/trace_csv.py`（u13-l2）检索 CTA 派发记录。

#### 4.6.5 小练习与答案

**练习 1**：为什么一次 launch 要发约 18 条 `CMD_DCR_WRITE` 而不是一条命令？
**答**：当前 launch 是「DCR 写 dance + CMD_LAUNCH」的经典编码（command_processor.md §10 第 5 条）。未来 `CMD_LAUNCH_QMD`（opcode 0x0B，仿真 CP 已支持，RTL 待镜像）会把描述符折叠成内存里一份 QMD 列表、用一条命令 replay，把这 ~18 条压成 1 条（cmd_processor.cpp:303-322 的 `apply_qmd_`）。

**练习 2**：仿真 CP 与 RTL CP 目前有哪些已知差异？
**答**：① 仿真 CP 有 `CP_SATP` 与 `cp_translate` 页表遍历（DMA 感知 MMU），RTL CP 尚无；② 仿真 CP 是字节精确 DMA，RTL CP 把尺寸向上取整到 64B 倍数；③ 仿真 CP 解码 `CMD_DRAW`/`CMD_LAUNCH_QMD`（`SUPPORTS_DRAW`/`SUPPORTS_QMD`=1），RTL CP 暂清零并回退到环批次。这些都记在 command_processor.md §10。

---

## 5. 综合实践

**任务：画出一次 `vx_start` 的完整时序图并标注「谁拥有 grid/block/cluster 维度」。**

要求：

1. 选一个 `tests/regression` 下的小程序（如 `demo`），用 `ci/blackbox.sh` 在 SimX 上跑通。
2. 画一张纵向时序图，包含这些泳道：**主机 runtime**、**CP（fetch/engine/launch）**、**KMU**、**CtaDispatcher/Scheduler**。
3. 在图上标出三类关键事件：
   - 主机发出的 `CMD_DCR_WRITE` 批（标注哪些写 grid/block/cluster 维度、值是多少）；
   - `CMD_LAUNCH` 抵达、`VX_cp_launch` 的 start 脉冲与 busy 起/落；
   - KMU 产出的 CTA 序列（标注 `block_idx` 与 `is_first_of_cluster`）。
4. 用一句话回答：**如果只把 `GRID_DIM_X` 改成 2 倍，图里哪一段会变长？**（答：KMU 产 CTA 的拍数与 CtaDispatcher 派发的 warp 数翻倍，CP 的 `WAIT_DRAIN` 拉长，但主机 WR 批与 start 脉冲不变。）

进阶：阅读 command_processor.md §10 第 5 条「QMD-style atomic CMD_LAUNCH」，思考把 ~18 条 DCR 写压成一条 `CMD_LAUNCH_QMD` 后，你的时序图哪一段会消失。

---

## 6. 本讲小结

- **CP 是唯一控制平面**：主机经 AXI-Lite 编程环与门铃，CP 从主机内存的命令环取指，经引擎 FSM 分类到 KMU/DMA/DCR/EVT 四资源，串行化访问。
- **命令 = 4B 头 + opcode 载荷**：每 64B 行最多 5 条，零头终止；门铃在 `Q_TAIL_HI` 原子提交；完成靠写回 seqnum、主机轮询 `Q_SEQNUM`。
- **引擎 FSM（IDLE→DECODE→BID→WAIT_DONE→RETIRE）**：`classify` 把 opcode 映射到资源，done 脉冲广播但只有授权 CPE 接收；退休经 completion 握手防丢 seqnum。
- **`VX_cp_launch` 是 start/busy 握手包装器**：四态机在内核整个运行期持有 KMU 仲裁器，start 脉冲 → 等 busy 升 → 等 busy 落 → done，是 launch 的隐式栅栏。
- **KMU 用 `group_origin + intra_offset` 双层计数器遍历 grid**：先填 cluster 内、再按 `cluster_dim` 步长推进 group，使 cluster 成员连续驻留（DXA 多播的基础）；`is_first_of_cluster` 触发下游预留连续 LMEM 槽。
- **完整路径**：主机 WR 约 18 条 `CMD_DCR_WRITE`（装 PC/entry/param/各维）+ `CMD_LAUNCH` → CP 解包 → KMU 武装与遍历 → CtaDispatcher 切 warp → Scheduler；RTL CP 与仿真 CP 是该路径的 model parity 双实现。

---

## 7. 下一步学习建议

- **内存侧的 cache flush**：本讲看到 `CMD_LAUNCH` 后强制 `CMD_CACHE_FLUSH`，它的硬件落点是 `VX_cp_dcr_proxy` 扫描 `VX_DCR_BASE_CACHE_FLUSH`——这是 u8-l1/u8-l2 的内容，建议接着读以理解「为何 launch 后必须 flush 才能读回结果」。
- **多队列并发与 QMD launch**：command_processor.md §10 第 5、6 条描述了 `CMD_LAUNCH_QMD` 与多队列并发的未来方向，是理解 CP 性能演进的关键。
- **CP 的 DMA 与虚拟内存**：仿真 CP 的 `cp_translate` 页表遍历（cmd_processor.cpp:157-201）连接到 u11-l1（虚拟内存子系统）；RTL CP 尚未实现 VM walker，是 model parity 的待补点。
- **RTL CP 单元测试**：`hw/unittest/cp_*` 下有 `cp_launch`、`cp_engine`、`cp_core` 等 Verilator 单测，是验证你理解的最好材料——尝试跑通 `cp_launch` 单测，对照本讲 4.4 节。
