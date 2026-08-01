# 项目定位与硬件架构总览

## 1. 本讲目标

本讲是整个《Ventus GPGPU（Verilog 版）学习手册》的第一篇。读完本讲后，你应当能够：

- 说清楚 **ventus-gpgpu-verilog 是什么**：它的指令集特征（RISC-V 向量扩展 RV32V）、它与 Chisel 原版「乘影」的关系，以及它参考了哪些开源设计。
- 对照架构框图，认出 **GPGPU_top、SM 核、CTA scheduler、L2Cache、AXI 接口** 这几个主要部件，并理解它们在数据通路里的相对位置。
- 读懂 `define.v` 里 **NUM_SM、NUM_WARP、NUM_THREAD** 这三个最关键的规模参数，并理解它们为什么是整个项目「牵一发而动全身」的总开关。
- 在脑海中建立一张「主机请求 → CTA 调度 → SM 核 → L2 → AXI/外部存储」的顶层数据流向图，为后续逐层深入打好地基。

本讲只讲「是什么」和「在哪里」，不展开任何模块的内部实现细节——那是后续讲义的任务。

## 2. 前置知识

在开始之前，建议你大致了解以下几个概念。看不懂没关系，本讲会用通俗的话再解释一遍。

| 概念 | 一句话解释 |
|------|-----------|
| **GPGPU** | 通用计算 GPU，即用 GPU 的并行计算能力来跑通用程序（而不只是画图）。 |
| **SIMT** | 单指令多线程，一条指令同时驱动一组线程（NVIDIA 叫它 warp，本项目也叫 warp）。 |
| **warp / wavefront** | GPU 调度的最小线程组。Ventus 里一个 warp 包含 `NUM_THREAD` 个线程。 |
| **SM / CU** | Streaming Multiprocessor（流式多处理器），GPU 里真正跑指令的核心，本项目源码里也叫 CU（Compute Unit）。 |
| **CTA / workgroup** | Cooperative Thread Array，一组协同的线程，由若干 warp 组成。主机一次下发一个 CTA（也叫 workgroup）。 |
| **Cache（L1/L2）** | 缓存。L1 在 SM 内部（私有、快），L2 在片上被所有 SM 共享。 |
| **AXI / TileLink** | 两种片上总线协议。AXI 是工业标准（ARM 提出），TileLink 来自 RISC-V 生态。Ventus 对外用 AXI，内部用 TileLink 风格接口。 |
| **Verilog / Chisel** | 两种硬件描述语言。Verilog 是传统语言；Chisel 是基于 Scala、能生成 Verilog 的高级语言。 |

**RISC-V 向量扩展（RV32V）** 是本项目的灵魂：传统 CPU 的向量指令在「一个寄存器里装多个数据」上做并行，而 GPU 的 SIMT 是在「一组线程各跑同一条指令」上做并行。Ventus 把这两者结合——用 RISC-V 的向量指令语义来驱动 GPU 的 SIMT 硬件，所以你会在源码里看到大量 `VADD_VV`、`VLE32_V` 这类向量指令（下划线后缀表示操作数形式，`VV` = 向量-向量，`VX` = 向量-标量）。

## 3. 本讲源码地图

本讲只读最顶层的几个「地标」文件，目的是建立全局观，不深入任何子模块。

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md) | 项目说明：定位、综合指标、运行方式、致谢（参考的开源设计）。 |
| [src/gpgpu_top/GPGPU_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v) | **顶层模块**：把主机接口、CTA 调度器、若干 SM、L2、对外 AXI 接口全部连起来。 |
| [src/gpgpu_top/cta_top/cta_interface.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v) | CTA 调度的对外接口封装（`cta_top` 目录是调度子系统）。 |
| [src/gpgpu_top/sm/sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v) | **SM 核的顶层包装**：一个 SM 的全部内容（CTA→warp 拆分、L1、流水线）。 |
| [src/gpgpu_top/l2cache/Scheduler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v) | **L2 缓存顶层**（基于 SiFive block-inclusivecache）。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | **全局配置参数**：所有规模、位宽、指令宏都在这里。本讲的「总开关」。 |

> 完整的硬件架构框图（本讲的「地图」）放在仓库 `docs/images/` 下：
> - 顶层层级图：[`ventus_verilog_arch1.png`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/docs/images/ventus_verilog_arch1.png)
> - SM 核内部图：[`ventus_verilog_arch2.png`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/docs/images/ventus_verilog_arch2.png)

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**GPGPU_top（顶层）、CTA scheduler（调度器）、sm_wrapper（SM 核）、L2Cache（二级缓存）**。每个模块都对应架构图里的一块「大积木」。

### 4.1 项目定位：什么是 Ventus GPGPU（乘影）

#### 4.1.1 概念说明

先回答最基本的问题：**这个项目到底是什么？**

`ventus-gpgpu-verilog` 是开源 GPGPU 项目「**乘影**」的 **Verilog 版本**。它支持 **RISC-V 向量扩展（RV32V）**，用 Verilog 编写。它的「亲哥哥」是清华大学 THU-DSP-LAB 用 Chisel 写的原版 [`ventus-gpgpu`](https://github.com/THU-DSP-LAB/ventus-gpgpu)，本仓库就是把它翻译/重写成 Verilog。

一句话定位：**一个用 RISC-V 向量指令驱动、SIMT 执行、可综合、可仿真的开源 GPGPU 处理器 RTL**。

#### 4.1.2 核心流程

从「用户视角」看，一次 GPU 计算的全生命周期是：

```
主机(CPU)  --下发一个 workgroup(CTA)-->  GPGPU
                                          │
                            CTA scheduler 调度，分配到某个 SM
                                          │
                            SM 把 workgroup 拆成若干 warp
                                          │
                            SM 流水线逐条执行 warp 内指令
                            (需要取指/译码/取操作数/执行/访存/写回)
                                          │
                            数据缺失时访问 L1 → L2 → 外部存储(AXI)
                                          │
                            所有 warp 完成 --> 上报主机
```

这条链路就是本讲要在顶层文件里「指认」出来的主干。

#### 4.1.3 源码精读

项目定位与来源写在 README 最开头：

[README.md:3-13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L3-L13) —— 这段说明了项目名、它支持 RISC-V-V 扩展、它是「乘影」的 Verilog 版，并给出了 Chisel 原版与项目网站的链接。

README 还给出了 **DC 综合指标**，这是衡量这个 RTL「能跑多快、多大」的硬数据：

[README.md:25-33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L25-L33) —— 关键配置是 `NUM_THREAD=32、NUM_SM=2、NUM_WARP=8、DCACHE_BLOCKWORDS=2`，在 tsmc 28nm 工艺下达到 **620MHz、总面积 3.908mm²**。

最后，README 的致谢表列出了项目复用/参考了哪些开源设计——这些信息能帮你理解为什么某些子模块「长得像别家的东西」：

[README.md:203-214](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L203-L214) —— 例如 CTA scheduler 基于 MIAOW、L2Cache 基于 SiFive block-inclusivecache、FPU 参考香山、SFU 基于 pulp-platform。

#### 4.1.4 代码实践

**实践目标**：亲手确认项目的「身份」与「血统」。

**操作步骤**：
1. 用编辑器打开 `README.md` 第 3–13 行，找到「乘影」原版（Chisel）的 GitHub 链接和项目网站 `opengpgpu.org.cn`。
2. 打开 `README.md` 第 203–214 行的致谢表。
3. 打开 [`src/define/define.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)，确认它最上方第 1 行有一行被注释掉的 `` `define T28_MEM ``——这是后面综合章节会提到的 SRAM 宏开关。

**需要观察的现象**：致谢表里 5 个子模块分别对应 5 个外部开源项目。

**预期结果**：你能填出下表（答案见 4.1.5）：

| 本项目子模块 | 参考来源 |
|------------|---------|
| CTA scheduler | ? |
| L2Cache | ? |
| FPU | ? |
| SFU | ? |

#### 4.1.5 小练习与答案

**练习 1**：Ventus GPGPU 支持什么指令集？为什么说它「既是 CPU 又是 GPU」？

> **答**：支持 RISC-V 标量指令 + RV32V 向量扩展。「像 CPU」是因为它用 RISC-V 指令集、有标准的 CSR、寄存器堆；「像 GPU」是因为它在 SIMT 硬件上把一条向量指令广播给一整个 warp 的线程并行执行，并具备 CTA/warp 调度、共享内存、张量核等 GPU 特性。

**练习 2**：`ventus-gpgpu-verilog` 和 `ventus-gpgpu` 是什么关系？

> **答**：前者是「乘影」的 **Verilog 版本**，后者是用 **Chisel** 写的原版；Verilog 版在功能和架构上对标原版，但 RTL 用手写 Verilog 实现。

---

### 4.2 顶层系统 GPGPU_top：从主机到核的数据通路

#### 4.2.1 概念说明

`GPGPU_top` 是整个处理器的「最外层壳子」。它不实现具体计算逻辑，而是把四大部件——**主机接口、CTA 调度器、若干个 SM、L2 缓存**——用线连起来，并把对外接口（可选 AXI）引到芯片边界。

对应到架构图（`ventus_verilog_arch1.png`），最外层蓝色边框「Ventus GPGPU」就是这个模块，它内部从上到下依次是：Host → CTA Scheduler → Clusters（每个含若干 SM）→ Cluster_to_L2_Arb → L2 Cache → 对外 Global Memory。

#### 4.2.2 核心流程

`GPGPU_top` 内部的连线可以抽象成「控制流」和「数据流」两条主线：

```
【控制/调度流】
host_req_* ──> cta_interface ──> cta2warp_*  ──> 各 sm_wrapper 的 cta_req_*
                                                   (按 NUM_CLUSTER×NUM_SM_IN_CLUSTER 例化)
【完成回收】
各 warp 完成 ──> warp2cta_* ──> cta_interface ──> wg_done
wg_done 触发 is_flushing ──> 等 l2cache_finish_issue ──> host_rsp_valid_o 回报主机

【数据/访存流】（不带 NO_CACHE 模式）
各 sm_wrapper mem_req ──> sm2cluster_arb(汇聚)
                          ──> l2_distribute(按地址分发)
                          ──> cluster_to_l2_arb(请求端仲裁)
                          ──> Scheduler(L2)
                          ──> out_a / out_d (对外 AXI 风格接口)
```

> 提示：`GPGPU_top` 里有个 `` `ifdef NO_CACHE `` 宏（默认注释掉，即不开）。打开它时，模块对外暴露的是直连 cache 的原始接口；不打开时（默认），对外是经 L2 的 TileLink 风格 A/D 通道，再由更外层的 `gpgpu_axi_top` 转成 AXI。本讲只看默认（带 L2）这一路。

#### 4.2.3 源码精读

**(1) 主机接口**——主机通过一组 `host_req_*` 信号下发一个 workgroup，关键信息包括 workgroup id、warp 数量、warp 大小、起始 PC、各类资源（VGPR/SGPR/LDS）需求量等：

[GPGPU_top.v:26-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L26-L45) —— `host_req_valid_i/ready_o` 握手，以及 `host_req_wg_id_i`、`host_req_num_wf_i`（warp 数）、`host_req_start_pc_i`（起始 PC）、`host_req_vgpr_size_per_wf_i`（每 warp 的 VGPR 需求）等字段；`host_rsp_valid_o` 用于向主机回报「这个 workgroup 做完了」。

**(2) 对外接口的两副面孔**——由 `` `ifdef NO_CACHE `` 选择「直连 cache 接口」还是「带 L2 的 A/D 通道」：

[GPGPU_top.v:77-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L77-L95) —— 这是默认（带 L2）的对外通道：`out_a_*` 是发往外部的请求（Address 通道），`out_d_*` 是外部返回的响应（Data 通道），数量都是 `` `NUM_L2CACHE `` 路。

**(3) CTA 调度器例化**——把主机请求接进调度子系统：

[GPGPU_top.v:273-319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L273-L319) —— `cta_interface` 模块（即 4.3 节的主角）。注意 `host2cta_*` 输入直接来自顶层 `host_req_*`，输出 `cta2warp_*` 去往各 SM。

**(4) SM 核的批量例化**——用两层 `generate` 循环按 `NUM_CLUSTER × NUM_SM_IN_CLUSTER` 例化 `sm_wrapper`：

[GPGPU_top.v:322-400](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L322-L400) —— 每个 `sm_wrapper` 的 `cta_req_*` 接 `cta2warp_*` 的对应片段（用位切片 `[index+1)*WIDTH-1-:WIDTH]` 取出第 i 个 SM 的字段），`cta_rsp_*` 接 `warp2cta_*`。

**(5) 完成与刷新逻辑**——workgroup 全部完成、缓存刷干净后才回报主机：

[GPGPU_top.v:249-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L249-L270) —— `wg_done`（来自 `cta_interface` 的 `cta2host_valid_o`）置起 `is_flushing`，等 L2 给出 `l2cache_finish_issue`（表示刷操作完成）后，`host_rsp_valid_o = l2cache_finish_issue && is_flushing` 才真正回报主机完成。

**(6) L2 + 互联三件套**——`Scheduler`（L2 本体）、`cluster_to_l2_arb`、`sm2cluster_arb`、`l2_distribute` 构成多 SM 到 L2 的互联：

- [GPGPU_top.v:406-443](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L406-L443) —— `Scheduler l2cache`（L2 顶层）。
- [GPGPU_top.v:487-521](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L487-L521) —— `sm2cluster_arb`：把一个 cluster 内各 SM 的请求汇聚成一路 cluster 级流。
- [GPGPU_top.v:523-558](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L523-L558) —— `l2_distribute`：把 cluster 请求分发到各路 L2。
- [GPGPU_top.v:565-583](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L565-L583) —— 把 L2 的 `out_a/out_d` 直接连到顶层对外引脚。

#### 4.2.4 代码实践

**实践目标**：在顶层文件里「走一遍」从主机到 SM 的控制路径。

**操作步骤**：
1. 打开 [`GPGPU_top.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v)，定位第 27 行的 `host_req_valid_i`。
2. 跟到第 277 行，确认它接进 `cta_interface` 的 `host2cta_valid_i`。
3. 再看第 299 行，`cta_interface` 输出的 `cta2warp_valid_o` 接到内部线网 `cta2warp_valid`。
4. 跳到第 330 行，确认 `cta2warp_valid[i*NUM_SM_IN_CLUSTER+p]` 又接进第 `i*NUM_SM_IN_CLUSTER+p` 个 `sm_wrapper` 的 `cta_req_valid_i`。

**需要观察的现象**：一条 `valid` 信号从顶层端口，经过 `cta_interface`，被「扇出」到每一个 SM。

**预期结果**：你能画出一根线 `host_req_valid_i → host2cta_valid_i → cta2warp_valid[i] → sm_wrapper.cta_req_valid_i`。

#### 4.2.5 小练习与答案

**练习 1**：默认配置（`NUM_CLUSTER=1, NUM_SM=2`）下，`GPGPU_top` 里一共例化了几个 `sm_wrapper`？

> **答**：2 个。循环是 `NUM_CLUSTER(1) × NUM_SM_IN_CLUSTER(=NUM_SM/NUM_CLUSTER=2)` = 2。

**练习 2**：`host_rsp_valid_o` 为什么不能直接等于 `wg_done`？

> **答**：因为 `wg_done` 只表示「所有 warp 执行完了」，但缓存里可能还有未回写的数据。必须等 `l2cache_finish_issue`（L2 把刷新操作发完）后，才能向主机确认完成，避免数据丢失。这正是 [L249-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L249-L270) 里 `is_flushing` 状态机的作用。

---

### 4.3 CTA scheduler：任务调度入口

#### 4.3.1 概念说明

CTA scheduler 负责「派活」：主机一次丢过来一个 workgroup（CTA），调度器要判断哪个 SM 有足够资源（寄存器、共享内存、warp 槽位），然后把这个 workgroup 分配给那个 SM，等它跑完再回收。

本项目的调度器**基于开源项目 MIAOW** 的 ultra-threads dispatcher（见 README 致谢）。在源码里，整个调度子系统位于 `src/gpgpu_top/cta_top/`，对外的「门面」是 `cta_interface.v`，真正的调度大脑在 `cta/` 子目录（`cta_scheduler.v`、`resource_table.v`、`cu_handler.v` 等）。

#### 4.3.2 核心流程

```
host2cta 请求进入
    │
    v
cta_interface（接口封装/握手）
    │
    v
cta_scheduler（核心调度，查资源表 resource_table）
    │  判定：哪个 CU(SM) 的 VGPR/SGPR/LDS/wf_slot 够用？
    v
allocator（分配资源）/ cu_handler（组装派发字段）
    │  组装：start_pc, vgpr/sgpr base, wf_tag, wf_count ...
    v
cta2warp（输出给 SM：一个 workgroup 的派发信息）
    │
    v
SM 执行完毕 ──> warp2cta（wf_tag done）──> inflight_wg_buffer 回收 ──> cta2host 完成
```

关键层次术语（GPU 通用概念，Ventus 沿用）：
- **workgroup / block / CTA**：同义词，主机下发的任务单元。
- **warp / wavefront (wf)**：CTA 内部再细分的调度执行单元，一个 CTA 含若干 warp。
- **CU（Compute Unit）**：执行 CTA 的硬件，在本项目里就是 SM。

#### 4.3.3 源码精读

**调度子系统的目录结构**（用 `ls` 即可看到）：

[`src/gpgpu_top/cta_top/`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v) 下包含：
- `cta_interface.v`——对外门面，被 `GPGPU_top` 例化（见 [GPGPU_top.v:273-319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L273-L319)）。
- `cta/cta_scheduler.v`——调度核心。
- `cta/resource_table.v`（及 `resource_table_group.v`、`top_resource_table.v`）——资源表，记录每个 CU 的剩余资源。
- `cta/allocator_neo.v`——资源分配。
- `cta/cu_handler.v`、`cta/inflight_wg_buffer.v`、`cta/dis_controller.v`——派发握手与在途 workgroup 跟踪。
- `wf_done_interface_single.v`——warp 完成信号回收。

**顶层如何接入调度器**——`cta_interface` 的端口就是「主机侧 ↔ SM 侧」的全部信号：

[GPGPU_top.v:273-319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L273-L319) —— 注意它的三类端口：`host2cta_*`（接主机）、`cta2warp_*`（派发到 SM，含 `start_pc`、`wg_id`、`wf_count`、`vgpr/sgpr/lds base`、`wf_tag` 等）、`warp2cta_*`（SM 回报完成，`wf_tag_done`）。

#### 4.3.4 代码实践

**实践目标**：从顶层连线确认「调度器输入什么、输出什么」。

**操作步骤**：
1. 在 `GPGPU_top.v` 第 277–292 行，列出 `cta_interface` 的 `host2cta_*` 输入都来自哪些顶层 `host_req_*`。
2. 在第 299–314 行，列出它输出的 `cta2warp_dispatch2cu_*` 字段有哪些（提示：`wg_wf_count`、`wf_size`、`sgpr_base`、`vgpr_base`、`wf_tag`、`start_pc`、`pds_baseaddr`、`wg_id` 等）。

**需要观察的现象**：派发字段里既有「程序从哪开始执行」（`start_pc`），也有「资源基址」（`vgpr_base`/`sgpr_base`/`lds_base`）和「身份标签」（`wf_tag`、`wg_id`）。

**预期结果**：你能用一句话概括——调度器把一个 workgroup 的「程序入口 + 资源位置 + 身份」打包发给某个 SM。

#### 4.3.5 小练习与答案

**练习 1**：在 GPU 术语里，CTA、workgroup、block 是什么关系？warp 和 wavefront 呢？

> **答**：CTA（Cooperative Thread Array）、workgroup、block 三者指同一个东西——主机下发、内部线程可协同（如共享内存通信）的任务单元。warp（NVIDIA 用语）和 wavefront（AMD/MIAOW 用语）也指同一个东西——CTA 内部更小的、硬件实际并行调度的线程组。本项目源码里 `wf` 即 wavefront = warp。

**练习 2**：为什么调度器需要 `resource_table`？

> **答**：一个 workgroup 要占用 SM 的 VGPR/SGPR/LDS/warp 槽位，只有当某个 SM 的剩余资源「装得下」时，才能派发过去。`resource_table` 就是用来记录每个 CU 当前剩余资源、据此判定能否派发的数据结构。

---

### 4.4 SM 核 sm_wrapper 与 L2Cache：执行与存储

#### 4.4.1 概念说明

**SM 核（`sm_wrapper`）** 是真正执行指令的地方。一个 SM 内部是一条完整的流水线：取指（icache）→ 译码 → 发射（带记分板）→ 操作数采集 → 各类执行单元（vALU/vFPU/SFU/LSU/CSR/张量核/SIMT 栈）→ 写回，外加 L1 数据缓存（dcache）和共享内存（shared memory）。对应架构图 `ventus_verilog_arch2.png`，分成 Decode / OPC / Issue / Execution / Writeback 五大阶段。

**L2Cache（`Scheduler.v`）** 是所有 SM 共享的二级缓存，向上通过互联网络接收各 SM 的 L1 缺失请求，向下经对外接口连到外部存储。它**基于 SiFive 的 block-inclusivecache**（包含式目录结构）。

#### 4.4.2 核心流程

**SM 核内部**（对应 `ventus_verilog_arch2.png`，数据从左到右）：

```
取指 Instruction Cache ──> Decode ──> [Warp Scheduler 选 warp]
   │
   v
OPC 阶段: Ibuffer(暂存) ──> Ibuffer2Issue ──> Operand Collector(取操作数) + Scoreboard(冒险检测)
   │
   v
Issue(发射) ──> Execution 阶段执行单元:
                vALU / sALU / vFPU / CSR / SFU / LSU / Tensor core / SIMT-stack
                (旁挂 Shared Memory + Data Cache)
   │
   v
Writeback(写回寄存器堆)
```

`sm_wrapper.v` 本身就是把上面这些部件连起来的「SM 顶层」，它在源码里例化了三个核心子模块：`cta2warp`（把 CTA 拆成 warp）、`pipe`（流水线主体）、`l1cache_arb`（L1 仲裁）。

**存储层次**：

```
SM 内部:  寄存器堆(VGPR/SGPR) ─最快─> L1(icache/dcache) ─> Shared Memory(LDS)
SM 对外:  L1 缺失 ──> l1cache_arb ──> 互联(sm2cluster_arb/l2_distribute/cluster_to_l2_arb) ──> L2(Scheduler) ──> 外部 AXI 存储器
```

#### 4.4.3 源码精读

**SM 核的对外接口**——一个 SM 看到的世界就是「调度器给的 cta_req」+「对外的 mem 请求」：

[sm_wrapper.v:22-46](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L22-L46) —— `cta_req_*`（接调度器的派发）、`cta_rsp_*`（回报 warp 完成）、`cache_invalid_i`（缓存无效控制），以及 `mem_req_*`/`mem_rsp_*`（对外访存）。

**SM 核内部例化的三大子模块**（用 `grep` 可定位行号）：
- [sm_wrapper.v:292](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L292) —— `cta2warp`：CTA → warp 拆分。
- [sm_wrapper.v:315](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L315) —— `pipe`：流水线主体（取指→译码→发射→执行→写回）。
- [sm_wrapper.v:430](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L430) —— `l1cache_arb`：L1（icache/dcache/shared）请求仲裁后统一对外。

**SM 核的目录构成**（`src/gpgpu_top/sm/`）：`cta2warp.v`、`sm_wrapper.v`、`l1cache_arb.v`，以及 `pipeline/`（流水线全部子模块：`pipe.v`、`decodeUnit.v`、`issue.v`、`scoreboard.v`、`valu/`、`vmul/`、`fpu/`、`sfu_v2/`、`lsu/`、`csr/`、`simt_stack/`、`tensor/`、`operand_collector/`、`ibuffer/`、`writeback.v`）和 `l1cache/`（`icache/`、`dcache/`、`shared_memory/`）。

**L2 缓存**——`Scheduler` 是 L2 顶层，目录 `src/gpgpu_top/l2cache/` 下能看到典型 inclusivecache 的部件：

[`src/gpgpu_top/l2cache/`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v) —— 包含 `Scheduler.v`（顶层）、`directory_test.v`（包含式目录）、`banked_store.v`（分体存储）、`MSHR.v`（未命中状态保持寄存器）、`Listbuffer.v`、`sinkA.v`/`sourceD.v`/`SourceA.v`/`sinkD.v`（TileLink 各通道处理）。它被 `GPGPU_top` 在 [GPGPU_top.v:406-443](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L406-L443) 例化，并输出 `finish_issue_o`（刷新完成信号，参与 4.2 节的 `host_rsp_valid_o` 产生）。

#### 4.4.4 代码实践

**实践目标**：把架构图（`ventus_verilog_arch2.png`）的每个方框映射到真实源码目录。

**操作步骤**：
1. 打开 SM 架构图 `docs/images/ventus_verilog_arch2.png`，记下 Execution 阶段里的方框：`SIMT-stack / vALU / sALU / vFPU / CSR / SFU / LSU / Tensor core`，以及 `Shared Memory / Data Cache / Instruction Cache`。
2. 打开目录 `src/gpgpu_top/sm/pipeline/` 和 `src/gpgpu_top/sm/l1cache/`。
3. 给每个方框找到对应的源码目录：`simt_stack/`、`valu/`（vALU）、`fpu/`（vFPU）、`csr/`（CSR）、`sfu_v2/`（SFU）、`lsu/`（LSU）、`tensor/`（Tensor core）、`l1cache/shared_memory/`（Shared Memory）、`l1cache/dcache/`（Data Cache）、`l1cache/icache/`（Instruction Cache）。

**需要观察的现象**：架构图上每个紫色「Function Unit」方框，都能在 `pipeline/` 下找到一个同名（或近义）目录。

**预期结果**：你能画出一张「方框 ↔ 目录」对照表（部分项：vALU↔`valu/`、SFU↔`sfu_v2/`、Tensor core↔`tensor/`、Shared Memory↔`l1cache/shared_memory/`）。注：标量 ALU（sALU）在本项目里与 CSR/分支等控制类执行单元合并在流水线中，不像 vALU 那样有独立大目录，这部分留待后续讲义确认。

#### 4.4.5 小练习与答案

**练习 1**：SM 核里，取指令的缓存叫什么？取数据的缓存叫什么？它们之间还有什么存储？

> **答**：取指令用 **Instruction Cache（icache）**，取数据用 **Data Cache（dcache）**。此外 SM 内还有 **Shared Memory（共享内存/LDS）**，用于同一 workgroup 内线程的低延迟通信。三者都在 `src/gpgpu_top/sm/l1cache/` 下，由 `l1cache_arb.v` 仲裁后统一对外。

**练习 2**：L2Cache 模块为什么取名叫 `Scheduler`，里面为什么有 `MSHR`？

> **答**：SiFive block-inclusivecache 用 `Scheduler` 作顶层，因为它要「调度」多个 TileLink 通道（sinkA/sourceD 等）的并发事务。`MSHR`（Miss Status Holding Register）用于记录「缓存未命中」的请求，等下级数据返回后再唤醒等待者，从而支持多个缺失并发处理而不阻塞流水线。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「顶层鸟瞰」任务（这是本讲的主实践）：

**任务**：阅读 README 与两张架构框图，**手绘一张从主机请求到 SM 核、再到 AXI/L2 的顶层数据流向草图**，并标注三个规模参数的含义。

**要求在图上画出并标注**：
1. 控制/调度路径：`Host → CTA scheduler → sm_wrapper(×NUM_SM)`，标出派发关键字段（`start_pc`、`wg_id`、`vgpr_base` 等）。
2. 数据/访存路径：`sm_wrapper → sm2cluster_arb → l2_distribute → cluster_to_l2_arb → Scheduler(L2) → out_a/out_d(对外)`。
3. 完成回收路径：`warp 完成 → warp2cta → wg_done → is_flushing → l2cache_finish_issue → host_rsp_valid_o`。
4. 在图旁用一句话解释 **NUM_SM / NUM_WARP / NUM_THREAD** 各是什么：
   - **NUM_SM**：GPU 里 SM 核的数量（源码默认 2，见 [define.v:5](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L5)）。
   - **NUM_WARP**：一个 SM 能同时持有的 warp 数量（默认 8，见 [define.v:9](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L9)）。
   - **NUM_THREAD**：一个 warp 内的线程数（默认 4，见 [define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11)），它等于 lane 数 `NUM_LANE`，并派生出 `NUM_SFU = NUM_THREAD/2`（[define.v:13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L13)、[define.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37)）。

**进阶思考**：README 综合指标用的是 `NUM_THREAD=32`，而 `define.v` 默认是 4——这说明什么？

> **提示**：`define.v` 是仿真默认的小配置（省仿真时间），综合时会把 `NUM_THREAD` 改大（如 32）以测真实性能。这正是为什么 README 在「开始」一节特意提醒：**仿真前必须在 `define.v` 修改 `NUM_THREAD`**（见 [README.md:37-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L37-L45)）。

## 6. 本讲小结

- **ventus-gpgpu-verilog** 是「乘影」GPGPU 的 Verilog 版，支持 RISC-V 向量扩展（RV32V），Chisel 原版在 `THU-DSP-LAB/ventus-gpgpu`。
- 顶层 `GPGPU_top.v` 把 **主机接口、CTA 调度器、若干 SM、L2 缓存** 连成整体，对外通过 `out_a/out_d` 接到外部（再转 AXI）。
- **CTA scheduler**（基于 MIAOW）负责把主机的 workgroup 按资源条件分配到某个 SM；**SM 核**（`sm_wrapper`→`pipe`）是执行指令的流水线，内部含 icache/dcache/shared memory 和各类执行单元。
- **L2Cache**（基于 SiFive block-inclusivecache，顶层 `Scheduler.v`）是所有 SM 共享的二级缓存，用 MSHR 处理缺失。
- 三个规模参数 **NUM_SM（核数）/NUM_WARP（每核 warp 数）/NUM_THREAD（每 warp 线程数）** 是全项目的总开关，定义在 `define.v`；仿真前必须确认 `NUM_THREAD`。
- 顶层数据流分两线：**控制流**（host_req→CTA→SM→host_rsp）和**数据流**（SM→互联→L2→外部）。

## 7. 下一步学习建议

本讲只建立了「鸟瞰图」。接下来建议：

1. **先看目录与配置**：下一讲 [u1-l2 源码目录结构与模块组织] 会带你走遍 `src/` 各子目录，[u1-l3 核心配置参数 define.v] 会逐组讲解 `define.v` 的全部参数。
2. **跑一个仿真**：[u1-l4 仿真环境搭建与用例运行] 教你用 VCS 跑通 `tc_gaussian`，亲眼看到 `PASSED`。
3. **精读顶层**：[u1-l5 顶层模块 GPGPU_top 与系统数据流] 会比本讲更细地剖析 `GPGPU_top.v` 的每一段连线。
4. 如果你想直接进入某个部件：调度看单元 2，SM 流水线看单元 3-5，存储看单元 6-7。但建议先完成单元 1，打好全局基础再深入。
