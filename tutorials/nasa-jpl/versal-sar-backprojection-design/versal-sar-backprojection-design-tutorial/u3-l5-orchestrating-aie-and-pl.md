# 用 XRT 编排 AIE 图与 PL 内核

## 1. 本讲目标

前面几讲我们分别把「家当」备齐了：u3-l2 在构造函数里打开了设备、加载了 xclbin、建好了 AIE 图句柄 `m_bp_graph_hdls`、PL 内核句柄 `m_dma_pkt_router_run_hdls` 以及输出 buffer `m_img_buffers`；u3-l3 / u3-l4 又把雷达数据（slowtime、RC）和目标像素网格填进了输入 buffer。但这些都只是「备料」——真正让反投影跑起来、把三类数据送进 AIE、控制图像何时输出、再把结果从 PL 取回主机，全靠本讲要剖析的两个函数：`runGraphs()` 与 `bp()`。

学完本讲你应该能够：

1. 说清 `graph.run(0)` 的「自由运行」语义，以及为什么主机改用「喂数据」而非「指定迭代次数」来驱动整张图。
2. 读懂 `bp()` 里三层循环（脉冲 × 开关 × 重建核）如何用 GMIO `async` 逐脉冲把数据推进 AIE，并用 RTP `update` 控制「最后一脉冲才 dump 图像」。
3. 理解 PL 包路由器如何用 `set_arg` / `start` 启动、再由 `bo.sync(FROM_DEVICE)` 把重排后的图像同步回主机，以及为什么这一句 `sync` 同时充当了「等 PL 完成」的栅栏。

---

## 2. 前置知识

本讲是把前面零散的知识点「串成一条流水线」，需要你先有以下概念（都已在前序讲义建立，这里只做一句话回顾，方便你定位）：

- **XRT（Xilinx Runtime）**：主机侧用来访问 Versal 加速资源（设备、xclbin、AIE 图、PL 内核、buffer）的用户态库。本讲里 `xrt::graph`、`xrt::kernel`、`xrt::run`、`xrt::bo`、`xrt::aie::bo` 都是它的句柄类型（详见 u3-l2）。
- **buffer object（bo）**：一块既能被主机访问、又能被设备（AIE/PL/DDR）访问的共享内存。`xrt::aie::bo` 专门配合 GMIO 通道，`xrt::bo` 是通用 buffer（被 PL 的 `m_axi` 直接寻址）。
- **GMIO 与 PLIO**（详见 u2-l2、u4-l3）：GMIO 是 DDR↔AIE、经 NoC 的 DMA 通道，本设计三类输入（slowtime / RC / 目标像素）全走 GMIO；PLIO 是 AIE↔PL 的 AXI4-Stream 直连、不过 NoC，本设计把 AIE 重建结果以 128 位流交给 PL 包路由器。
- **RTP（Runtime Parameter，运行时参数）**：用 `connect<parameter>` 连线、主机用 `graph.update(...)` 在运行时改值，本设计用它传「现在要不要把累加的图像 dump 出去」的 0/1 开关。
- **Kahn 数据驱动执行**（详见 u2-l3）：AIE 图里的内核「输入就绪才 fire（点火）」，没有全局节拍。这一条是理解本讲「主机只管喂数据、AIE 自然跟着算」的关键。
- **包流（pktstream）与包路由器**：AIE 内核把图像按包头（含 `instance_id`）打成包从 PLIO 流出，顺序会被 AIE 流水打乱；PL 包路由器（`dma_pkt_router`）负责按 `instance_id` 把乱序的包重新拼回连续 DDR 区域（详见 u6-l1）。

一句话总结这三类机制的分工，也是本讲的主线：**GMIO 喂数据进去，buffer/stream 在内核里收并累加，RTP 决定何时输出，PLIO 把结果流到 PL，PL 再写回 DDR 供主机取回。**

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `design/host/sar_backproject.cpp` | 主机控制类实现 | `runGraphs()`（272-277 行）与 `bp()`（279-335 行）的整段编排逻辑 |
| `design/host/main.cpp` | 程序入口 | 如何准备 buffer 数组、调用 `runGraphs()` 与 `bp()`（51-78 行） |
| `design/host/sar_backproject.h` | 类声明 | `bp()` / `runGraphs()` 签名与各类句柄成员（38-80 行） |
| `design/aie/graph.h` | ADF 图拓扑 | GMIO/PLIO/RTP 端口的名字与层级（被 `bp()` 用字符串引用） |
| `design/aie/backprojection.cc` | AIE 内核实现 | `rtp_dump_img_in` 如何触发 `m_img` 的 dump（184、188-213 行） |
| `design/aie/custom_kernels.h` | 内核类声明 | 跨调用持久累加的 `m_img` 缓冲（41 行） |
| `design/pl/dma_pkt_router.cpp` | PL 包路由器 | 接口 pragma、`instance_id`→`ddr_offset` 重排（11-16、63 行） |

记住一条线索：`bp()` 里出现的那些长字符串（如 `"bpGraph[0].bpCluster[3].gmio_in_xyz_px"`）并不是凭空写的，它们必须和 `graph.h` 里端口的层级与命名**逐字符对得上**——这正是 u3-l2 强调的「主机字符串与 `system.cfg` 的 `nk=` 实例名一一对应」在端口层面的延续。

---

## 4. 核心概念与源码讲解

### 4.1 `runGraphs()` 与 `graph.run(0)`：让图自由运行

#### 4.1.1 概念说明

ADF 图有几种运行模式：

- `graph.run(N)`：图跑恰好 `N` 轮迭代后自动停。
- `graph.run()` 或 `graph.run(0)`：图进入**自由运行（free-running / indefinite）**，没有固定迭代次数，一直跑直到主机调用 `graph.end()` 或程序退出。

为什么反投影要选自由运行？因为「一轮迭代」对内核来说只是「输入就绪 → fire 一次」。而反投影的进度单位是**脉冲（pulse）**，一帧图像要喂 `PULSES`（默认 602）个脉冲。如果把迭代数写死成 602，就等于把「算法节奏」硬编码进图本身；而本项目的设计哲学是：**图只负责「数据来了就算」，算法节奏交给主机用 GMIO 投递来控制**。于是 `run(0)` 把图「武装好」让它待命，接下来 `bp()` 每往 GMIO 推一脉冲数据，相关的广播核、解复用核、重建核就被数据驱动着 fire 一轮。

换个说法：`run(0)` 之后，主机和 AIE 之间是**生产者–消费者**关系——主机是生产者（推数据），AIE 图是消费者（数据到哪就算到哪），中间没有「主机发一条命令、AIE 算一步」的握手节拍。

#### 4.1.2 核心流程

```
main.cpp 调用 ifcc.runGraphs()
        │
        ▼
runGraphs():  对每个实例 i ── m_bp_graph_hdls[i].run(0)
        │
        ▼  (立即返回，非阻塞)
   AIE 图进入自由运行：内核「输入就绪才 fire」
   此时还没有数据，图在等 GMIO/PLIO 端口来数据
        │
        ▼
   控制权回到 main，进入 bp()，由 bp() 开始喂数据
```

注意：`run(0)` 是**非阻塞**的，它只是下达「开始运行」的指令后立刻返回。真正让内核动起来的，是后续 `bp()` 里推到 GMIO 的数据。

#### 4.1.3 源码精读

`runGraphs()` 极短，核心就一句 `run(0)`：

[design/host/sar_backproject.cpp:272-277](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L272-L277) —— 遍历所有实例，对每个 AIE 图句柄调用 `run(0)`，让图无限期自由运行。

调用点在 `main.cpp`，被计时埋点包裹：

[design/host/main.cpp:51-56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/main.cpp#L51-L56) —— 主机在此启动所有 AIE 图。注意它打印的耗时是「主机下达 `run` 指令的时间」，并不是反投影的计算时间（那要等 `bp()` 真正喂数据后才发生）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：理解 `run(0)` 与「逐脉冲喂数据」的耦合关系。
2. **操作步骤**：
   - 打开 `design/host/sar_backproject.cpp`，定位 272-277 行的 `runGraphs()`。
   - 再打开 `design/host/main.cpp`，确认 `runGraphs()`（54 行）调用在 `bp()`（73 行）**之前**。
   - 思考：如果有人误把 `run(0)` 改成 `run(PULSES)`（即 `run(602)`），程序会怎样？
3. **需要观察的现象**：理论上图会在累计 fire 满一定次数后自行停止；但由于「一次 fire」的计数口径与「一个脉冲」并不简单相等（一次脉冲会触发广播核 1 次、每个重建核 1 次，计数方式取决于 ADF 对 `run(N)` 中 N 的定义），改成 `run(602)` 很可能让图在中途意外停摆，导致后面脉冲的数据无人消费、GMIO 通道阻塞。
4. **预期结果**：`run(0)` 是与本设计「主机驱动节奏」相匹配的唯一正确选择。结论：**不要给自由运行的图写死迭代数**。
5. 本结论为「待本地验证」的部分：精确的 `run(N)` 计数语义以你本地 Vitis 版本的 ADF 文档为准。

#### 4.1.5 小练习与答案

**练习 1**：`runGraphs()` 里为什么要 `for (int i = 0; i < m_instances; i++)` 循环，而不是只调一次 `run(0)`？

**参考答案**：因为本项目预留了「多实例」能力——每个实例对应一张独立的 `bpGraph[i]` 图（见 u3-l2 构造函数里的 `m_bp_graph_hdls.push_back(...)`）。`run(0)` 是针对**单张图句柄**下达的指令，所以要逐张启动。当前 `INSTANCES=1`，循环只跑一次，但结构上为多实例留了门。

**练习 2**：`run(0)` 返回后，AIE 内核立刻开始算了吗？

**参考答案**：没有。`run(0)` 只是把图「武装」到自由运行状态。内核遵循 Kahn 数据驱动模型，必须等 GMIO 端口把数据送到、输入就绪才会 fire。在 `bp()` 推第一脉冲数据之前，图处于「待命空转」。

---

### 4.2 `bp()` 中的 GMIO `async` 与 RTP `update`：逐脉冲喂数据、控制 dump 时机

这是本讲最核心、也最长的一段。`bp()` 做两件事：**把三类输入逐脉冲推进 AIE，并用 RTP 告诉重建核「现在该不该把累加图像 dump 出来」**。它本质上是主机在用 GMIO + RTP 当「遥控器」，驱动一张自由运行的图。

#### 4.2.1 概念说明

先认清三个 API：

1. **GMIO 异步投递 `xrt::aie::bo::async(port_name, dir, size, offset)`**：把 buffer 里 `offset` 处、长度 `size` 字节的数据，沿 `port_name` 指定的 GMIO 端口**非阻塞地**推进 AIE。方向常量 `XCL_BO_SYNC_BO_GMIO_TO_AIE` 表示「从 DDR 流向 AIE」（站在 AIE 视角是「数据来到我这里」）。因为是 `async`，它**立刻返回**，传输在后台由 GMIO DMA 引擎执行。
2. **RTP 更新 `xrt::graph::update(port_name, value)`**：把 `value` 写到 `port_name` 指定的运行时参数端口；内核在**下一次 fire 读这个参数**时拿到新值。本设计里 `rtp_dump_img_in` 就是 0/1 开关。
3. **端口名的层级字符串**：例如 `"bpGraph[0].bpCluster[3].gmio_in_xyz_px"` 表示「第 0 张图的第 3 个子图（switch）的像素 GMIO 端口」。这串名字和 `graph.h` 里 `input_gmio::create(...)` 的实例化结果必须对得上。

再看反投影算法对「节奏」的要求（这是理解整段循环的钥匙）：

- 每个目标像素都要与**每一个脉冲**做一次「算双程时延 → 取 RC 样本 → 相位校正 → 累加」。
- 因此 224 个重建核里各自维护一个**跨调用持久**的累加缓冲 `m_img`（见 [design/aie/custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)），每来一个脉冲就 `m_img[...] += img`（[design/aie/backprojection.cc:184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L184)）。
- 图像必须等**全部 PULSES 个脉冲都累加完**才算聚焦完成。中途 dump 出来的是半成品（模糊、不聚焦）。
- 所以 RTP 策略很自然：**前 PULSES−1 个脉冲传 0（只累加、不输出），最后 1 个脉冲传 1（既累加最后一份、又把完整图像 dump 出去）**。这正对应 [design/aie/backprojection.cc:188](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L188) 的 `if (rtp_dump_img_in) { ... 打包写出 ... }`。

#### 4.2.2 核心流程

`bp()` 的喂数据部分是一个「四层嵌套」结构（实例层 → 脉冲层 → 开关层 → 重建核层）：

```
对每个 buff_idx (实例/缓冲层, 0 .. num_of_buffers-1):
 │
 ├─ slowtime：整块 async 一次（全部 PULSES 行）──► gmio_in_st
 │
 └─ 对每个 pulse_idx (脉冲层, 0 .. PULSES-1):       ← 算法的时间轴
      ├─ RC：本脉冲的 1 条距离线 async ───────────► gmio_in_rc
      │       (offset = pulse_idx*RC_SAMPLES)
      │
      ├─ 对每个 sw_id (开关层, 0 .. AIE_SWITCHES-1):
      │     目标像素的「第 sw_id 切片」async ──────► bpCluster[sw_id].gmio_in_xyz_px
      │       (offset = sw_id * px_per_demux_kern)
      │
      └─ 对每个 kern_id (重建核层, 0 .. IMG_SOLVERS-1):
            RTP update：pulse_idx==PULSES-1 ? 1 : 0 ─► rtp_dump_img_in[kern_id]
```

几个要点先点破：

- **slowtime 整块投递一次**：slowtime（每脉冲的天线 X/Y/Z 与参考距离）虽然也是「每脉冲一组 4 个 float」，但主机把它当作一整条流一次性推进 `gmio_in_st`。广播核 `data_broadcast_kern` 每次 fire 只 `readincr_v<4>` 读 4 个 float（[design/aie/backprojection.cc:50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L50)），与「每脉冲一条 RC 线」的节奏自然对齐——Kahn 模型保证第 N 次 fire 读到的就是第 N 组 slowtime。
- **RC 与目标像素逐脉冲投递**：RC 是「随脉冲变化」的回波，必须按脉冲切片；目标像素虽然每个脉冲都一样（同一批像素要对每个脉冲算一次），但也要**每个脉冲重复推一遍**，因为每个重建核每次 fire 都要重新读入它负责的那批像素。
- **像素按 switch 切片**：`px_per_demux_kern = (PULSES*RC_SAMPLES)/AIE_SWITCHES`，把全部目标像素均分成 `AIE_SWITCHES`（默认 7）份，每份喂给一个 switch 的 `gmio_in_xyz_px`；再由该 switch 的 `px_demux_kern` 用包交换分发到 32 个重建核（详见 u5-l2）。
- **RTP 逐核更新**：每个脉冲、每个重建核都更新一次 RTP（共 `PULSES × IMG_SOLVERS` 次 `update`）。绝大多数写的是 0，只有最后一个脉冲写 1。

#### 4.2.3 源码精读

先看 `bp()` 的签名与一个「未使用」的细节：

[design/host/sar_backproject.cpp:279-285](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L279-L285) —— `bp()` 接收四类 buffer 数组指针与 `num_of_buffers`；282 行声明了 `buff_async_hdls`（异步句柄向量）却**从未填充**——所有 `async()` 的返回值都被丢弃了。这意味着主机对每一次 GMIO 投递都「发射后不管」，不在单次传输上阻塞等待，整段同步只靠最后的 `sync(FROM_DEVICE)` 兜底（见 4.3）。`px_per_demux_kern` 在 285 行算出每个 demux 核要处理的像素数。

slowtime 整块投递（每个 buff_idx 只发一次）：

[design/host/sar_backproject.cpp:289-292](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L289-L292) —— 把整个 slowtime buffer（`PULSES*BC_ELEMENTS*sizeof(float)` 字节，即全部 602 行 ×4 列）一次性 `async` 推进 `gmio_in_st`，偏移为 0。

脉冲循环里的 RC 投递（每脉冲一条距离线）：

[design/host/sar_backproject.cpp:293-297](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L293-L297) —— 每个 `pulse_idx` 只取 `RC_SAMPLES*sizeof(cfloat)` 字节（一条距离线），偏移随脉冲递增 `(pulse_idx*RC_SAMPLES)*sizeof(cfloat)`。方向 `XCL_BO_SYNC_BO_GMIO_TO_AIE` = 数据流入 AIE。

像素按 switch 切片投递（每脉冲、每 switch 一份）：

[design/host/sar_backproject.cpp:300-305](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L300-L305) —— 每个 `sw_id` 推 `px_per_demux_kern*sizeof(float)*3` 字节（一份像素的 X/Y/Z，故乘 3），偏移 `sw_id*px_per_demux_kern*sizeof(float)*3`，端口名层级到 `bpCluster[sw_id].gmio_in_xyz_px`。注意：**每个脉冲都把同一批像素重推一遍**，因为每个脉冲都要对所有像素重算一次。

RTP 逐核更新——本模块的「节奏开关」：

[design/host/sar_backproject.cpp:307-314](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L307-L314) —— 对全部 `IMG_SOLVERS`（默认 224）个重建核，在最后一脉冲（`pulse_idx==PULSES-1`）把 `rtp_dump_img_in` 置 1，其余脉冲置 0。这就是「累加到最后一刻才输出」的主机侧控制。

RTP 的「消费端」——重建核里如何使用这个值：

[design/aie/backprojection.cc:62-66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L62-L66) —— `img_reconstruct_kern` 的最后一个形参 `int rtp_dump_img_in` 就是从 RTP 端口读到的值。

[design/aie/backprojection.cc:184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L184) —— 每次fire都在累加：`m_img[(px_seg_idx*16)+px_idx] += img`，无论 RTP 是 0 还是 1。

[design/aie/backprojection.cc:188-213](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L188-L213) —— 只有 `rtp_dump_img_in` 为真时，才把累加完毕的 `m_img` 打成带包头的包流写出。可以看到内核的 fire 流程是「无条件累加 + 有条件输出」，与主机「最后脉冲才置 1」配合得天衣无缝。

最后回到端口命名：`bp()` 里那些字符串必须和 `graph.h` 的端口一一对应。例如顶层 GMIO：

[design/aie/graph.h:149-150](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L149-L150) 与 [design/aie/graph.h:165-166](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L165-L166) —— 顶层图的 `gmio_in_st` / `gmio_in_rc` 端口，名字里的 `bp_graph_insts` 计数器使每个实例命名唯一（如 `gmio_in_st_0`），但主机侧用层级名 `bpGraph[0].gmio_in_st` 访问。

[design/aie/graph.h:153](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L153) 与 [design/aie/graph.h:189-193](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L189-L193) —— RTP 端口 `rtp_dump_img_in[IMG_SOLVERS]` 通过 `connect<parameter>` 连到每个重建核的 `in[3]`。主机用 `bpGraph[0].rtp_dump_img_in[kern_id]` 寻址。

[design/aie/graph.h:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L35) 与 [design/aie/graph.h:66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L66) —— 子图的 `gmio_in_xyz_px` 端口，主机用 `bpGraph[0].bpCluster[sw_id].gmio_in_xyz_px` 寻址。

> 一句话锚点：**主机字符串寻址 = 图的层级路径**。写错一个字符，XRT 在运行时就找不到端口。

#### 4.2.4 代码实践（源码阅读 + 计算）

1. **实践目标**：把「四层嵌套循环」的内层三圈（脉冲 × 开关 × 重建核）在纸上画成时序，并验证关键数值。
2. **操作步骤**：
   - 在 `common.h` 读出 `PULSES=602`、`RC_SAMPLES=512`、`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS=224`。
   - 计算 `px_per_demux_kern = (602*512)/7 = 44032` 个像素（每 switch 一次 GMIO 推送的数据量）。
   - 计算一次完整 `bp()`（`num_of_buffers=1`）里：
     - slowtime `async` 次数 = **1**；
     - RC `async` 次数 = `PULSES` = **602**；
     - 像素 `async` 次数 = `PULSES × AIE_SWITCHES` = **4214**；
     - RTP `update` 次数 = `PULSES × IMG_SOLVERS` = **134848**。
   - 把上述数字填进下面这张时序表（脉冲维度展开头尾两个脉冲即可）：

     | 时刻 | RC async | 像素 async（每 switch） | RTP update（每核） |
     | --- | --- | --- | --- |
     | pulse 0 | 推第 0 条距离线 | 推各 switch 像素切片 | 全 224 核 ← 0 |
     | ... | ... | ... | ... |
     | pulse 601（最后） | 推第 601 条距离线 | 推各 switch 像素切片 | 全 224 核 ← 1 |

3. **需要观察的现象**：RTP `update` 的调用次数（约 13.5 万次）远多于 GMIO `async`（约 4.8 千次）。这说明主机在编排阶段的开销主要来自「逐核、逐脉冲」的 RTP 写入。
4. **预期结果**：你能口头复述「最后一脉冲才把 RTP 置 1」的原因——因为 `m_img` 必须累加满全部 602 个脉冲才是聚焦完整的图像，中途置 1 会 dump 出半成品。
5. 数值结果可直接手算验证；性能含义（RTP 开销）为「待本地验证」的度量项，可结合 u8-l2 的计时进一步确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 slowtime 用「整块一次 async」，而 RC 必须按脉冲逐条 async？

**参考答案**：slowtime 在 `data_broadcast_kern` 里是通过 **stream** 消费（每次 `readincr_v<4>` 读 4 个 float），整块推进后由内核按 fire 节奏逐组读取，流自带缓冲，所以一次性推没问题。而 RC 是 **buffer** 端口（`input_buffer<cfloat, extents<RC_SAMPLES>>`），每次 fire 要求一整条 `RC_SAMPLES` 的距离线就绪，所以主机必须按脉冲切片、逐条喂，让第 N 次 fire 拿到第 N 条距离线。两者的端口类型决定了投递粒度。

**练习 2**：如果把「最后一脉冲才置 1」误写成「每一脉冲都置 1」，图像会变成什么样？

**参考答案**：每个脉冲 fire 结束都会触发 `if (rtp_dump_img_in)` 把**当时**的 `m_img` 打包输出一次。于是 PL 包路由器会收到 602 倍的数据（且每份都是「只累加了前 k 个脉冲」的半成品），不仅 DDR 被反复覆盖、最终图像只是「第 602 脉冲累加后」的结果（碰巧可能接近正确），中间还会产生大量无效搬运。正确做法是只在最后一脉冲置 1，保证只输出一次完整聚焦图像。

**练习 3**：`async()` 的返回值（异步句柄）被丢弃了，这安全吗？

**参考答案**：在本设计的同步模型下是安全的。所有输入投递都是「发射后不管」，数据由 GMIO DMA 引擎在后台搬到 AIE；主机唯一阻塞等待的点是 4.3 节的 `bo.sync(FROM_DEVICE)`。只要这个最终同步发生在「所有数据都已投递、且 PL 已把结果写回 DDR」之后，就不会有数据竞争。代价是丢失了「按单次传输精细同步」的能力——如果将来要做流水线重叠（一边算一边喂下一帧），就需要保留并等待这些异步句柄。

---

### 4.3 PL `set_arg` / `start` + `sync(FROM_DEVICE)`：启动包路由器、取回图像

数据喂完、最后一脉冲的 RTP 已置 1，AIE 重建核会把聚焦好的 `m_img` 打成包从 PLIO 流出。但这些包是**乱序**的（AIE 流水线优化所致），而且分散在 7 个 switch 各自的 PLIO 端口上。**PL 包路由器 `dma_pkt_router` 的职责就是把这些乱序包按 `instance_id` 重排，写回连续的 DDR 区域，供主机取回。** 本节讲主机如何启动它、又如何把结果搬回主机。

#### 4.3.1 概念说明

先区分两类「同步」API（名字相近但语义不同，别混淆）：

- **输入侧**：`xrt::aie::bo::async(..., XCL_BO_SYNC_BO_GMIO_TO_AIE, ...)` —— 非阻塞，把主机数据推进 AIE。
- **输出侧**：`xrt::bo::sync(XCL_BO_SYNC_BO_FROM_DEVICE, size, offset)` —— **阻塞**，等设备把 DDR 里这块 buffer 的内容「同步」回主机内存（方向 `FROM_DEVICE` = 从设备取回）。

再看 PL 内核的句柄操作（详见 u3-l2）：

- `xrt::run::set_arg(idx, value)`：给内核的第 `idx` 个参数设定值（对 PL 内核而言，参数顺序就是 HLS 函数签名顺序）。
- `xrt::run::start()`：**非阻塞**启动内核，立刻返回。

最后要理解 PL 内核如何「不重叠地」共写同一个 img buffer：每个 PL 路由器都被 `set_arg(1, 同一个 img_bo)` 指向同一块输出 buffer，但它在内部按包头里的 `instance_id` 算出自己的写入偏移 `ddr_offset = instance_id * SAMPLES_PER_KERN`（[design/pl/dma_pkt_router.cpp:63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L63)）。因为 224 个重建核的 `instance_id` 全局唯一（0..223），每个写 `SAMPLES_PER_KERN` 个像素，正好把整块图像 buffer 平铺成 224 段、互不重叠。

#### 4.3.2 核心流程

`bp()` 的「PL 启动 + 取回」部分（紧接 4.2 的脉冲循环之后）：

```
对每个 sw_id (0 .. AIE_SWITCHES-1):
 │   pl_kern_id = buff_idx*m_instances + sw_id
 ├─ set_arg(1, buffers_img_out[buff_idx])   ← 把第 2 个参数(ddr_mem)指向 img buffer
 └─ start()                                   ← 非阻塞启动该 PL 路由器
        │  (PL 内核在 pl_stream_in.read() 上等待 AIE 的 PLIO 数据)
        ▼
   PL 路由器读包头 → 算 ddr_offset → 把 32 个核的数据按序写入 img buffer 的对应区段

最后：
buffers_img_out[buff_idx].sync(XCL_BO_SYNC_BO_FROM_DEVICE, PULSES*RC_SAMPLES*sizeof(cfloat), 0)
        │  (阻塞：等 PL 写完 + 把 DDR 内容搬回主机)
        ▼
   m_img_arrays[buff_idx] 指向的主机内存里就是完整的重建图像
        │
        ▼  (控制权回到 main.cpp:78)
   writeImg() 把它写成 CSV
```

关键时序关系（也是本讲的「对齐」要点）：

- 主机在**脉冲循环全部结束后**才 `start()` PL 路由器。此时所有输入数据都已投递、最后一脉冲的 RTP=1 也已下达，AIE 即将（或正在）dump。
- `start()` 非阻塞，PL 路由器立刻在 `pl_stream_in.read()` 上阻塞等待——它要等 AIE 把包流过来。所以 PL 与 AIE 是 **AXI-Stream 上的生产者–消费者**，主机不参与它们的逐拍握手。
- 主机自己**不显式等待 AIE 完成**，也不显式 `wait()` 每个 PL 内核（代码里那几行 `wait()` 是被注释掉的，见 4.3.3）。主机唯一的阻塞点是最后的 `sync(FROM_DEVICE)`——它既要「把 DDR 的图像搬回主机」，又**顺带充当了「等 PL 写完」的栅栏**。

#### 4.3.3 源码精读

PL 路由器循环——每个 switch 启动一个：

[design/host/sar_backproject.cpp:317-325](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L317-L325) —— `pl_kern_id = buff_idx*m_instances + sw_id` 在多实例时把不同实例的 PL 句柄错开；当前 `m_instances=1`，即 `pl_kern_id = sw_id`。`set_arg(1, buffers_img_out[buff_idx])` 把 PL 内核的**第 2 个参数**（`ddr_mem`）指向输出 img buffer；`start()` 非阻塞启动。

被注释掉的显式等待——值得品味的设计取舍：

[design/host/sar_backproject.cpp:327-330](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L327-L330) —— 作者注释掉了「逐 switch `wait()`」的写法，改用下面一句 `sync` 兜底。说明在本流程的 XRT 语义下，`sync(FROM_DEVICE)` 已经足够保证 PL 写完成；逐个 `wait()` 是更「显式但冗余」的等法。

最终的阻塞同步——把图像搬回主机：

[design/host/sar_backproject.cpp:333](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L333) —— `buffers_img_out[buff_idx].sync(XCL_BO_SYNC_BO_FROM_DEVICE, PULSES*RC_SAMPLES*sizeof(cfloat), 0)`：方向 `FROM_DEVICE`，搬运整块图像（`602*512` 个 cfloat）。这一句返回后，主机映射指针 `m_img_arrays[buff_idx]` 里就是完整图像。它也是 `bp()` 里**唯一**的主机阻塞点。

PL 内核签名与接口 pragma——理解 `set_arg(1, ...)` 的依据：

[design/pl/dma_pkt_router.cpp:11-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L11-L16) —— 签名 `(hls::stream<ap_axiu<128,0,0,0>> &pl_stream_in, ap_uint<64>* ddr_mem)`。**arg 0 = `pl_stream_in`**（来自 AIE PLIO 的 AXI-Stream，由 `system.cfg` 的 `stream_connect` 接好，主机不用管），**arg 1 = `ddr_mem`**（`m_axi bundle=gmem`，主机用 `set_arg(1, img_bo)` 指定写入目标）。`s_axilite bundle=control` 让 `return` 与 `ddr_mem` 偏移可被主机经控制总线启动/传参。

重排的核心——按 `instance_id` 算偏移：

[design/pl/dma_pkt_router.cpp:18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L18) 与 [design/pl/dma_pkt_router.cpp:63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L63) —— `SAMPLES_PER_KERN = (PULSES*RC_SAMPLES)/IMG_SOLVERS = 1376`；`ddr_offset = instance_id * SAMPLES_PER_KERN`。于是内核 `instance_id` 越大，写到 img buffer 越靠后；7 个 PL 路由器各自处理 32 个核（`IMG_SOLVERS_PER_SWITCH`），合起来覆盖全 224 核，拼出整张图像。

[design/pl/dma_pkt_router.cpp:31-71](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L31-L71) —— 外层 `IMG_KERNEL_LOOP` 遍历本 switch 的 32 个核：先读 128 位包头（[51-56 行](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L51-L56) 解析 `pkt_id`/`instance_id`），再在 `IMG_DATA_LOOP` 里把 `SAMPLES_PER_KERN` 个 cfloat 按 `ddr_offset` 写入 `ddr_mem`。

PLIO 输出端口——AIE 侧的「流出」对接点：

[design/aie/graph.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L41) 与 [design/aie/graph.h:73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L73)、[design/aie/graph.h:82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L82) —— 子图的 `plio_pkt_rtr_out`（128 位 PLIO），由 `connect(mg.out[0], plio_pkt_rtr_out.in[0])` 把 32 个重建核经 `pktmerge` 汇合后流给 PL。这个 PLIO 在 `system.cfg` 里会被接到对应 `dma_pkt_router` 实例的 `pl_stream_in`（详见 u7-l1）。

#### 4.3.4 代码实践（源码阅读 + 推演）

1. **实践目标**：理解「7 个 PL 路由器写同一块 img buffer 却不冲突」的机制，以及 `sync` 充当栅栏的原因。
2. **操作步骤**：
   - 在 `dma_pkt_router.cpp` 读出 `SAMPLES_PER_KERN = (602*512)/224 = 1376` 个 cfloat/核。
   - 推演：1 个 PL 路由器写 `32 × 1376 = 44032` 个 cfloat；7 个路由器共写 `7 × 44032 = 308224 = 602×512` 个 cfloat，**正好**等于 img buffer 容量 `PULSES*RC_SAMPLES`。
   - 推演每个重建核的 `instance_id`：由 [design/aie/graph.h:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L53) 的 `IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i` 给出，跨 7 个子图取值 0..223 全局唯一 → 每核写到 `instance_id×1376` 起的区段，互不重叠。
   - 回答：既然 7 个 PL 路由器**并发写**同一 buffer 的不同区段，主机在 `sync(FROM_DEVICE)` 之前为什么敢不 `wait()`？
3. **需要观察的现象**：你应当得出——`sync(FROM_DEVICE)` 本身是阻塞调用，它在执行「DDR→主机」搬运前需要确保设备侧对该 buffer 的写入已完成；在当前 XRT 流程里，这一句既是数据搬运、也隐含了对 PL 写完成的等待。被注释的 `wait()` 是更直白但冗余的写法。
4. **预期结果**：能画出「img buffer 被 224 个核的 instance_id 平铺成 224 段、7 个 PL 路由器各负责 32 段」的示意图，并解释 `set_arg(1, img_bo)` 让 7 个路由器共享同一基址、由 `ddr_offset` 区分写入位置。
5. 关于「`sync(FROM_DEVICE)` 是否在所有 Vitis 版本都隐含等待 PL 写完成」，标注为「待本地验证」——若你的版本下出现读到半幅图像，可取消 327-330 行的 `wait()` 显式等待。

#### 4.3.5 小练习与答案

**练习 1**：`set_arg(1, buffers_img_out[buff_idx])` 里的 `1` 是怎么来的？

**参考答案**：它是 PL 内核 `dma_pkt_router(pl_stream_in, ddr_mem)` 签名里**第 2 个参数**（从 0 起算下标为 1），即 `ddr_mem`。第 1 个参数 `pl_stream_in`（下标 0）是 AXI-Stream，由 `system.cfg` 的 `stream_connect` 在 AIE PLIO 与 PL 内核间硬连接，主机无需也不能用 `set_arg` 设它。所以主机只需设定 `ddr_mem` 这一个参数。

**练习 2**：为什么 7 个 PL 路由器可以同时 `start()`，而不用担心它们写坏彼此的数据？

**参考答案**：因为它们写到 img buffer 的**不同区段**。每个路由器处理 32 个核，每个核的数据按 `ddr_offset = instance_id × SAMPLES_PER_KERN` 写入；224 个核的 `instance_id` 全局唯一，因此 7 个路由器各自写入的 `[instance_id×1376, (instance_id+1)×1376)` 区段互不重叠。并发只是「各自画各自的那块拼图」。

**练习 3**：把最后的 `sync(FROM_DEVICE)` 删掉会怎样？

**参考答案**：主机映射指针 `m_img_arrays[0]` 里会读不到（或只读到旧的）图像数据——因为 `xrt::bo` 有「主机内存」与「设备 DDR」两份，平时主机改的是主机内存、设备改的是 DDR，两者靠 `sync` 来对齐。`FROM_DEVICE` 正是把设备侧 PL 写好的结果搬回主机内存；删掉它，后续 `writeImg()` 写出的 CSV 就是空/错的内容。此外，少了这个阻塞点，主机可能在 PL 还没写完时就往下走，引入竞态。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「端到端时序图」并回答三个问题。这是一道贯穿 `runGraphs()` + `bp()` 全流程的源码阅读型综合题。

**任务**：在一张大图上并排画出三条时间轴（HOST / AIE / PL），标注下列事件的发生顺序与依赖关系：

1. `main` 调 `runGraphs()` → AIE 图 `run(0)` 自由运行（待命）。
2. `main` 调 `bp()`，主机开始：
   - 一次性 async slowtime；
   - 602 脉冲循环：每脉冲 async 一条 RC 线 + 7 份像素切片 + 给 224 核写 RTP（末脉冲置 1）。
3. 脉冲循环结束后：7 个 PL 路由器 `set_arg(1, img_bo)` + `start()`。
4. `bo.sync(FROM_DEVICE)` 阻塞 → 返回 → `writeImg()` 写 CSV。

**AIE 时间轴**要标出：广播核每脉冲 fire 1 次（把 slowtime+RC 广播给 224 核）；每个重建核每脉冲 fire 1 次（`m_img += ...`）；末脉冲（RTP=1）时重建核 dump → PLIO 流出。

**PL 时间轴**要标出：7 个路由器在 `pl_stream_in.read()` 上等待 → 收到包 → 按 `instance_id` 写 img buffer → 主机 `sync` 取回。

**需要回答的三个问题**（对应规格里的实践任务）：

- **(a)** 为什么「最后一脉冲才把 RTP 置 1」？——因为 `m_img` 要累加满全部 602 脉冲才是聚焦完整的图像；中途置 1 会 dump 半成品，且让 PL 收到 602 倍的无效数据。
- **(b)** PL 内核启动如何与 AIE 输出流对齐到同一 img buffer？——PL 路由器经 `system.cfg` 的 `stream_connect` 接到 AIE 子图的 `plio_pkt_rtr_out`，在 `pl_stream_in.read()` 上阻塞等 AIE dump；主机 `set_arg(1, img_bo)` 让 7 个路由器共享同一输出 buffer 基址，由包头 `instance_id` 计算的 `ddr_offset` 区分各自写入区段，从而把 224 个核的输出拼成一张完整图像。
- **(c)** 整个 `bp()` 里主机到底阻塞了几次、在哪阻塞？——名义上**只阻塞一次**，在 [sar_backproject.cpp:333](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L333) 的 `sync(FROM_DEVICE)`。其余 GMIO `async`、RTP `update`、PL `start()` 全是非阻塞「发射后不管」。

**预期结果**：你能指着这张图讲清楚「主机用 run(0)+GMIO+RTP 遥控一张自由运行的 AIE 图，再用 PL 路由器拼回结果、最后一把 sync 取回」的完整故事。

---

## 6. 本讲小结

- `runGraphs()` 用 `graph.run(0)` 把 AIE 图置为**自由运行**：图本身不计数迭代，算法节奏完全由主机经 GMIO 投递来驱动，内核遵循 Kahn 数据驱动模型「数据就绪才 fire」。
- `bp()` 的核心是四层嵌套循环：slowtime 整块投递一次；RC 逐脉冲按距离线投递；目标像素逐脉冲按 switch 切片投递（每个脉冲都重推同一批像素）；RTP 逐核逐脉冲更新。
- **RTP `rtp_dump_img_in`** 是输出开关：前 601 脉冲置 0（只累加 `m_img`），末脉冲置 1（累加最后一份并 dump 完整图像），保证只输出一次聚焦完整的图。
- 所有 GMIO `async` 与 RTP `update` 都**非阻塞**，异步句柄被丢弃；主机不在单次传输上等待。
- PL 包路由器经 `set_arg(1, img_bo)` + `start()` 非阻塞启动；它按包头 `instance_id` 算 `ddr_offset`，把 224 个核的乱序输出重排进同一 img buffer 的不重叠区段。
- `bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE)` 是 `bp()` 里**唯一**的阻塞点：既把 DDR 图像搬回主机内存，又充当「等 PL 写完」的栅栏（被注释的 `wait()` 是显式但冗余的等法）。

---

## 7. 下一步学习建议

本讲讲清了「主机如何编排 AIE 图与 PL 内核」，但有几处我们只当黑盒用了：

- **`px_demux_kern` 如何用包交换把像素分发到 32 个重建核**：本讲里它只是 `gmio_in_xyz_px` 的下游，详见 **u5-l2（Pixel Demux 内核与包头部）**。
- **重建核内部到底怎么算**（差分距离、相位校正、插值累加）：本讲只用到「它每个脉冲 fire 一次累加 `m_img`」，完整算法见 **u5-l3 / u5-l4 / u5-l5**。
- **`system.cfg` 里 PLIO 与 PL 内核的 `stream_connect` 是怎么生成的**：本讲假设 `pl_stream_in` 已经接好，其自动生成见 **u7-l1（系统集成：system.cfg、XSA 链接与打包）**。
- **PL 包路由器自身的 HLS 实现与仿真**：本讲只从主机视角看它，内核细节与 testbench 见 **u6-l1 / u6-l2**。
- **`run(0)` 的计时与性能口径**：本讲指出 `runGraphs` 的耗时只是「下达指令」，真正的反投影时间要结合 **u8-l2（性能与功耗度量）** 里的分段计时来读。

建议下一步先读 **u5-2** 把包交换补齐，再回到 **u7-l1** 看主机用的这些句柄名/端口名是如何在构建期被自动生成并写进 `system.cfg` 的——那样你就把「主机编排 → AIE 计算 → PL 拼接 → 构建链接」整条链路彻底打通了。
