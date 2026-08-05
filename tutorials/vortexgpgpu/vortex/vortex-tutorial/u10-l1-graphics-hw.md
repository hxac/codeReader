# 图形硬件栈（RASTER/TEX/OM）

## 1. 本讲目标

Vortex 不只是一颗通用 GPGPU，它还实现了传统图形管线的「固定功能（Fixed-Function, FF）硬件」——也就是把光栅化、纹理采样、输出合并这三件图形管线最重的活，从软件内核里搬到专用硬件里加速。本讲聚焦这条 FF 硬件栈，学完后你应当能够：

1. 说清图形固定功能管线的三段（RASTER / TEX / OM）各自做什么、解决什么问题；
2. 在 RTL（`hw/rtl`）与 SimX（`sim/simx`）两侧分别定位到 raster / tex / om 的对应模块，并说出每段流水线的输入与输出；
3. 理解 RASTER 采用「push 派发」把 fragment 工作推给 SIMT 核、而 TEX/OM 作为 SFU 处理单元被着色器调用的关键差异；
4. 理解 early-Z 遮挡剔除为何必须用「严格在后方」判定，以及它如何与 model parity（u7-l4）这条主线对齐。

本讲承接 u8-l4（LSU 流水线）与 u6-l4（功能单元 / SFU 作为分派器），把图形子系统接进你已经建立的「层次即共享边界」「SFU 分派器」「SimX↔RTL 一致性」三条主线。

## 2. 前置知识

在进入 FF 硬件之前，先用三段话补齐图形管线的直觉。

**传统图形管线做什么。** 一帧画面由顶点开始：顶点经过顶点着色器变换到屏幕坐标，组装成三角形（primitive，图元）；接着进入 **光栅化（Rasterization）**——硬件逐像素判断哪些样本点落在三角形内部，把它们打包成一个个 2×2 的「四元组（quad）」；每个被覆盖的样本会运行一段 **片段着色器（Fragment Shader, FS）**，FS 可以采样纹理（**纹理采样 TEX**）算出颜色与深度；最后这些颜色/深度进入 **输出合并（Output Merger, OM）**，做深度测试、模板测试、混合（blend），决定最终写进帧缓冲的值。RASTER/TEX/OM 正是对应这三段的硬件。

**SIMT 与固定功能如何分工。** Vortex 的 FS 跑在普通的 SIMT 核上（和计算 kernel 用同一套 6 级流水线，见 u7-l2），它不是固定功能。固定功能指的是 RASTER（算三角形覆盖）、TEX（采样纹理）、OM（混合写回）这三个**硬件加速器**。它们是「cluster 共享」的——一个 cluster 内所有核共用一套，而不是每核私有。这点与缓存层次「层次即共享边界」的思路一致（u8-l1）。

**定点数据通路。** Vortex 的 FF 单元里**没有任何浮点数据通路**，全是定点（fixed-point），面向移动级功耗。凡是 FF 算不了的活（异构像素格式、特殊混合模式、MSAA resolve），一律走**设备侧 SIMT 软件回退**（`sw/gfx`），绝不在主机侧偷偷做。这条「FF 快路径 + 设备侧软件回退」的双路原则是整个图形栈的基石。

> 名词速查：FF=Fixed-Function 固定功能；FS=Fragment Shader 片段着色器；quad=2×2 像素四元组（光栅化的最小粒度，用于算导数）；ROP=Raster Operation Pipeline，OM 的别名；CTA=Vortex 的线程块（u6-l1）；DCR=设备配置寄存器（u3-l4）；SFU=特殊功能单元（u6-l4）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [docs/designs/graphics_hardware_stack.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md) | FF 硬件的权威设计文档，总览 / ISA / RTL 模块清单 / early-Z / SimX 状态全覆盖 |
| [hw/rtl/VX_graphics.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv) | cluster 级封装：实例化 tex/raster/om 的仲裁器、core、三套 cache，扇出 DCR |
| [hw/rtl/raster/VX_raster_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv) | RASTER 核心：覆盖率行走（tile→block→quad）、early-Z、frame drain |
| [hw/rtl/tex/VX_tex_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tex/VX_tex_core.sv) | TEX 核心：地址生成 → 4 纹素取回 → 双线性采样 |
| [hw/rtl/om/VX_om_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv) | OM 核心：读改写 → 深度/模板测试 → 混合 |
| [hw/rtl/core/VX_sfu_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_sfu_unit.sv) | SFU 分派器：把 `vx_tex4`/`vx_om4` 路由到 TEX/OM 处理单元 |
| [sim/simx/raster/raster_core.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/raster/raster_core.h) / [.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/raster/raster_core.cpp) | SimX RASTER：生产者 FSM + early-Z，RTL 的预言机 |

附带目录：`hw/rtl/raster/`（te/be/slice/edge/extents/qe/earlyz/packer/dispatch/arb）、`hw/rtl/tex/`（addr/mem/format/sampler/lerp/wrap）、`hw/rtl/om/`（mem/ds/blend/compare/stencil_op/logic_op）、`sim/simx/{raster,tex,om}/`。

---

## 4. 核心概念与源码讲解

### 4.1 图形硬件栈总览与集群封装 VX_graphics.sv

#### 4.1.1 概念说明

三个 FF 单元是 **cluster 共享**的固定功能引擎（不是每核私有）。它们之间的关系是一条「推—算—合」的数据流：

- **RASTER 是「推」方**：它自己产生 fragment 工作，把覆盖好的 quad **推（push）** 到 SIMT 核上，触发 FS 运行。FS 自己不主动请求光栅化。
- **TEX 与 OM 是「被调」方**：FS 在运行过程中，通过专用指令 `vx_tex4`（纹理采样）和 `vx_om4`（输出合并提交）调用它们，二者作为 **SFU 的处理单元（PE）** 接入核的流水线（回顾 u6-l4：SFU 本质是一个分派器）。
- 每个单元配一套 cluster 级 cache：**tcache**（纹理）、**rcache**（光栅的图元/tile 缓冲）、**ocache**（颜色+深度帧缓冲，与 early-Z 读相干）。

设计文档里有一张总览图，把这条数据流画得很清楚（RASTER 推 fragment → FS → `vx_tex4`/`vx_om4`）：

[docs/designs/graphics_hardware_stack.md:28-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L28-L59) 给出架构总览与数据流示意图。

#### 4.1.2 核心流程：ISA 与使能

三个单元都是 RISC-V ISA 扩展，挂在 `custom1`（`INST_EXT2 = 0x2B`）槽位下，靠 `funct3` 区分，并各自被一个配置宏门控（这些宏来自 `VX_config.toml`，见 u2-l1）：

- `vx_om4`（funct3=2）：在图形寄存器窗口上提交输出合并；
- `vx_tex4`（funct3=5）：在窗口上做纹理采样（`funct7.mode` 区分单采样与硬件 LOD quad）；
- `SETW`/`GETW`（funct3=6）：图形寄存器窗口的写/读，是 TEX/OM/RASTER 共用的「操作数暂存」原语；
- RASTER **没有内核指令**——它是 push 模型，由硬件自己启动 FS，不存在「着色器发出的光栅化指令」。

指令通过 `MISA` 位对外宣告（TEX=6, RASTER=7, OM=8），三套 DCR 分别配置各单元状态。详见 [docs/designs/graphics_hardware_stack.md:67-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L67-L98)。

#### 4.1.3 源码精读：VX_graphics.sv 集群封装

`VX_graphics.sv` 是一个**真实的封装模块**（设计上特意没有内联进 `VX_cluster.sv`）。它的职责：实例化三个单元各自的 cluster 仲裁器、core、三套 cache，给每个 raster core 设 `INSTANCE_IDX`，暴露 ocache 读口给 early-Z，并扇出 DCR。

[hw/rtl/VX_graphics.sv:14-19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv#L14-L19) 是模块头与「cluster 级封装，拥有共享 TEX/RASTER/OM 及其 cache」的注释。

DCR 扇出：一条 DCR slave 输入按「每消费者一个 master」扇出，每个单元内部的 case 语句按 DCR 地址区间自过滤。消费者数量由三个扩展的 `*_ENABLED`（自动派生镜像，见 u2-l1）与 core 数相乘得到：

[hw/rtl/VX_graphics.sv:80-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv#L80-L95) — `NUM_DCR_REQS` 计算与 `VX_dcr_arb` 扇出。

RASTER 段实例化（注意 `raster_launch_if` 是设备 KMU 委托的 draw launch 输入，`INSTANCE_IDX` 由 cluster id 与 core id 合成）：

[hw/rtl/VX_graphics.sv:287-330](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv#L287-L330) — raster_launch_fork + 每个 raster_core 的实例化。

OM 段的 ocache 是三套 cache 里**唯一可写**的（帧缓冲要回写颜色/深度），且当 early-Z 使能时，raster 引擎的深度读口作为 ocache 的**尾部输入**挂进来，保证 early-Z 读与 OM 的写穿（write-through）深度相干：

[hw/rtl/VX_graphics.sv:547-588](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv#L547-L588) — early-Z 读口作为 ocache 尾部输入，`VX_cache_cluster` 的 `WRITE_ENABLE(1)`、`WRITEBACK(0)`（即写穿）。

最后是「设备忙」聚合：RASTER 用带外（out-of-band）信号告知一帧是否排空（它没有带内 `done` token），OM 在 fragment 在途时保持忙。两者相或后上报 cluster，确保主机在 raster→shader→OM 整条链排空前不会误判设备空闲：

[hw/rtl/VX_graphics.sv:740-750](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_graphics.sv#L740-L750) — `busy = raster_busy_any || om_busy_any`。

#### 4.1.4 代码实践：读图与模块清点

1. **实践目标**：建立「VX_graphics.sv 把哪些零件装在一起」的整体印象。
2. **操作步骤**：
   - 打开 `hw/rtl/VX_graphics.sv`，定位三段 `\`ifdef VX_CFG_EXT_{TEX,RASTER,OM}_ENABLE`；
   - 在每段里数出：仲裁器（`*_cluster_arb`）、core（genvar 实例化）、cache（`VX_cache_cluster`）各一个；
   - 注意 ocache 的 `WRITE_ENABLE` 与 tcache/rcache 的区别。
3. **需要观察的现象**：tcache、rcache、ocache 三段几乎对称，唯独 ocache 多了写口与 early-Z 尾部输入。
4. **预期结果**：你会得到一张「每单元 = 仲裁器 + core + cache」的三联表，ocache 是唯一可写且与 early-Z 共享的那个。
5. 运行结果：待本地验证（本实践是源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 tcache/rcache 的 `WRITE_ENABLE=0`，而 ocache 必须为 1？
> 参考答案：纹理与光栅图元缓冲在渲染一帧期间只读（内容由主机预先写好），不需要 FF 单元回写；而 ocache 承载颜色/深度帧缓冲，OM 必须把混合后的颜色与新深度写回，故必须可写，且采用写穿（`WRITEBACK=0`）以与 early-Z 的相干读一致。

**练习 2**：`NUM_DCR_REQS` 在只有 RTU（光追）、没有 TEX/RASTER/OM 时会是多少？
> 参考答案：RTU 不带设备 CSR，故 `NUM_DCR_REQS = 0`，代码走 `g_dcr_tie` 分支把 DCR 响应直接置零、省掉仲裁器（见 L96-103）。

---

### 4.2 RASTER 固定功能：覆盖率、early-Z 与 push 派发

#### 4.2.1 概念说明

RASTER 解决的问题是：给定一组三角形（图元）的顶点，快速判断屏幕上哪些像素被覆盖，并把这些覆盖信息打包给 FS。它的核心数学是**边函数（edge function）**：对三角形每条边，写一个线性方程 \(E(x,y)=A\cdot x+B\cdot y+C\)，一个样本点被三角形覆盖当且仅当三条边的 \(E\) 都非负（配合「左上填充规则」处理落在边上的样本，避免相邻三角形裂缝或重复）。

Vortex 的 RASTER 还做了两件重要的事：
- **early-Z 遮挡剔除**：在 FS 运行前，先用屏幕空间深度平面估算每个被覆盖像素的深度，与已提交的深度缓冲比较，把**严格在被遮挡**的覆盖位提前清掉，省掉不必要的 FS 计算。
- **push 派发**：把覆盖好的 quad 波打包，**主动**在目标核上启动一个 1-warp 的 fragment CTA。

#### 4.2.2 核心流程

`VX_raster_core` 内部的覆盖率流水线是一条定点行走：

```
VX_raster_mem → VX_raster_te(tile引擎) → VX_raster_be(block引擎)
   → VX_raster_slice / VX_raster_edge / VX_raster_extents（边函数求值）
   → VX_raster_qe（quad引擎，发射 2×2 覆盖戳）
   → [VX_raster_earlyz]（可选，遮挡剔除）
   → raster_bus_if（覆盖 quad 波，送出给核）
```

`VX_raster_mem` 经 rcache 取回 tile/图元缓冲；`VX_raster_edge` 用定点乘法算边函数；`VX_raster_qe` 套用 Vulkan 左上规则做逐样本覆盖测试，发射 quad 戳。early-Z（若使能）在 quad 出口处收紧覆盖掩码。注意：**packer（`VX_raster_packer`）与 dispatch（`VX_raster_dispatch`）在核侧**，不在 `VX_raster_core` 内——core 产出覆盖 quad 波到 `raster_bus_if`，经 `VX_raster_arb` 扇出到各 socket/核，由核侧的 packer 压实稀疏 quad、dispatch 启动 fragment CTA。

控制上是 **push 模型**：KMU 委托的 draw launch 发出一个 `frame_kick`，raster 引擎收到后自驱动加载 tile/图元、行走、发射 quad；FS 不轮询。FS 的入口 PC 与参数骑在 RASTER DCR 上（`FRAG_PC_LO/HI`、`FRAG_ENTRY`、`FRAG_PARAM`），由 CP/运行时写好，raster 引擎据此自启动，无需主机往返。详见 [docs/designs/graphics_hardware_stack.md:193-218](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L193-L218)。

#### 4.2.3 源码精读：frame_kick 与覆盖率行走

模块头与参数（`INSTANCE_IDX` 用于多实例时按条带自选自己的 tile）：

[hw/rtl/raster/VX_raster_core.sv:18-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L18-L61) — RASTER core 模块声明与接口（含 `busy` 带外排空信号）。

push 模型的启动握手：`launch_if.ready = ~armed_r`，`frame_kick` 是 `valid && ready`，kick 后下一拍发一个 `mem_unit_start` 脉冲启动 tile/图元加载；`armed_r` 从 kick 保持到引擎完全排空，驱动 `busy`：

[hw/rtl/raster/VX_raster_core.sv:104-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L104-L124) — frame_kick / armed_r / started_r 控制_push 启动。

覆盖率行走的前端：`VX_raster_mem`（经 rcache 取 tile/图元）、`VX_raster_extents`（算包围盒）、`VX_raster_edge`（边函数求值，延迟 = `LATENCY_IMUL`）：

[hw/rtl/raster/VX_raster_core.sv:134-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L134-L185) — raster_mem + extents + edge，边函数结果用移位寄存器与有效位对齐。

每个 slice 是一条完整的覆盖评估 + early-Z 子流水线，多个 slice 并行，最后由 `VX_raster_arb` 汇聚到 `raster_bus_if`：

[hw/rtl/raster/VX_raster_core.sv:330-399](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L330-L399) — `VX_raster_slice` + 可选 `VX_raster_earlyz`（条件编译 `VX_CFG_RASTER_EARLYZ_ENABLE`）。

early-Z 把本引擎多个 slice 的深度读口合并到单一 ocache 读口（仲裁器在 tag 高位追加 slice 选择位以便响应解复用）：

[hw/rtl/raster/VX_raster_core.sv:408-446](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L408-L446) — early-Z 读口合并到 ocache。

带外排空：`engine_idle` 综合所有在途信号（加载/边/slice/输出总线），`busy = armed_r | frame_kick`，并处理「该引擎本帧分到 0 个 tile」的退化情形（否则 `started_r` 永不置位、`busy` 卡死）：

[hw/rtl/raster/VX_raster_core.sv:471-512](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/raster/VX_raster_core.sv#L471-L512) — engine_idle 判定与 `has_no_tiles` 退化保护。

#### 4.2.4 early-Z 的正确性：严格在后方剔除

early-Z 的难点在于它读到的「已提交深度」并不因果地绑定到当前 fragment——那个深度值可能已经包含了本 fragment 自己将来的写入、等深面的写入，或因果上更晚但更近的写入（fragment 并不严格按提交顺序到达 OM）。所以 early-Z **只能剔除严格在被遮挡的像素**——这是深度比较函数的「自反松弛」：

- `LESS`/`LEQUAL` → 仅当 `cand > stored`（严格更远）才剔除；
- `GREATER`/`GEQUAL` → 仅当 `cand < stored` 才剔除；
- 其他函数（EQUAL/NOTEQUAL/NEVER/ALWAYS）永不提前剔除。

一个可见 fragment 满足 `cand == 最终缓冲深度 ≤ early-Z 读到的任意值`，故「严格在后方」永远不会误杀它——开 early-Z 与纯 ROP 路径**像素一致**，与读新鲜度、流水线顺序无关。详见 [docs/designs/graphics_hardware_stack.md:222-249](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L222-L249)。

#### 4.2.5 代码实践：跟踪一次 draw launch

1. **实践目标**：看清 RASTER 从 kick 到发出覆盖 quad 波的完整控制流。
2. **操作步骤**：
   - 在 `VX_raster_core.sv` 里从 `launch_if`（L51）出发，依次跳到 `frame_kick`（L115）→ `mem_unit_start`（L123）→ `VX_raster_mem`（L134）→ `VX_raster_edge`（L175）→ slice（L330）→ `VX_raster_arb`（L448）→ `raster_bus_if`（L54）；
   - 找到 `engine_idle`（L471）与 `busy`（L512），解释「设备忙」如何依赖 raster 排空。
3. **需要观察的现象**：`armed_r` 在 kick 后立即置位、`started_r` 延迟到 `mem_unit_busy` 才置位——两者之间有一个 kick→mem-busy 的缝隙，`started_r` 正是来堵这个缝隙，防止 `busy` 短暂掉 0。
4. **预期结果**：能画出「kick → 加载 tile/图元 → 边函数 → slice 覆盖 → (early-Z) → quad 波出总线 → busy 排空」的时序。
5. 运行结果：待本地验证（源码阅读型实践）。

#### 4.2.6 小练习与答案

**练习 1**：如果 `NUM_INSTANCES > 1` 且某引擎本帧分到 0 个 tile，不加 `has_no_tiles` 保护会怎样？
> 参考答案：该引擎永远不置 `mem_unit_busy`，故 `started_r` 永不置 1，`armed_r`（busy）会一直卡高，整个帧排空等待永远不解除，挂死。`has_no_tiles` 让这种零工作引擎立即排空（L486、L502）。

**练习 2**：为什么 early-Z 的深度比较用 `cand > stored` 而不是直接用配置的深度函数？
> 参考答案：读到的 `stored` 可能已是本像素最终值或更近的值；用严格不等式（自反松弛）保证只有「证明被遮挡」的像素才被砍，可见像素永不被误杀，从而与不开 early-Z 的 ROP 路径像素一致。

---

### 4.3 TEX 纹理采样流水线

#### 4.3.1 概念说明

TEX 解决的问题是：FS 给定纹理坐标 (u,v) 与 LOD（细节层级），从纹理图（存放在设备内存、经 tcache 缓存）里取回对应的颜色。双线性插值需要取 2×2 共 4 个纹素（texel）并按小数部分加权，所以 TEX 的核心是一条「地址生成 → 取 4 纹素 → 格式解码 → 双线性插值」的定点流水线。

#### 4.3.2 核心流程

```
vx_tex4（FS 发出，经 SFU 路由到 TEX PE）
  → pipe_reg（输入弹性缓冲，锁存 mask/coords/lod + DCR 配置）
  → VX_tex_addr： → mip 地址（定点）
  → VX_tex_mem：经 tcache 取回 4 纹素
  → VX_tex_sampler / VX_tex_lerp：像素格式解码（7 种格式）+ 双线性
  → VX_tex_sat（饱和）
  → rsp_buf → tex_bus_if（返回 texel 给 FS）
```

DCR 里按纹理阶段（stage）配置每级的地址、logdim、格式、过滤模式、wrap 模式与 mip 偏移。`vx_tex4` 的 quad 形态会用 2×2 quad 的导数算一个整数 mip LOD。支持 7 种像素格式（A8R8G8B8、R5G6B5、A1R5G5B5、A4R4G4B4、A8L8、L8、A8）与三种寻址模式（CLAMP/REPEAT/MIRROR）。状态清单见 [docs/designs/graphics_hardware_stack.md:156-165](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L156-L165)。

#### 4.3.3 源码精读：VX_tex_core 三级

模块头与输入弹性缓冲（把总线请求与 DCR 配置合并锁存为一组寄存器，打一拍）：

[hw/rtl/tex/VX_tex_core.sv:48-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tex/VX_tex_core.sv#L48-L91) — tex_dcr + pipe_reg。

地址生成（`VX_tex_addr`：定点 (u,v,lod) → 4 个纹素的 mip 地址）：

[hw/rtl/tex/VX_tex_core.sv:105-137](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tex/VX_tex_core.sv#L105-L137) — tex_addr。

纹素取回（`VX_tex_mem`：经 tcache 取回 `NUM_LANES × 4` 个纹素，tag 里夹带格式与混合小数部分）：

[hw/rtl/tex/VX_tex_core.sv:146-172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tex/VX_tex_core.sv#L146-L172) — tex_mem。

采样器（`VX_tex_sampler`：像素格式解码 + 双线性插值，结果经 `rsp_buf` 返回总线）：

[hw/rtl/tex/VX_tex_core.sv:185-221](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tex/VX_tex_core.sv#L185-L221) — tex_sampler + rsp_buf。

#### 4.3.4 代码实践：数流水线级

1. **实践目标**：在 RTL 中确认 TEX 的「地址 → 取 → 采样」三级。
2. **操作步骤**：打开 `VX_tex_core.sv`，分别定位 `VX_tex_addr`、`VX_tex_mem`、`VX_tex_sampler` 三处实例化（L105/L146/L185），看清前一级的 `rsp_*` 如何接进下一级的 `req_*`。
3. **需要观察的现象**：三级之间靠 `valid/ready` 弹性握手串联，tag 在级间不断「夹带」更多信息（地址级夹格式，取回级夹混合小数）。
4. **预期结果**：画出 `(u,v,lod) → 地址 → 4 纹素 → 解码+插值 → texel` 的三级数据通路。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `VX_tex_mem` 要在 tag 里夹带「格式」与「混合小数部分（blends）」到下一级？
> 参考答案：纹素取回是异步的（经 tcache 可能多拍），格式与小数部分是后续采样器做解码与双线性插值所需的每请求私有数据；把它们随 tag 流水，避免在采样器侧再维护一张按请求查的状态表。

**练习 2**：`vx_tex4` 的 quad 形态相对 single 形态多了什么？
> 参考答案：quad 形态用 2×2 quad 的四个纹理坐标导数算出一个整数 mip LOD（取 `LZC`，前导零），用于硬件自动选层；single 形态直接用 FS 给定的 LOD 小数部分。

---

### 4.4 OM 输出合并：深度/模板/混合

#### 4.4.1 概念说明

OM（也叫 ROP）解决的问题是：FS 算出每个被覆盖像素的颜色与深度后，按渲染状态决定最终写进帧缓冲的值——做深度测试（depth test）、模板测试（stencil test）、颜色混合（blend）或逻辑操作（logic op）。因为要先读旧值（深度比较、混合的目标色）再写新值，OM 是一个**读改写（Read-Modify-Write, RMW）**单元。

#### 4.4.2 核心流程

```
vx_om4（FS 发出，经 SFU 路由到 OM PE，每 lane 提交一个子像素：color@base..base+3, depth@base+4..base+7）
  → VX_om_mem：经 ocache 读旧 color+depth（仅当需要时）→ RMW
  → VX_om_ds：深度测试（8 种 func）+ 模板测试/更新（8 种 op）
  → VX_om_blend：颜色混合（blend_func/minmax/multadd）或 VX_om_logic_op（ROP）
  → VX_om_mem：把新 color+depth 写回 ocache
```

关键细节：
- **同像素 RMW 互锁**：OM 持有一个槽直到其写提交，保证后到的同地址 fragment 的读不会越过在途的写。
- **OM 是权威 late-Z**：即便开了 early-Z，最终的深度测试仍由 OM 给出，early-Z 只做保守剔除。
- 读信用（`VX_CFG_OM_MEM_QUEUE_SIZE`）：每个被接纳的读都预留一个写回缓冲槽，确保响应消费永远不依赖 cache 侧请求进度，避免循环死锁。

状态清单见 [docs/designs/graphics_hardware_stack.md:167-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L167-L178)。

#### 4.4.3 源码精读：VX_om_core 三段 + RMW 互锁

模块头与 `busy`（OM 的 `vx_om4` 是 fire-and-forget，`busy` 是唯一能在 ROP 提交前拽住设备忙的信号）：

[hw/rtl/om/VX_om_core.sv:16-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv#L16-L41) — OM core 模块声明与 busy 注释。

`VX_om_mem`（经 ocache 做 color+depth 的 RMW）：

[hw/rtl/om/VX_om_core.sv:85-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv#L85-L119) — om_mem。

`VX_om_ds`（深度 + 模板测试/更新）与 `VX_om_blend`（混合）：

[hw/rtl/om/VX_om_core.sv:140-202](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv#L140-L202) — om_ds + om_blend。

读/写限定的互锁逻辑：读请求只有在没有 ds/blend 写在途时才允许发出；读信用计数器 `pending_reads` 把每个接纳的读预留到其写离开，缓冲深度按完整预留量设计：

[hw/rtl/om/VX_om_core.sv:282-344](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv#L282-L344) — 读写限定 + `pending_reads` 读信用。

`busy` 聚合所有在途 fragment 工作（排队请求、缓冲请求/写回、未完成读链、调度器内响应）：

[hw/rtl/om/VX_om_core.sv:370-371](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/om/VX_om_core.sv#L370-L371) — busy 表达式。

#### 4.4.4 代码实践：找读改写的三态

1. **实践目标**：理解 OM 如何根据渲染状态决定「只写」「先读再写」「直接写穿」。
2. **操作步骤**：在 `VX_om_core.sv` 阅读 L206-313 的限定逻辑，定位三个关键信号：`write_bypass`（无深度/模板/混合，直接写颜色）、`mem_readen`（需要先读旧值）、`ds_blend_read`（接纳一次读）。
3. **需要观察的现象**：当深度与混合都关、只写颜色时（`write_bypass`），OM 跳过读阶段直接写；当需要混合或深度比较时，必须先读旧 color/depth。
4. **预期结果**：列出「状态组合 → 是否需要读 → 写什么」的真值表。
5. 运行结果：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 OM 要做「同像素 RMW 互锁」？不加会怎样？
> 参考答案：两个 fragment 命中同一像素且都需 RMW 时，若后到的读越过前一个在途的写，它会读到旧值并把旧值当作混合目标色，导致混合/深度结果错误。互锁让同地址 fragment 串行化，保证读看到的是已提交的最新值。

**练习 2**：`vx_om4` 提交时，颜色和深度分别放在图形寄存器窗口的哪里？
> 参考答案：颜色放在 `base..base+3`，深度放在 `base+4..base+7`（每个被覆盖的子像素一份）；OM 从窗口读出后做深度/模板/混合再写回 ocache。

---

### 4.5 FF↔SIMT 接口：SFU 路由、gfx 寄存器窗口与 SimX 镜像

#### 4.5.1 概念说明

把上面三段串起来要回答一个问题：FS（跑在 SIMT 核上）怎么调 TEX/OM，RASTER 又怎么把 fragment 推给核？答案有两半：

- **TEX/OM 是 SFU 处理单元**：`vx_tex4`/`vx_om4` 经核的 SFU 分派器路由（回顾 u6-l4：SFU 是分派器而非单一执行单元）。`op_type` 决定路由到哪个 PE。
- **图形寄存器窗口（gfx register window）**：所有 FF↔SIMT 的操作数与结果不走散落 CSR，而是走一个 per-warp 的窗口。kernel 用 `vx_gfx_set`（SETW）暂存输入、发出 FF 操作、用 `vx_gfx_get*`（GETW）读结果；窗口寄存器是经记分板的，保证操作按序退休。RASTER 的 fragment 载荷则在 launch 时作为 FS warp 的窗口内容植入。

这条「接口律」是图形栈的核心约束：每个 FF↔SIMT 值都按作用域分区到窗口、单发射、经记分板寄存器交接（C1–C3 不变量），没有比操作生命期更长的共享可变边带（C4）。详见 [docs/designs/graphics_hardware_stack.md:99-111](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L99-L111)。

#### 4.5.2 核心流程：SFU PE 路由

SFU 内部的 PE 索引是**动态拼出来的**——前两个固定是 WCTL/CSRS，之后 DXA/TEX/OM/RASTER/GFXW 按「已使能的扩展数」累加偏移，这样关掉某个扩展时后续索引自动前移、不留空洞：

[hw/rtl/core/VX_sfu_unit.sv:83-94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_sfu_unit.sv#L83-L94) — PE_IDX_TEX / PE_IDX_OM / PE_IDX_RASTER 动态偏移。

路由逻辑按 `op_type` 选 PE：

[hw/rtl/core/VX_sfu_unit.sv:134-148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_sfu_unit.sv#L134-L148) — `INST_SFU_TEX`→PE_IDX_TEX，`INST_SFU_OM`→PE_IDX_OM。

注意 `PE_IDX_RASTER` 的处理：因为 RASTER 是 push 模型（没有 FS 发出的 raster 指令），它的 execute/result 口被**绑死**（ready=1、valid=0），仅占一个索引槽位以保持编号连续：

[hw/rtl/core/VX_sfu_unit.sv:334-339](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_sfu_unit.sv#L334-L339) — RASTER PE 口绑死。

#### 4.5.3 SimX 镜像与 model parity

SimX 把每个单元镜像为一个 `*Core`（TEX/OM 还多一层 `*Unit` SFU-PE），驱动真实的 `MemReq`/`MemRsp` 流量打 rcache/tcache/ocache，并复用 `sw/common/` 里的主机参考实现（`graphics::Rasterizer`、`graphics::DepthStencil`、`graphics::Blender`）。SimX 是图形栈的开发与评估引擎、正确性 oracle；RTL 的 FF 数据通路已基本建成（不是桩），SimX 仅在少数未建特性（TEX 三线性、OM MRT）上领先。见 [docs/designs/graphics_hardware_stack.md:253-292](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md#L253-L292)。

SimX 的 `RasterCore` 是一个 `SimObject`（回顾 u5-l1），暴露 rcache 的 MemReq/MemRsp channel 与每核的 raster 请求/响应 channel：

[sim/simx/raster/raster_core.h:30-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/raster/raster_core.h#L30-L61) — RasterCore 类与 channel 接口。

生产者 FSM 注释（`LOAD_TILES → LOAD_PIDS → LOAD_PRIMS → RASTERIZE → READY`）与 early-Z 的严格在后方剔除实现（与 RTL `VX_om_compare` 逐位一致）：

[sim/simx/raster/raster_core.cpp:103-108](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/raster/raster_core.cpp#L103-L108) — 生产者 FSM。

[sim/simx/raster/raster_core.cpp:87-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/raster/raster_core.cpp#L87-L95) — `earlyz_occluded` 的严格在后方判定，与 §4.2.4 的 RTL 语义对应。

这正是 model parity（u7-l4）在图形子系统上的落点：SimX 的 early-Z 比较与 RTL 逐位对齐，`graphics_parity` 矩阵验证两者字节级一致。

#### 4.5.4 代码实践：两侧对照找模块

1. **实践目标**：在 RTL 与 SimX 两侧分别定位 raster/tex/om 的对应模块，验证「逐模块对应」。
2. **操作步骤**：
   - RTL 侧：`hw/rtl/raster/VX_raster_core.sv`、`hw/rtl/tex/VX_tex_core.sv`、`hw/rtl/om/VX_om_core.sv`；
   - SimX 侧：`sim/simx/raster/raster_core.cpp`、`sim/simx/tex/tex_core.cpp`、`sim/simx/om/om_core.cpp`；
   - 各写一句话：输入是什么、输出是什么。
3. **需要观察的现象**：两侧的 cache 端口（rcache/tcache/ocache）都走 MemReq/MemRsp（SimX）或 `VX_mem_bus_if`（RTL），符合 u5-l3 的基数规则——模块只通过 channel/总线通信。
4. **预期结果**：得到一张「单元 | RTL 模块 | SimX 模块 | 输入 | 输出」五列对照表（这就是本讲综合实践的核心产物）。
5. 运行结果：待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `PE_IDX_RASTER` 的 execute/result 口被绑死？
> 参考答案：v2 采用 push 模型，FS 不发射 raster 指令——raster 引擎自己启动 FS，没有「着色器发出的光栅化指令」需要 SFU 路由。保留索引槽位仅为让 TEX/OM 之后的编号连续，实际口子不接任何单元。

**练习 2**：图形寄存器窗口为什么要经记分板？
> 参考答案：TEX/OM 操作异步完成、响应可能乱序；窗口寄存器经记分板后，操作按序退休（C1–C3 不变量），FS 读结果时能保证它已就绪，且没有比操作生命期更长的共享可变边带（C4）。

---

## 5. 综合实践：画一张贯穿三段的「流水线→模块→输入输出」对照表

把本讲全部内容串起来，完成下面这张表（这就是本讲规格里要求的实践任务）：

| 段 | RTL 模块（核心文件） | SimX 模块 | 输入 | 输出 | 专属 cache |
|----|------|------|------|------|------|
| RASTER | `hw/rtl/raster/VX_raster_core.sv`（+ te/be/slice/edge/qe/earlyz；核侧 packer/dispatch） | `sim/simx/raster/raster_core.cpp` | draw launch（frame_kick）+ tile/图元缓冲（经 rcache） | 覆盖 quad 波（`raster_bus_if`，pos/mask/pid），并触发核侧启动 fragment CTA | rcache（+ early-Z 读 ocache） |
| TEX | `hw/rtl/tex/VX_tex_core.sv`（addr/mem/sampler） | `sim/simx/tex/tex_core.cpp` | FS 的 `vx_tex4`：纹理坐标 (u,v,lod) | 采样后的 texel 颜色 | tcache |
| OM | `hw/rtl/om/VX_om_core.sv`（mem/ds/blend） | `sim/simx/om/om_core.cpp` | FS 的 `vx_om4`：每子像素 color+depth | 经深度/模板/混合后写回的帧缓冲值 | ocache（可写、写穿） |

**操作步骤**：

1. 先读 `docs/designs/graphics_hardware_stack.md` §1 与 §3，建立三段全景；
2. 对每段，分别打开上表里的 RTL 与 SimX 模块，确认入口（请求总线/方法）与出口（响应总线/方法）；
3. 用一句话写出每段的输入与输出（如 RASTER 输入 = frame_kick + tile/图元，输出 = 覆盖 quad 波）；
4. 标注三段各自的 cache，并解释为什么只有 ocache 可写；
5. 进阶：在 `tests/regression/gfx_pipeline_kernel/` 里找到一个真实图形测试，对照它的 kernel.cpp 找出 FS 用到的 `vx_tex4`/`vx_om4` 调用，回溯它们如何经 SFU 进入本讲的三段硬件。

**预期结果**：你能脱稿讲出「FS 发 `vx_tex4` → SFU 路由到 TEX PE → tcache 取纹素 → 双线性 → 返回」与「RASTER 覆盖 quad → push 启动 FS → FS 发 `vx_om4` → OM 经 ocache RMW → 写回帧缓冲」这两条完整链路。

运行结果：待本地验证（若本地已 `configure` 并装好工具链，可用 `./ci/blackbox.sh --driver=simx --app=gfx_pipeline_kernel` 尝试跑通该图形测试以观察输出）。

## 6. 本讲小结

- Vortex 的图形 FF 硬件分三段：**RASTER**（光栅化，产生覆盖 quad 并 push 启动 FS）、**TEX**（纹理采样）、**OM**（输出合并/ROP），三者 cluster 共享、全定点数据通路。
- 三段都是 `custom1` 槽位的 RISC-V ISA 扩展，靠 `funct3` 区分（`vx_om4`=2、`vx_tex4`=5、`SETW/GETW`=6），各被 `VX_CFG_EXT_{TEX,RASTER,OM}_ENABLE` 门控。
- `VX_graphics.sv` 是 cluster 封装，实例化每段的仲裁器 + core + cache（tcache/rcache/ocache），扇出 DCR；ocache 是唯一可写、且与 early-Z 共享相干读口的 cache。
- RASTER 是 **push 模型**：KMU 委托的 draw launch 发 `frame_kick`，raster 自驱动行走并主动在核上启动 fragment CTA；TEX/OM 则是 SFU 处理单元，由 FS 经 `vx_tex4`/`vx_om4` 调用。
- early-Z 用「严格在后方」判定做遮挡剔除，与不开 early-Z 的 ROP 路径像素一致；OM 始终是权威 late-Z。
- SimX 与 RTL 逐模块对应、early-Z 逐位对齐，是 `graphics_parity` 的物理基础，承接 u7-l4 的 model parity 主线。

## 7. 下一步学习建议

- 想了解这条 FF 硬件之上的**软件/编译/渲染管线**（vortexpipe Gallium 驱动、设备前端 setup + bin-sort、NIR→Vortex lowering、`vkCmdDraw` 流程），请读下一讲 **u10-l2 图形软件栈与软件发射器** 与设计文档 `graphics_software_stack.md`、`vortexpipe_architecture.md`。
- 想了解 RASTER 推 fragment 时用到的 KMU/CTA 派发机制，可回顾 **u11-l3 命令处理器与 KMU**；想了解 FS 读取窗口载荷的 CTA CSR 模型，可回顾 **u4-l2 SIMT 控制指令**。
- 若对硬件光线追踪（RTU）感兴趣，可继续 **u10-l3 硬件光线追踪单元**——RTU 与本讲的 TEX 共享 SFU 分派框架，但解决的是 BVH 遍历与相交测试问题。
