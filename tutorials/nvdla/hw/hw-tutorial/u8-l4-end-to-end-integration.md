# 端到端：编程一个网络层与集成指南

## 1. 本讲目标

本讲是整本手册的收尾篇。前面 35 篇我们把 NVDLA 拆成了 16+ 个引擎，逐个看过它们的源码：配置总线、卷积五级流水、存储接口、后处理四件套、时钟复位、FIFO/RAM/FPU 原语、验证参考模型、配置与综合流程。本讲要回答最后、也是最重要的问题：

> **这些引擎如何被「串起来」跑完一个真实的网络层？要把这颗 IP 嵌进一颗 SoC，需要接哪些线、注意什么？**

读完本讲，你应当能够：

- 用一段清晰的「编程序列」描述一个卷积 + 后处理层从数据预装、配置、启动到中断完成的完整生命周期。
- 解释为什么各引擎的 `OP_ENABLE` 要按「逆流水线」顺序写、为什么 CDMA 必须最后点火。
- 说清 producer/consumer 影偶（shadow）配置与各引擎 `done` 中断如何协作，实现「不停顿接跑下一层」。
- 列出 NVDLA 对外的全部接口（CSB/APB、两组 AXI memif、时钟/复位/电源），并知道集成时要连什么、留什么。

---

## 2. 前置知识

本讲不再讲新机制，而是把已学的拼起来。请确认你已理解以下概念（对应前置讲义）：

- **CSB 配置总线与地址译码**（u2-l1、u2-l2）：CPU 通过 CSB 写各引擎寄存器；`csb_master` 按 4 KB 对齐的地址把请求分发到各引擎。
- **影偶（shadow）寄存器与 producer/consumer**（u2-l3）：每个引擎的操作参数放 `dual_reg`，被例化两份（group 0/group 1）轮换；`POINTER` 的 bit0=producer（CPU 写哪组）、bit16=consumer（引擎用哪组）。
- **GLB 中断聚合**（u2-l4）：8 类引擎 × 2 影偶组 = 16 个 `done` 源，经 `mask/set/status` 三寄存器聚合成单根 `core_intr`，算式 `core_intr = OR(~mask & status)`。
- **卷积主流水线 CDMA→CBUF→CSC→CMAC→CACC**（单元 3），其中 **CDMA 是数据流的源头（生产者）**、**CACC 是卷积段的终点并交付 SDP**（u3-l1、u3-l6）。
- **后处理 SDP/PDP/CDP**（单元 5）：SDP 紧接 CACC 做逐点后处理，支持 `flying` 模式直接吃 CACC 的输出。
- **BDMA 桥 DMA**（u4-l4）：在 DBB（片外主存）与 CVSRAM（片上）间批量搬数据，描述符式编程、双组启动、完成后报 `done` 中断。
- **存储接口 MCIF/CVIF**（u4-l1）：每个引擎的 DMA 用 `ram_type` 一比特选择走 MCIF（DBB）还是 CVIF（CVSRAM）。
- **顶层与分区**（u1-l5）：`NV_nvdla` 顶层把所有引擎装进 6 个分区实例；`partition_o` 是中央枢纽，集中了 csb_master/glb/mcif/cvif/bdma/rubik/cdp/pdp。

> 一句话复习：**CPU 写寄存器配置 → 引擎点火（OP_ENABLE） → 数据沿流水线流动 → 引擎完成报 done → GLB 聚合成中断 → CPU 清状态、准备下一层。** 本讲就是把这五步细化成可执行的代码。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vmod/nvdla/top/NV_nvdla.v` | 顶层。定义全部对外端口（CSB、两组 AXI memif、中断、时钟/复位/电源），并例化 6 个分区，把 `partition_o` 的 `core_intr` 接到顶层 `dla_intr`。**集成时看这里。** |
| `vmod/nvdla/glb/NV_NVDLA_glb.v` | 中断聚合台。把 8 类引擎的 `done_intr_pd[1:0]` 收拢，经 `u_ic` 算出 `core_intr`。**理解中断协作看这里。** |
| `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v` | 中央配置路由器。用地址掩码把 CSB 请求分发到各引擎，给出每个引擎的寄存器基址。**查寄存器基址看这里。** |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v` | BDMA 寄存器文件。给出搬运描述符的全部寄存器名与偏移（源/目的地址、行大小、stride、OP_ENABLE、LAUNCH0/1）。**编 BDMA 搬运序列看这里。** |
| `verif/traces/traceplayer/sanity3/input.txn` | 一段**真实的** INT16 卷积 + SDP（ReLU/缩放）后处理 trace。它就是「编程一个网络层」的活样板，本讲反复对照它。 |
| `perf/NVDLA_OpenSource_Performance.xlsx` | 官方性能/算力评估表，用于核对端到端吞吐与算力规格。 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块：

1. **端到端编程序列**——把一个网络层拆成「预装数据 → 配置 → 逆流水线点火 → 轮询中断」四段。
2. **影偶配置与 done 中断协作**——为什么能不停顿接跑下一层、中断如何精确反映「哪一组哪一层完成」。
3. **SoC 集成要点**——把 IP 嵌进芯片要接的线与时序/电源约束。

---

### 4.1 端到端编程序列

#### 4.1.1 概念说明

「编程一个网络层」本质上是**用一连串 CSB 寄存器写，把每个引擎调成想要的算子，再依次点火，让数据自动沿流水线流过**。CPU 不搬运像素、不做卷积，只做两件事：**写配置** 和 **写 OP_ENABLE（点火）**。

一个最小的「卷积 + SDP 后处理」层，其生命周期是：

```
(0) 预装数据：把权重/特征图放到引擎能读到的存储里（DBB 或 CVSRAM）
(1) 配置每个引擎的操作参数（地址、尺寸、stride、精度、算子模式）
(2) 按逆流水线顺序写 OP_ENABLE：SDP → CACC → CMAC_B → CMAC_A → CSC → CDMA
(3) 等待 / 轮询 GLB 的 done 中断
(4) 清中断状态，准备下一层
```

为什么是「逆流水线」？因为流水线**下游必须先就绪**，上游产出的数据才不会无处可去。若先点 CDMA（源头），它立刻开始向 CBUF 喂数据，可此时 CSC/CMAC/CACC 还没点火，数据会堆在缓冲里甚至被丢弃。反过来：先把 CACC、CMAC、CSC、最后 CDMA 点着，数据一旦从 CDMA 流出，每一级都已在「待命」状态接住它，整条流水线**一次点亮、无空泡**。SDP 作为 CACC 的下游更要先就绪，所以它在最前面。

> 这正是 u7-l2 提到的规律。下面用真实 trace 印证。

#### 4.1.2 核心流程

以仓库自带的 `sanity3`（一个 8×8、32 通道输入、16 个 3×3 卷积核的 INT16 卷积，接一个 ReLU+缩放的 SDP）为例，其编程序列可提炼为下图：

```
[预装] load_mem  DBB@0x80000000 ← sample_surf(特征图)
        load_mem  DBB@0x80100000 ← weight(权重)

[配置 CDMA]  D_MISC_CFG / DATAIN_SIZE / DAIN_ADDR_LOW(=0x80000000)
             WEIGHT_ADDR_LOW(=0x80100000) / LINE_STRIDE / SURF_STRIDE ...
[配置 CSC]   D_MISC_CFG / DATAIN_SIZE_EXT / WEIGHT_SIZE_EXT / ENTRY_PER_SLICE ...
[配置 CMAC_A/B] D_MISC_CFG(精度)
[配置 CACC]  D_MISC_CFG(精度) /_clip
[配置 SDP]   DP_BS_CFG(ReLU) / DP_BS_MUL(缩放) / FEATURE_MODE_CFG(flying) / DST_BASE_ADDR ...

[点火·逆流水线]
   write  SDP.OP_ENABLE      = 1   ← 下游最先
   write  SDP_RDMA.OP_ENABLE = 0   ← flying 模式下关闭输入 RDMA
   write  CACC.OP_ENABLE     = 1
   write  CMAC_A.OP_ENABLE   = 1
   write  CMAC_B.OP_ENABLE   = 1
   write  CSC.OP_ENABLE      = 1
   write  CDMA.OP_ENABLE     = 1   ← 源头最后，全流水点亮

[等待]      wait  (等 dla_intr 拉高)
[清中断]    write  GLB.S_INTR_STATUS = 0xffffffff   (W1C 清 done)
[校验]      dump_mem DBB@0x80400000 → golden_output_memory.dat
```

注意 SDP 这一行：`FEATURE_MODE_CFG` 设成 `flying=ON`，意味着 SDP **不经过自己的输入 RDMA**，而是直接吃 CACC 的输出（u5-l1）。所以序列里把 `SDP_RDMA.OP_ENABLE` 写成 0 关掉它。最终结果由 SDP 的 WDMA 写回 DBB 的 `0x80400000`，再用 `dump_mem` 取出与黄金参考比对。

#### 4.1.3 源码精读

**(a) 寄存器基址——来自 csb_master 的地址译码。** 写哪个引擎的寄存器，由地址高位决定。`csb_master` 把 CSB 的 16 位**字地址**左移两位得到字节地址，再用 `addr_mask` 取高位做相等比较：

字地址到字节地址的转换（[vmod/nvdla/csb_master/NV_NVDLA_csb_master.v:596-608](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L596-L608)），这段代码先取出请求里的字地址 `core_req_addr`，再拼成字节地址 `core_byte_addr` 供后续译码。

各引擎的译码命中条件（字节基址）：

- BDMA：[NV_NVDLA_csb_master.v:1422](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1422) → `0x4000`
- CDMA：[NV_NVDLA_csb_master.v:1292](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1292) → `0x5000`
- CSC：[NV_NVDLA_csb_master.v:772](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L772) → `0x6000`
- CMAC_A：[NV_NVDLA_csb_master.v:642](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L642) → `0x7000`
- CACC：[NV_NVDLA_csb_master.v:967](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L967) → `0x9000`
- SDP：[NV_NVDLA_csb_master.v:1357](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1357) → `0xb000`
- GLB：[NV_NVDLA_csb_master.v:1032](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L1032) → `0x0000`

把字节基址除以 4 就得到 trace 里看到的**字地址基址**，整理成下表（即编程时实际填进 CSB addr 的值）：

| 引擎 | 字节基址 | 字地址基址（trace 用） | OP_ENABLE 字地址（sanity3 实测） |
| --- | --- | --- | --- |
| GLB | 0x0000 | 0x0000 | —（中断寄存器 S_INTR_STATUS = 0x0003） |
| MCIF | 0x2000 | 0x0800 | — |
| CVIF | 0x3000 | 0x0c00 | — |
| BDMA | 0x4000 | 0x1000 | — |
| CDMA | 0x5000 | 0x1400 | 0x1404（offset 4） |
| CSC | 0x6000 | 0x1800 | 0x1802（offset 2） |
| CMAC_A | 0x7000 | 0x1c00 | 0x1c02 |
| CMAC_B | 0x8000 | 0x2000 | 0x2002 |
| CACC | 0x9000 | 0x2400 | 0x2402 |
| SDP_RDMA | 0xa000 | 0x2800 | 0x2802 |
| SDP | 0xb000 | 0x2c00 | 0x2c0e（offset 0xe） |
| PDP_RDMA | 0xc000 | 0x3000 | — |
| PDP | 0xd000 | 0x3400 | — |
| CDP_RDMA | 0xe000 | 0x3800 | — |
| CDP | 0xf000 | 0x3c00 | — |
| Rubik | 0x10000 | 0x4000 | — |

> 提示：不同引擎的 `OP_ENABLE` 在各自寄存器页里的偏移并不统一（CSC/CMAC/CACC 多在 offset 2，CDMA 在 offset 4，SDP 在 offset 0xe），编程时要以该引擎的 `_reg.v` 为准，不能想当然。

**(b) 预装数据——sanity3 用 testbench 的 load_mem。** trace 开头把特征图与权重直接灌进 DBB 存储模型：[verif/traces/traceplayer/sanity3/input.txn:3-4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L3-L4)。注意这是**测试平台命令**（`load_mem` 由 sequencer 直接写 slave 存储数组，见 u7-l1），并非 BDMA 搬运。在真实芯片里，这步等价于「CPU/主控把权重和特征图写到 DDR 的对应地址」；若想让数据落到片上 CVSRAM 以降低延迟，就用 **BDMA** 搬运（见 4.1.4 综合实践）。

**(c) 配置写——以 CDMA 为例。** trace 里逐个写 CDMA 的操作参数并读回校验，例如精度/模式 [input.txn:9-10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L9-L10)、输入特征图基址（指向 DBB 的 0x80000000）[input.txn:23-24](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L23-L24)、权重基址（指向 0x80100000）[input.txn:53-54](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L53-L54)。配置阶段写完一条就 `read_reg` 回读比对，确保寄存器真的吃进去了——这是 trace-player 的稳妥写法。

**(d) 逆流水线点火——整段最关键。** [input.txn:211-224](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L211-L224) 一口气按 SDP→SDP_RDMA(disable)→CACC→CMAC_A→CMAC_B→CSC→CDMA 的顺序写 OP_ENABLE，CDMA（0x1404）压轴。读 4.1.2 的图对照即可。

**(e) 等待 + 清中断。** [input.txn:225-235](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L225-L235)：`wait` 等中断到来，再写 `GLB.S_INTR_STATUS`（字地址 0x0003，即字节 0xc，W1C）为 `0xffffffff` 清掉 done 位，反复几轮。最后 [input.txn:237](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L237) 用 `dump_mem` 把 SDP 写回的结果取到 0x80400000 与黄金参考比对。

#### 4.1.4 代码实践

**实践 A：源码阅读——追踪 sanity3 的逆流水线点火。**

1. 实践目标：亲眼确认「下游先点火、CDMA 最后点火」这条规律，并能把每个 `OP_ENABLE` 的地址对应到上表的引擎基址。
2. 操作步骤：
   - 打开 `verif/traces/traceplayer/sanity3/input.txn`，跳到第 211–224 行。
   - 逐行把每个 `write_reg 0xffffXXXX 0x1` 的低 16 位地址 `XXXX` 拆成「基址 + offset」，对照上面的字地址基址表，填出它点的是哪个引擎。
   - 数一下从 SDP 到 CDMA 的点火顺序，验证它是否严格**逆**于数据流方向 CDMA→…→CACC→SDP。
3. 需要观察的现象：SDP 在最前、CDMA 在最后；中间 SDP_RDMA 被写成 `0x0`（flying 模式关闭）。
4. 预期结果：点火顺序为 SDP → SDP_RDMA(关) → CACC → CMAC_A → CMAC_B → CSC → CDMA，与 4.1.2 图一致。
5. 命令：`sed -n '211,224p' verif/traces/traceplayer/sanity3/input.txn`（只看这段即可）。

**实践 B：伪代码——用 BDMA 预装权重 + 完整启动。**

> 说明：sanity3 本身用 `load_mem` 预装、权重放在 DBB。下面这段是**示例代码**，演示一个更接近真实芯片的流程：先用 BDMA 把权重从 DBB 搬到片上 CVSRAM，再让 CDMA 从 CVSRAM 读权重（低延迟）。寄存器名与偏移取自真实的 `NV_NVDLA_BDMA_reg.v`（见 4.2.3）。

```c
// ===== 示例代码：端到端编程一个「卷积 + SDP(ReLU/缩放)」层 =====
// 假定：权重已在 DBB @0x8010_0000；特征图已在 DBB @0x8000_0000。
// 目标：用 BDMA 把权重搬到 CVSRAM @0x5000_0000，再配置并启动整条流水。

// ---- 第 0 步：BDMA 预装权重 DBB → CVSRAM（描述符式编程）----
// BDMA 寄存器页字基址 0x1000（字节 0x4000）。下列为字节偏移（已含基址）。
csb_write(0x4000, 0x80100000); // CFG_SRC_ADDR_LOW   源=权重 DBB 地址
csb_write(0x4004, 0x00);       // CFG_SRC_ADDR_HIGH
csb_write(0x4008, 0x50000000); // CFG_DST_ADDR_LOW   目的=CVSRAM 地址
csb_write(0x400c, 0x00);       // CFG_DST_ADDR_HIGH
csb_write(0x4010, WEIGHT_32B_UNITS); // CFG_LINE  行大小（32B 为单位）
csb_write(0x4014, 0x1);        // CFG_CMD  src_ram_type=1(MCIF/DBB)
                               //          dst_ram_type=0(CVIF/CVSRAM)  ← 以源码译码为准
csb_write(0x4018, NUM_LINES);  // CFG_LINE_REPEAT
csb_write(0x4030, 0x1);        // CFG_OP(en)  快照入队
csb_write(0x4034, 0x1);        // CFG_LAUNCH0  启动 group 0
// 等 BDMA done 中断（GLB done_source 的 BDMA 位），再继续。

// ---- 第 1 步：配置各引擎（此处只给关键寄存器，省略读回校验）----
csb_write(0x5005, 0x11001100); // CDMA.D_MISC_CFG  INT16, direct
csb_write(0x500d, 0x80000000); // CDMA.D_DAIN_ADDR_LOW  特征图在 DBB
csb_write(0x501f, 0x50000000); // CDMA.D_WEIGHT_ADDR_LOW 权重现在在 CVSRAM
csb_write(0x501d, 0x0);        // CDMA.D_WEIGHT_RAM_TYPE = CVIF（走 CVSRAM）
csb_write(0x6003, 0x11001100); // CSC.D_MISC_CFG
csb_write(0x7001, ...);        // CMAC_A.D_MISC_CFG
csb_write(0x8001, ...);        // CMAC_B.D_MISC_CFG
csb_write(0x9001, ...);        // CACC.D_MISC_CFG
csb_write(0xb016, 0x1a);       // SDP.D_DP_BS_CFG  ReLU 开
csb_write(0xb02c, 0x1);        // SDP.D_FEATURE_MODE_CFG  flying=ON

// ---- 第 2 步：逆流水线点火 ----
csb_write(0xb00e, 0x1);        // SDP.OP_ENABLE      下游先
csb_write(0xa002, 0x0);        // SDP_RDMA.OP_ENABLE 关（flying）
csb_write(0x9002, 0x1);        // CACC.OP_ENABLE
csb_write(0x7002, 0x1);        // CMAC_A.OP_ENABLE
csb_write(0x8002, 0x1);        // CMAC_B.OP_ENABLE
csb_write(0x6002, 0x1);        // CSC.OP_ENABLE
csb_write(0x5004, 0x1);        // CDMA.OP_ENABLE     源头最后，全流水点亮

// ---- 第 3 步：轮询 GLB done 中断 ----
while ((csb_read(0x0003) & EXPECTED_DONE_MASK) == 0) { /* 等 CACC/SDP done */ }
csb_write(0x0003, 0xffffffff); // 清 S_INTR_STATUS（W1C），准备下一层
```

练习时关注三点：① BDMA 的 `CFG_CMD` 里 `src/dst_ram_type` 决定各端走 MCIF 还是 CVIF（值以源码译码为准，参见 u4-l4）；② `CFG_LINE` 以 32 字节为单位；③ 点火顺序必须逆流水线。

5. 预期结果：若接好存储模型并跑通，最终 SDP 的 WDMA 会把结果写到指定 DBB 地址；若仅静态阅读，则能说清每行配置对应哪个引擎的哪一项行为。**实际运行结果待本地验证**（需先按 u1-l4 跑通仿真环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果把上例的点火顺序反过来——先写 CDMA.OP_ENABLE、最后才写 SDP——会发生什么？
**答**：CDMA 会立刻开始向 CBUF/CSC 喂数据，但下游 CSC/CMAC/CACC/SDP 尚未点火、不在待命态，数据要么堆积、要么被丢弃，卷积结果错误甚至触发缓冲断言。正确做法是下游先就绪、CDMA（源头）最后点火。

**练习 2**：sanity3 里 SDP_RDMA.OP_ENABLE 被写成 0，为什么？
**答**：因为 `SDP.D_FEATURE_MODE_CFG` 设了 `flying=ON`，SDP 直接吃 CACC 的输出、不读存储（u5-l1），所以它的输入 RDMA 必须关闭，否则会多此一举甚至抢占带宽。

**练习 3**：BDMA 的 `CFG_LINE`（0x4010）寄存器，写值 `N` 表示搬多少字节？
**答**：以 **32 字节为单位**（一个 atom），所以实际搬运字节数 = `N × 32`（参见 u4-l4 的 line/cube 搬运模型）。

---

### 4.2 影偶配置与 done 中断协作

#### 4.2.1 概念说明

只跑一层不稀奇，NVDLA 真正的设计目标是**跑完一层立刻无缝接跑下一层，中间不空泡**。这靠两套机制配合：

- **影偶（shadow）配置**：每个引擎的操作参数寄存器有两份（group 0 / group 1）。CPU 给「下一层」写参数时，写的是**另一组**（producer 组），不影响引擎**当前正在用的这一组**（consumer 组）。
- **done 中断**：引擎跑完当前层时拉 `done`，同时**翻转 consumer 指针**，无缝切到刚写好的新一组参数。

两者结合，CPU 可以在引擎跑第 N 层的同时，把第 N+1 层参数写进空闲的那组；第 N 层一完成，引擎自动切组继续跑第 N+1 层，CPU 只是收到一个中断而已。这就是「双缓冲不停顿」。

#### 4.2.2 核心流程

影偶 + 中断的协作时序：

```
时刻 t0: 引擎在跑 group0（consumer=0）。CPU 把第 N+1 层参数写入 group1（producer 翻到 1）。
时刻 t1: 引擎跑完 group0 的最后一拍 → 拉起 done_intr_pd[0]（第 0 组完成脉冲）。
时刻 t2: regfile 收到 done → consumer 翻成 1、清 group1 的 op_en、输出选通切到 group1。
时刻 t3: 引擎无缝开始跑 group1（第 N+1 层）。CPU 侧：GLB 的 done_status[0] 置位 → core_intr 拉高。
时刻 t4: CPU 响应中断，读 GLB.S_INTR_STATUS 看是哪一组完成，写 1 清掉（W1C），再给 group0 写第 N+2 层。
```

关键点：`done_intr_pd` 是 **2 位**，两位分别对应两个影偶组——所以中断不仅告诉你「哪个引擎完成」，还告诉你「是第几组完成」，CPU 据此知道该为哪一组准备下一层。

GLB 这一侧把 8 类引擎的 2 位脉冲拼成 16 位 `done_source`：

```
done_source[15:0] = {cacc[1:0], cdma_wt[1:0], cdma_dat[1:0], rubik[1:0],
                     bdma[1:0], pdp[1:0], cdp[1:0], sdp[1:0]}   // 位序见 4.2.3
core_intr = OR( ~mask[15:0] & status[15:0] )
```

`mask`（写 1 屏蔽）、`set`（软件写 1 置位）、`status`（W1C 清除）三者按这 16 位一一对应（详见 u2-l4）。

#### 4.2.3 源码精读

**(a) 8 类引擎的 done 脉冲进 GLB。** [vmod/nvdla/glb/NV_NVDLA_glb.v:59-73](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L59-L73) 声明了 `sdp2glb_done_intr_pd[1:0]`、`cdp2glb...`、`pdp2glb...`、`bdma2glb...`、`rubik2glb...`、`cdma_wt2glb...`、`cdma_dat2glb...`、`cacc2glb...` 共 8 个 2 位输入——每个引擎两根线，对应两个影偶组。

**(b) 中断控制器 u_ic 算出 core_intr。** [NV_NVDLA_glb.v:190-239](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L190-L239) 例化 `NV_NVDLA_GLB_ic u_ic`，把上述 8 个 `done_intr_pd` 连进去，输出 `core_intr`（[L232](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L232)），并回写各引擎的 `done_status`/`done_mask`。`u_csb`（[L125-168](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_glb.v#L125-L168)）则是 CPU 访问这套中断寄存器的 CSB 接口适配。

**(c) core_intr 接到顶层 dla_intr。** [vmod/nvdla/top/NV_nvdla.v:1165](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1165) 例化 `u_partition_o`，第 [1309](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1309) 行把 partition_o 内 GLB 算出的 `core_intr` 直接连到顶层唯一的中断输出 `dla_intr`（端口声明见 [NV_nvdla.v:79](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L79) 与 [L170](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L170)）。也就是说，整颗 IP 对外只拉一根中断线，CPU 收到后去读 GLB 的 `S_INTR_STATUS` 分辨是哪个引擎、哪一组完成。

**(d) BDMA 的双组启动寄存器——影偶在搬运引擎上的体现。** [vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v:148-168](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v#L148-L168) 列出 BDMA 全部寄存器的写使能与偏移：`CFG_OP`(0x4030)、`CFG_LAUNCH0`(0x4034)、`CFG_LAUNCH1`(0x4038) 正是「描述符快照入队 + 双组启动」的入口（参见 u4-l4）；`STATUS`(0x4040) 只读地回报 `free_slot/grp0_busy/grp1_busy/idle`（[NV_NVDLA_BDMA_reg.v:401-407](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v#L401-L407)），让 CPU 知道哪个组在忙、还能塞几条命令。BDMA 的 done 同样以 `bdma2glb_done_intr_pd[1:0]` 双脉冲上报（对应 GLB 的 BDMA 两位）。

#### 4.2.4 代码实践

1. 实践目标：在真实 trace 与源码里，看清「done 是 2 位、对应两个影偶组」这件事。
2. 操作步骤：
   - 在 `NV_NVDLA_glb.v` 第 190–239 行，数一数 `u_ic` 收了多少个 `*_done_intr_pd[1:0]` 输入，确认每个都是 2 位。
   - 打开任一引擎的 `*_dual_reg.v`（如 `vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v`），找到 `POINTER` 寄存器，确认它有 producer（bit0）与 consumer（bit16）两个字段。
   - 思考：当 `bdma2glb_done_intr_pd = 2'b10` 时，是 group0 还是 group1 完成？
3. 需要观察的现象：每个引擎贡献 2 位 done；POINTER 的 consumer 是只读镜像、producer 是 CPU 写开关。
4. 预期结果：8 类引擎 × 2 位 = 16 个中断源；`2'b10` 表示 group1（第 1 组）完成（位序约定见 u2-l4 的 `done_source` 拼接）。
5. 若想跑起来验证 done 翻转，可在仿真中给某引擎的 done 中断加一句 `$display`，**实际运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `done_intr_pd` 要做成 2 位，而不是 1 位？
**答**：因为有两套影偶组在轮换。1 位只能告诉你「引擎完成了一次」，2 位还能告诉你「是哪一组完成的」，CPU 据此精确地为空闲组准备下一层，避免覆盖正在运行的那组。

**练习 2**：CPU 在引擎跑第 N 层时写第 N+1 层参数，为什么不会打乱正在运行的层？
**答**：因为写的是 producer 组（另一组），引擎当前用的是 consumer 组，两者物理上是两份寄存器。写保护机制还会拒绝向「已点火」的组写入（u2-l3），双重保险。

**练习 3**：`core_intr = OR(~mask & status)` 里，若 CPU 想暂时屏蔽 SDP 的中断但保留其它，怎么做？
**答**：把 `S_INTR_MASK` 里 SDP 对应的那一位置 1（写 1 屏蔽），其余保持 0。注意复位时 mask 全 0，即默认放行所有中断。

---

### 4.3 SoC 集成要点

#### 4.3.1 概念说明

NVDLA 是一颗 **IP**，不是成品芯片。把它嵌进 SoC，本质是**把顶层 `NV_nvdla` 的端口接到 SoC 的相应基础设施上**：配置总线接 CPU、两组 AXI 接内存控制器/片上 SRAM、时钟复位接时钟树与复位控制器、电源接电源域、中断接中断控制器。

集成的难点不在「接哪根线」（端口很清楚），而在三点：

1. **配置入口选型**：顶层给的是原生 CSB 口；若 SoC 的 CPU 走 APB，需要在外部或片内加一个 apb2csb 桥（u2-l1）。
2. **存储接口的 AXI 适配**：两组 AXI memif（DBB / CVSRAM）要接到 SoC 的 AXI 互联，处理好 id、outstanding、地址映射与回压。
3. **跨时钟域与电源域**：IP 内有 core/csb(falcon) 两个时钟域（u6-l1），对外暴露两个时钟输入与相应复位；SRAM 还有电源岛控制信号。

#### 4.3.2 核心流程

集成连接清单（对照顶层端口）：

```
配置口    : csb2nvdla_*/nvdla2csb_*  ← CPU（直连 CSB，或经外部 APB→CSB 桥）
存储口 DBB : nvdla_core2dbb_* (AW/W/B/AR/R 五通道) ← AXI 互联 → DDR 控制器
存储口 CV  : nvdla_core2cvsram_* (五通道)          ← 片上 CVSRAM
中断      : dla_intr                                      ← SoC 中断控制器 (GIC 等)
时钟      : dla_core_clk（计算/存储）、dla_csb_clk（配置）  ← 时钟树
复位      : dla_reset_rstn、direct_reset_                ← 复位控制器
电源/测试 : nvdla_pwrbus_ram_*_pd（按分区）、test_mode、global_clk_ovr_on、tmc2slcg_disable_clock_gating
```

复位与时钟的关键事实：顶层 `dla_reset_rstn` 经 partition_o 内的 `core_reset` 产生内部 `nvdla_core_rstn`（[NV_nvdla.v:323](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L323) 声明的内部 wire），再回灌所有分区（参见 u6-l1 的「异步复位、同步释放」与先 core 后 falcon 的上电顺序）。集成功必读的综合约束见 u8-l3（SDC 把 `nvdla_core_clk` 约到 0.9 ns ≈ 1.11 GHz，并把复位/测试网设为 ideal/false path）。

#### 4.3.3 源码精读

**(a) 顶层全部对外端口。** [vmod/nvdla/top/NV_nvdla.v:16-86](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L16-L86) 是模块端口表，逐类对应集成清单：

- 时钟/复位/测试：[L17-23](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L17-L23)（`dla_core_clk`、`dla_csb_clk`、`global_clk_ovr_on`、`tmc2slcg_disable_clock_gating`、`dla_reset_rstn`、`direct_reset_`、`test_mode`）。
- CSB 配置口：[L24-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L24-L32)（`csb2nvdla_valid/ready/addr[15:0]/wdat[31:0]/write/nposted` 与 `nvdla2csb_valid/data/wr_complete`）—— 16 位字地址、32 位数据。
- DBB AXI 五通道：[L33-55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L33-L55)（AW/W/B/AR/R，512 位数据、64 位地址、8 位 id、4 位 len）。
- CVSRAM AXI 五通道：[L56-78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L56-L78)（结构同 DBB）。
- 中断与按分区电源：[L79-86](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L79-L86)（`dla_intr` 与 `nvdla_pwrbus_ram_{c,ma,mb,p,o,a}_pd`，分别对应各分区的 SRAM 电源岛）。

**(b) 两组 AXI memif 实际由 partition_o 驱出。** 在 [NV_nvdla.v:1262-1305](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1262-L1305) 可见，MCIF 的 AXI（`mcif2noc_axi_*`）连到顶层 `nvdla_core2dbb_*`，CVIF 的 AXI（`cvif2noc_axi_*`）连到 `nvdla_core2cvsram_*`——集成者只需把这两组对外 AXI 接到 SoC 互联，IP 内部 MCIF/CVIF 的 IG→cq→eg 三级（u4-l2/u4-l3）自动处理多引擎仲裁与回压。

> 提醒：端口注释里的星号（如 `nvdla_pwrbus_ram_ma_pd //|< i *`）标记 CMAC 两半（ma/mb）的电源岛——集成时要为每个分区 SRAM 提供正确的电源域信号。

#### 4.3.4 代码实践

1. 实践目标：不看本讲，仅凭顶层端口表，画出 NVDLA 与 SoC 的连接框图。
2. 操作步骤：
   - 打开 `vmod/nvdla/top/NV_nvdla.v` 第 16–86 行端口表。
   - 按「配置 / 存储(DBB) / 存储(CV) / 中断 / 时钟 / 复位 / 电源」七类把端口归组。
   - 给每一类在 SoC 侧找到对应设施（如 DBB→AXI 互联→DDR 控制器；dla_intr→GIC；dla_core_clk→PLL/时钟树）。
3. 需要观察的现象：CSB 口是 16 位字地址（覆盖 256 KB 配置空间，正好容下全部引擎寄存器页）；两组 AXI 都是标准五通道、512 位数据。
4. 预期结果：得到一张「NV_nvdla ↔ SoC」连接图，能指出若 SoC CPU 走 APB，需要在 `csb2nvdla_*` 前外加 apb2csb 桥。
5. 这是源码阅读型实践，无需运行命令；端口语义若拿不准，对照 u1-l5 与 u2-l1。

#### 4.3.5 小练习与答案

**练习 1**：顶层 CSB 地址只有 16 位，够覆盖所有引擎寄存器吗？
**答**：够。16 位是**字地址**，对应 2¹⁶ × 4 B = 256 KB 配置空间；16 个引擎各占一个 4 KB 页（见 4.1.3 表），加起来远小于 256 KB。

**练习 2**：DBB 和 CVSRAM 两组 AXI，数据/地址/id 位宽是否相同？
**答**：相同，都是 512 位数据、64 位地址、8 位 id、4 位 len（[NV_nvdla.v:33-78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L33-L78)）。差异只在终点：DBB 接片外 DDR，CVSRAM 接片上 SRAM。

**练习 3**：为什么顶层有 `dla_core_clk` 与 `dla_csb_clk` 两个时钟，而不是一个？
**答**：因为 IP 内部把高吞吐的计算/存储（core 域）与慢速的寄存器配置（csb/falcon 域）分成两个时钟域（u6-l1），跨域用同步器/异步 FIFO。两个时钟让集成者可以给计算域高频、给配置域低频，各自优化功耗与时序。

---

## 5. 综合实践

把本讲三个模块串成一个端到端任务：**为一层「卷积 + ReLU + 缩放」编写完整的启动 trace，并写出它的 SoC 集成连接说明。**

**任务背景**：8×8 输入特征图、32 通道、INT16；16 个 3×3 卷积核；卷积后做 ReLU 与 ×1 缩放；权重希望放在片上 CVSRAM 以降延迟。

**要求**：

1. **数据预装**：写一段 BDMA 描述符配置（用 4.1.4 B 中的真实寄存器偏移），把权重从 DBB `0x80100000` 搬到 CVSRAM `0x50000000`，并说明搬运完成如何经 `bdma2glb_done_intr_pd` 报到 GLB。
2. **引擎配置**：列出 CDMA/CSC/CMAC_A/CMAC_B/CACC/SDP 至少需要写哪些关键寄存器（精度、输入/权重地址与 ram_type、尺寸/stride、SDP 的 BS/ReLU/flying），并指出 CDMA 的权重地址现在应指向 CVSRAM、`ram_type` 设为走 CVIF。
3. **启动与中断**：按逆流水线顺序写出 6 个 `OP_ENABLE` 写；写出轮询 `GLB.S_INTR_STATUS`（字地址 0x0003）与 W1C 清除的伪代码；说明 `done_intr_pd` 的 2 位如何告诉你哪一组完成。
4. **集成说明**：对照 `NV_nvdla.v:16-86`，指出本任务用到的对外接口（CSB、DBB AXI、CVSRAM AXI、dla_intr、两个时钟、复位），并说明若 SoC CPU 走 APB 要加什么。

**自检**：你的点火顺序最后一条必须是 CDMA.OP_ENABLE；SDP 必须在 CACC 之前；BDMA 搬运必须等 done 后再让 CDMA 指向 CVSRAM。完成后，你就把整本手册从「单引擎源码」串成了「一颗可工作的推理加速器」。

> 实际仿真验证可复用 `verif/sim` 流程（u1-l4）：把自己的 trace 放进 `verif/traces/traceplayer/`，`make run TESTDIR=...`，检查 `_test_` 目录下的 `test.log` 是否 PASSED。**端到端运行结果待本地验证。**

---

## 6. 本讲小结

- 一个网络层的生命周期 = **预装数据 → 配置各引擎 → 逆流水线点火 → 轮询/清 GLB done 中断**；CPU 只写寄存器，不搬像素、不算卷积。
- **逆流水线点火**：下游（SDP）先就绪、源头（CDMA）最后点火，整条流水线一次点亮无空泡——真实 trace `sanity3` 的 L211–224 即此规律。
- 各引擎寄存器基址由 `csb_master` 的地址译码决定（如 CDMA 字基址 0x1400、CACC 0x2400、SDP 0x2c00、BDMA 0x1000、GLB 0x0000）。
- **影偶配置 + 2 位 done** 让引擎跑第 N 层时 CPU 可同时写第 N+1 层，完成即无缝切组，双缓冲不停顿。
- 8 类引擎 × 2 组 = 16 个 `done` 源，在 GLB 的 `u_ic` 里聚合成 `core_intr = OR(~mask & status)`，顶层仅一根 `dla_intr`，CPU 靠读 `S_INTR_STATUS` 分辨来源。
- SoC 集成 = 把顶层端口接好：CSB（或经 apb2csb 桥的 APB）、两组 AXI memif（DBB/CVSRAM）、两个时钟、复位、按分区的 SRAM 电源、单根中断——计算细节 IP 内部自理。

---

## 7. 下一步学习建议

- **跑通一个真实大网络**：仓库自带 `cc_alexnet_conv5_relu5_int16_dtest_cvsram`、`googlenet_conv2_3x3_int16` 等 trace（`verif/traces/traceplayer/`），用本讲的「读 trace」方法分析它们的多层配置，体会影偶接跑。
- **对照 C 参考模型**（u7-l3）：打开 `cmod/nvdla_core/NV_NVDLA_core.cpp`，看软件侧如何用同样的事件驱动（op_en、done、影偶）编排引擎，与 RTL 编程模型互证。
- **深入软件栈**：本讲只到「寄存器级编程」。若要写驱动/编译器，可参考 NVDLA 上游的 `sw` 仓库（UMD/KMD），它把本讲的寄存器序列封装成「提交一个层描述符」的高层 API。
- **性能与算力**：结合 `perf/NVDLA_OpenSource_Performance.xlsx` 与 u8-l3 的 SDC（`nvdla_core_clk -period 0.9`），核算本讲这层卷积的理论耗时与带宽需求，理解「算力＝MAC 数×频率」如何落地到具体网络。
- **如果继续改 RTL**：从 BDMA 或 Rubik 这类「纯搬运/重排、无复杂数学」的引擎入手做二次开发，影响面小、易验证，是熟悉整条数据通路的好起点。

至此，NVDLA 硬件手册 36 篇全部完成——从「它是什么」到「如何编程一个层并集成进芯片」，你已经具备端到端阅读、修改与集成这颗开源推理加速器 RTL 的能力。
