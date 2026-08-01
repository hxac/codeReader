# 项目定位与整体架构

## 1. 本讲目标

本讲是《Ventus(乘影) GPGPU 学习手册》的第一篇。读完本讲，你应当能够：

- 说清楚 **Ventus（乘影）是什么**：它是一个用 Chisel 硬件描述语言实现、支持 RISC-V 向量扩展（RVV）的开源通用 GPU（GPGPU）。
- 看懂它的 **整体微架构**：从 Host（CPU）派发任务，到 CTA 调度器、若干个 SM（流多处理器）、L1/L2 缓存、最后经 AXI 接口访问 DDR 的完整数据通路。
- 知道它 **在软硬件工具链中的位置**：本仓库只负责「硬件 RTL」，而编译器（LLVM）、ISA 模拟器（SPIKE）、运行时（pocl/driver）分别在哪些姊妹仓库里。
- 第一次把「架构图里的方块」和「仓库里的源码目录」**一一对应**起来，为后续逐模块精读源码打下地图。

本讲不要求你懂 Chisel，也不要求你已经会跑仿真。我们只建立宏观认知。

---

## 2. 前置知识

如果你对以下概念完全陌生，建议先花十分钟了解，本讲会顺带再解释一遍：

- **GPGPU（General-Purpose GPU）**：把原本用来画图的 GPU 拿来做通用计算。它擅长「用大量线程并行处理同类任务」，例如矩阵乘法、向量加法。
- **GPU 编程模型 grid / block / thread**：程序员写「一个线程（thread）要做什么」，再告诉 GPU「我要并行跑多少个线程」。线程被组织成 block，block 又被组织成 grid。本讲只需建立这个层级直觉，细节在第 2 单元讲。
- **warp（线程束）**：硬件层面，GPU 不会单独调度一个线程，而是把若干线程（Ventus 里是 32 个）捆成一束，叫一个 warp，作为最小调度单位。同一个 warp 里的线程「锁步」执行同一条指令。
- **RISC-V 与 RVV**：RISC-V 是一种开源指令集架构（ISA）；RVV（RISC-V Vector Extension）是它的向量扩展，让一条指令能同时处理一「向量」数据。Ventus 的聪明之处在于：**用 RVV 这套现成的向量指令来充当 GPU 的 SIMT 指令**。
- **Chisel / HDL**：Chisel 是一种基于 Scala 的高级硬件描述语言，最终会生成 Verilog。Ventus 的 RTL 是用 Chisel 写的，再用 Mill 构建工具编译成 Verilog。
- **Cache（缓存）与 AXI 总线**：缓存是靠近计算单元的小容量高速存储，用来缓解「计算快、内存慢」的矛盾；AXI 是 ARM 定义的一种总线协议，常用来把芯片挂在 DDR 内存上。

如果你对以上内容一知半解也没关系，本讲会用通俗语言重新串一遍。

---

## 3. 本讲源码地图

本讲主要读「文档」和「顶层模块」，目的是建立全景图。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md) | 项目门面：一句话定位、架构图、构建命令、工具链指引、输出格式说明。 |
| [docs/Ventus-GPGPU-doc.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md) | 中文架构文档：软件模型、CTA 分配、指令集、微架构各模块的设计意图。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | **顶层硬件模块**：把 CTA 接口、SM 集群、L2 缓存、AXI 全连起来。本讲最重要的源码。 |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | 全局参数：SM 数、warp 数、线程数、缓存配置等，决定了 GPU 的规模。 |

记住一句话：**先看 `GPGPU_top.scala` 的 `class GPGPU_top`，就能一眼数出整块 GPU 由哪些大件拼成。**

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **整体架构图 ventus_arch** —— 硬件长什么样、数据怎么流。
2. **工具链组成** —— 本仓库负责什么、其它仓库负责什么。

### 4.1 整体架构图 ventus_arch

#### 4.1.1 概念说明

一句话定位（来自 README）：

> GPGPU processor supporting RISCV-V extension, developed with Chisel HDL.
> （一个用 Chisel 实现、支持 RISC-V 向量扩展的 GPGPU 处理器。）

见 [README.md:L1-L5](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L1-L5)。

Ventus 的整体微架构在仓库里有两张图，分别在：

- 顶层框图：[docs/images/ventus_arch.png](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/images/ventus_arch.png)，README 在 [README.md:L56-L66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L56-L66) 引用了它。
- SM 内部框图：[docs/images/ventus_arch2.png](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/images/ventus_arch2.png)，中文文档在 [docs/Ventus-GPGPU-doc.md:L156-L159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L156-L159) 引用了它。

从最高层看，整块 GPU 由四大类部件组成：

| 大件 | 职责 | 类比 |
| --- | --- | --- |
| **Host 接口** | 接收 CPU 发来的任务（kernel），返回完成信号 | 「前台」接单 |
| **CTA 调度器** | 把任务拆成 block，按资源情况分发到各个 SM | 「调度员」分活 |
| **SM 集群** | 真正执行计算的若干个流多处理器 | 「车间」干活 |
| **缓存层次 + AXI** | L1/L2 缓存 + 经 AXI 访问 DDR | 「仓库」取料存料 |

#### 4.1.2 核心流程

GPU 的一次典型执行，可以用下面这条「任务流水线」描述：

```
        (1) Host(CPU) 经 AXI4-Lite 写寄存器，派发一个 kernel
                          │  host_req (wg_id, num_wf, start_pc, 资源量...)
                          ▼
        (2) CTAinterface / cta_scheduler_top  接收 workgroup
                          │  按 LDS/SGPR/VGPR 余量选一个 SM
                          ▼
        (3) CTA2warp  把 block 拆成一个个 warp，逐个发给某个 SM
                          │  CTA2warp（warpReq: wg_id, wf_tag, start_pc...）
                          ▼
        (4) pipe (SM 流水线)  取指→译码→ibuffer→记分板→发射→执行→写回
                │                       │
        ┌───────┘                       └────────┐
        ▼                                        ▼
   InstructionCache(指令)            DataCache + SharedMemory(数据)
        │                                        │
        └──────────────┬─────────────────────────┘
                       ▼
              L1Cache2L2Arbiter  (每个 SM 内部 I$/D$ 仲裁)
                       ▼
        SM2clusterArbiter → l2Distribute → cluster2L2Arbiter
                       ▼
              L2 Scheduler  (L2 缓存, 参考 SiFive inclusive-cache)
                       ▼
              out_a / out_d  (TileLink-Lite)
                       ▼
              AXI4Adapter → AXI4 → DDR  (全局内存)
```

要点解读：

- **任务从 Host 进**：CPU 把 kernel 的元信息（workgroup id、warp 数、起始 PC、需要多少寄存器/共享内存）通过 `host2CTA_data` 写进来。
- **CTA 调度器分发**：它知道每个 SM 还剩多少资源，把 block 分给「装得下」的 SM，并以 warp 为单位逐个派发。
- **SM 内部是一条流水线**：取指（icache）、译码、指令缓冲、记分板、发射、各类执行单元（ALU/vALU/vFPU/LSU/SFU/MUL/SIMT/CSR）、写回。LSU 发出的访存请求按地址范围分流到 SharedMemory（共享内存）或 DataCache（全局内存）。
- **缓存层层下探**：L1 miss → 经仲裁 → L2 → 经 AXI → DDR。响应原路返回。
- **完成信号回 Host**：warp 跑完发 `endprg`，整个 block 完成后经 `CTA2host` 回报 `wg_id`。

#### 4.1.3 源码精读

**① 顶层模块的端口 —— 一眼看清对外接口**

[ventus/src/top/GPGPU_top.scala:L150-L163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L163) 定义了 `class GPGPU_top`，它的 `io` 端口就是这块 GPU 对外的全部接口：

```scala
class GPGPU_top(implicit p: Parameters, FakeCache: Boolean = false, SV: Option[mmu.SVParam] = None)
  extends RVGModule{
  override val desiredName = s"GPU"
  val io = IO(new Bundle{
    val host_req = Flipped(DecoupledIO(new host2CTA_data))   // Host 派发任务进来
    val host_rsp = DecoupledIO(new CTA2host_data)            // 完成信号回 Host
    val out_a    = Vec(NL2Cache, Decoupled(new TLBundleA_lite(l2cache_params)))  // L2 发往外部内存的请求
    val out_d    = Flipped(Vec(NL2Cache, Decoupled(new TLBundleD_lite(l2cache_params)))) // 外部内存的响应
    val cycle_cnt = Input(UInt(20.W))
    val icache_invalidate = Input(Bool())
    ...
  })
```

这正好对应架构图里 GPU 的左右两侧：左边是 `host_req/host_rsp`（接 CPU），右边是 `out_a/out_d`（接 DDR，经 AXI 桥接）。`out_a/out_d` 用的是 TileLink-Lite 协议（`TLBundleA_lite`/`TLBundleD_lite`），后面会被 `AXI4Adapter` 转成 AXI4。

**② 顶层例化的四大部件 —— 一眼数清 GPU 由什么拼成**

紧接端口之后，[ventus/src/top/GPGPU_top.scala:L164-L169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L164-L169) 例化了核心部件：

```scala
val cta            = Module(new CTAinterface)                                  // CTA 调度器接口
val sm_wrapper     = VecInit((0 until NSms).map(i => Module(new SM_wrapper(FakeCache, i, SV)).io))  // NSms 个 SM
val l2cache        = VecInit(Seq.fill(NL2Cache)(Module(new Scheduler(l2cache_params)).io))          // NL2Cache 个 L2
val sm2clusterArb  = VecInit(Seq.fill(NCluster)(Module(new SM2clusterArbiter(l2cache_params_l)).io)) // SM→cluster 仲裁
val l2distribute   = VecInit(Seq.fill(NCluster)(Module(new l2Distribute(l2cache_params_l)).io))     // cluster→L2 分发
val cluster2l2Arb  = VecInit(Seq.fill(NL2Cache)(Module(new cluster2L2Arbiter(l2cache_params_l, l2cache_params)).io))
```

读这段代码能得到三件事：

1. **有多少个 SM？** `NSms` 个（由参数决定，见 4.1.3 ③）。
2. **有多少个 L2？** `NL2Cache` 个，每个是一个 `Scheduler`（即 SiFive 风格的 inclusive L2）。
3. **SM 和 L2 之间有三层互联**：`SM2clusterArbiter`（把同 cluster 内多个 SM 的请求仲裁成一路）→ `l2Distribute`（按地址把请求分发到对应 L2）→ `cluster2L2Arbiter`（把多个 cluster 的请求仲裁到 L2）。响应原路返回。这正是架构图中间那团「集群互联」。

**③ 决定 GPU 规模的全局参数**

[ventus/src/top/parameters.scala:L6-L15](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L6-L15) 里几个最关键的数字：

```scala
object parameters {
  def num_sm = 2          // SM 的数量
  var num_warp = 8        // 每个 SM 能同时承载的 warp 数
  var num_thread = 32     // 每个 warp 包含的线程数（lane 数）
  ...
  val GVM_ENABLED: Boolean = sys.env.getOrElse("RTL_GVM_ENABLED", "false").toBoolean  // 是否开协同仿真对拍
  val MMU_ENABLED: Boolean = false   // 是否启用 MMU（TLB/PTW），默认关
  ...
}
```

所以「默认配置」下，Ventus 是一个 **2 个 SM、每 SM 8 个 warp、每 warp 32 线程** 的小型 GPU——这正是它适合用来学习和仿真的规模。这些数字会层层影响位宽（如寄存器堆大小、source 路由位域），后续讲义会反复回到这里。

> 概念小贴士：`num_sfu = (num_thread >> 2)`（[parameters.scala:L91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L91)）说明 SFU（特殊运算单元，如除法/开方）的数量只有 lane 数的 1/4，所以除法这类指令在多线程下会更慢——这是 GPU 设计里常见的「按需配功能单元」取舍。

**④ 一个 SM 内部装了什么 —— 对应架构图里的 SM 方块**

SM 的内部组装在 `class SM_wrapper` 里，见 [ventus/src/top/GPGPU_top.scala:L329-L369](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L329-L369)。关键例化：

```scala
val cta2warp     = Module(new CTA2warp)                 // 接收 warp 派发，分配硬件 warp id
val pipe         = Module(new pipe(sm_id))              // SM 流水线主体（取指/译码/执行/写回）
val l1Cache2L2Arb= Module(new L1Cache2L2Arbiter()(param)) // I$/D$ → L2 的仲裁
val icache       = Module(new InstructionCache(SV)(param)) // L1 指令缓存
val dcache       = Module(new DataCache(SV)(param))     // L1 数据缓存
val sharedmem    = Module(new SharedMemory()(param))    // 片上共享内存
```

这段把架构图里「一个 SM」内部的方块全列出来了：流水线 `pipe`、指令缓存 `icache`、数据缓存 `dcache`、共享内存 `sharedmem`，再经 `L1Cache2L2Arbiter` 统一通往 L2。

**⑤ 可选的 MMU 与 AXI 顶层**

- MMU（内存管理单元）是可选的，由 `MMU_ENABLED` 控制。当为 `true` 时，顶层会额外例化 `L2TLB`、`AsidLookup`、`L1ToL2TlbXBar` 等，见 [ventus/src/top/GPGPU_top.scala:L200-L308](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L200-L308)。默认 `false`，所以本讲先忽略它。
- 对外暴露 AXI 的顶层是 `class GPGPU_axi_top`，见 [ventus/src/top/GPGPU_top.scala:L116-L137](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L116-L137)：它把一个 `AXI4Lite2CTA`（Host 经 AXI4-Lite 写寄存器派发 kernel）和一个 `AXI4Adapter`（L2 TileLink↔AXI4 桥接）包在 `GPGPU_top` 外面。这就解释了架构图里 Host/DDR 与 GPU 的连接方式。

#### 4.1.4 代码实践

**实践目标**：把「架构图里的方块」和「仓库里的源码目录」对应起来，亲手画一张带源码标注的顶层关系图。这是后续所有讲义的基础地图。

**操作步骤**：

1. 打开 [docs/images/ventus_arch.png](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/images/ventus_arch.png)，先肉眼过一遍各个方块的命名。
2. 对照下表（本表已根据真实仓库目录整理），在图上每个方块旁边标注对应的源码目录：

   | 架构图方块 | 源码目录 / 文件 |
   | --- | --- |
   | Host / AXI4-Lite 接口 | `ventus/src/axi/AXI4Lite2CTA.scala`、`ventus/src/axi/AXI4Adapter.scala` |
   | CTA Scheduler（任务调度） | `ventus/src/cta/`（`cta_scheduler.scala`、`wg_buffer.scala`、`allocator.scala`、`resource_table.scala`、`cu_interface.scala`） |
   | SM 顶层包装 | `ventus/src/top/GPGPU_top.scala` 中的 `SM_wrapper` |
   | SM 流水线（取指/译码/执行/写回） | `ventus/src/pipeline/`（`pipe.scala`、`warp_schedule.scala`、`DecodeUnit.scala`、`issue.scala`、`execution.scala`、`writeback.scala` 等） |
   | L1 指令缓存（L1I） | `ventus/src/L1Cache/ICache/`（`ICache.scala`、`ICacheMSHR.scala`） |
   | L1 数据缓存（L1D） | `ventus/src/L1Cache/DCache/`（`DCache.scala`、`DCacheWSHR.scala`） |
   | SharedMemory（共享内存） | `ventus/src/L1Cache/ShareMem/`（`ShareMem.scala`、`BankConflictArbiter.scala`） |
   | L2 Cache | `ventus/src/L2cache/`（`Scheduler.scala`、`BankedStore.scala`、`Directory` 相关） |
   | MMU / TLB / PTW（可选） | `ventus/src/mmu/`（`L1TLB.scala`、`L2TLB.scala`、`PTW.scala`） |
   | 全局参数 / 顶层 | `ventus/src/top/`（`parameters.scala`、`GPGPU_top.scala`） |

3. 在你的图上，用箭头把数据流串起来：`Host → CTA → SM(pipe) → {I$, D$, SharedMem} → L1Cache2L2Arbiter → SM2clusterArbiter → l2Distribute → cluster2L2Arbiter → L2(Scheduler) → AXI4Adapter → DDR`。

**需要观察的现象**：

- 你会发现仓库目录的划分与架构图的方块高度吻合——这是有意的「按功能域分目录」组织方式。
- 注意 `ICache`/`DCache`/`ShareMem` 是 `L1Cache` 的子目录，而 L1 的公共逻辑（Tag、MSHR、接口、仲裁器）直接放在 `L1Cache/` 根目录下。

**预期结果**：得到一张「方块 + 源码路径」一一对应的顶层关系图。后续每读一篇讲义，你都可以回到这张图定位「我现在在读 GPU 的哪一块」。

> 说明：本实践为「源码阅读 + 文档对照」型，无需运行命令，因此不存在「待本地验证」的不确定结果。

#### 4.1.5 小练习与答案

**练习 1**：`GPGPU_top.scala` 里 `sm_wrapper` 的数量由哪个符号决定？它和 `parameters.scala` 里的哪个参数对应？

**参考答案**：由 `NSms` 决定（见 [GPGPU_top.scala:L165](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L165)）。`NSms` 来自 CDE 参数系统，默认值对应 `parameters.scala` 里的 `num_sm = 2`（[parameters.scala:L7](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7)）。所以默认有 2 个 SM。

**练习 2**：架构图里 GPU 对外的两组主要接口分别叫什么？各连向哪里？

**参考答案**：`host_req` / `host_rsp` 连向 Host（CPU），用于派发 kernel 和回收完成信号；`out_a` / `out_d` 连向外部内存（DDR），经 `AXI4Adapter` 转成 AXI4。见 [GPGPU_top.scala:L154-L157](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L154-L157)。

**练习 3**：为什么说「LSU 的访存请求会分流到两个不同的存储」？根据本讲内容简述。

**参考答案**：SM 内部同时有 `DataCache`（对应全局内存，经 L2→DDR）和 `SharedMemory`（片上共享内存）。LSU 会根据访存地址落在哪个地址区间，把请求路由到 `dcache` 或 `sharedmem`（详见 [docs/Ventus-GPGPU-doc.md:L56-L60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L56-L60) 的地址映射说明）。后续 LSU 讲义会展开。

---

### 4.2 工具链组成

#### 4.2.1 概念说明

一个能跑程序的 GPU，光有硬件 RTL 是不够的。要让一段 C/OpenGL 代码真正在 Ventus 上跑起来，需要一整套「软件工具链」配合。本讲的关键认知是：**本仓库（`ventus-gpgpu`）只是这条工具链里的「硬件 RTL」那一环**，其它环节在姊妹仓库里。把职责边界画清楚，你才知道以后遇到问题该去哪个仓库找。

#### 4.2.2 核心流程

一段 GPU 程序从源码到在硬件上跑起来，大致经过这些环节：

```
OpenCL C / C 源码
       │  (编译器)
       ▼
Ventus 机器码（.metadata + .data 测试用例 / kernel 二进制）
       │  (driver / 运行时)
       ▼
   派发给硬件  ──►  Ventus RTL (本仓库)  ──►  结果写回内存
       ▲
       │  (参考模型，用于对拍验证)
   SPIKE isa-simulator
```

对应到具体仓库（来源：[README.md:L62-L66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L62-L66)）：

| 环节 | 仓库 / 来源 | 作用 |
| --- | --- | --- |
| 编译器 | [ventus-llvm](https://github.com/THU-DSP-LAB/llvm-project)（由 [兆松科技 Terapines](https://www.terapines.com/) 开发的基于 LLVM 的 OpenCL C 编译器） | 把 OpenCL C 编译成 Ventus 机器码 |
| ISA 模拟器 | [isa-simulator](https://github.com/THU-DSP-LAB/ventus-gpgpu-isa-simulator)（基于 SPIKE） | 软件参考模型，用于功能对拍验证（GVM 协同仿真） |
| 运行时 | [pocl](https://github.com/THU-DSP-LAB/pocl) + [driver](https://github.com/THU-DSP-LAB/pocl) | 把 kernel 装载、派发给硬件，管理内存 |
| **硬件 RTL** | **本仓库 `ventus-gpgpu`** | **用 Chisel 实现的 GPU 处理器** |
| 一站式环境 | [ventus-env](https://github.com/THU-DSP-LAB/ventus-gpgpu) | 把上面这些打包，方便一次性跑通仿真 |

本仓库内部，和「跑通」最相关的两个目录：

- `sim-verilator/`：基于 Verilator 的 **仿真框架**，把生成的 Verilog 编译成 `libVentusRTL.so`，配上一个 mini driver 和测试用例就能仿真。这是后续「环境搭建」讲义的主角。
- `ventus/fpga_test/`：FPGA 上板验证工程。

> 边界提示：本仓库 **不包含** 编译器和 ISA 模拟器的源码。如果你拿到的只是一个 `.metadata`/`.data` 测试用例，那是「已经编译好」的 kernel，可以直接喂给仿真框架跑，不需要自己装编译器。

#### 4.2.3 源码精读

**① README 如何描述工具链**

[README.md:L62-L66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L62-L66) 原文：

> OpenCL C compiler based on LLVM is developed by Terapines(兆松科技).
> Use the script in ventus-llvm to configure the complete software toolchain, including isa-simulator, pocl and driver.

这段是理解「本仓库职责边界」最权威的一句话：编译器在 `ventus-llvm`，模拟器/运行时分别在各自仓库，**本仓库专注于硬件实现**。

**② 仿真框架在哪、用什么**

[README.md:L104-L108](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L104-L108) 说明：旧的 `make test`（基于 chiseltest）已废弃，官方改用定制的 Verilator 仿真框架，详见 `sim-verilator/`。同时推荐用 `ventus-env` 一站式获取完整工具链。

> 概念小贴士：`make test` 之所以被废弃，是因为 chiseltest 在大型 RTL 上速度和功能都受限；Ventus 团队因此自己写了 `sim-verilator` 框架，把 Verilator 生成的 C++ 模型包成 `libVentusRTL.so`，再用一个 C++ driver 驱动，详见第 7 单元的「Verilator 仿真框架深入」讲义。

**③ 测试用例的格式（.metadata / .data）**

测试用例由两个文件组成（[README.md:L116-L168](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L116-L168)）：

- `.metadata`：kernel 的元信息，结构体 `meta_data` 包含 `start_addr`、`kernel_size[3]`（3D 维度）、`wf_size`（每 warp 线程数）、`wg_size`（每 block 的 warp 数）、`ldsSize`、`sgprUsage`、`vgprUsage` 等。
- `.data`：各 buffer 的初始化数据，顺序存放。

这套格式把「编译器输出」和「仿真输入」解耦——任何能产出这两个文件的途径，都能驱动 Ventus 仿真。

#### 4.2.4 代码实践

**实践目标**：搞清楚「我现在想跑一个 kernel，需要哪些仓库、各自负责什么」，避免一开始就迷路。

**操作步骤**：

1. 浏览 [README.md:L68-L108](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L68-L108) 的 Quick Start，注意它推荐的两条路径：
   - 路径 A（推荐）：用 [ventus-env](https://github.com/THU-DSP-LAB/ventus-env) 一站式获取完整工具链。
   - 路径 B（轻量）：只拿本仓库，用 `sim-verilator/` 做部分 RTL 仿真。
2. 列出 `sim-verilator/` 目录下的关键文件（如 `README.md`、`Makefile`、`ventus_rtlsim.cpp`），确认它们存在。你暂时不用读懂，只要知道「仿真相关代码在这里」。
3. 写一句话回答：「如果我只想验证一小段汇编在硬件上的行为，最少需要本仓库的哪个目录？」
4. 写一句话回答：「如果我想把 OpenCL C 编译成 Ventus 机器码，应该去哪个仓库？」

**需要观察的现象 / 预期结果**：

- 路径 A 的依赖链：`ventus-env` → 拉取 `ventus-gpgpu`（硬件）+ `ventus-llvm`（编译器）+ `isa-simulator`（模拟器）+ `pocl`（运行时）。
- 路径 B 只需本仓库的 `sim-verilator/`，前提是你已经有现成的 `.metadata`/`.data` 测试用例（编译器产出的）。
- 第 3 步答案应是：`sim-verilator/`。第 4 步答案应是：`ventus-llvm`（[llvm-project](https://github.com/THU-DSP-LAB/llvm-project)）。

> 说明：本实践为「文档阅读 + 目录定位」型，无需运行命令；若你想真正执行 `make init` 等命令，那是下一篇讲义（u1-l2 构建系统）的内容，此处不要假装已运行。

#### 4.2.5 小练习与答案

**练习 1**：本仓库（`ventus-gpgpu`）包含 OpenCL C 编译器吗？如果不包含，编译器在哪？

**参考答案**：不包含。编译器是基于 LLVM 的 OpenCL C 编译器，由兆松科技开发，源码在姊妹仓库 [ventus-llvm](https://github.com/THU-DSP-LAB/llvm-project)。本仓库只负责硬件 RTL。见 [README.md:L62-L64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L62-L64)。

**练习 2**：旧的 `make test` 为什么被废弃？现在用什么做仿真？

**参考答案**：`make test` 基于 chiseltest，在大型 RTL 上受限，已被废弃（[README.md:L104-L105](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L104-L105)）。现在使用 `sim-verilator/` 下定制的 Verilator 仿真框架。

**练习 3**：`.metadata` 文件里的 `wf_size` 和 `wg_size` 分别描述什么？它们和 `parameters.scala` 里的哪个参数对应？

**参考答案**：`wf_size` 是每个 warp 的线程数，`wg_size` 是每个 block 的 warp 数。`wf_size` 对应硬件参数 `num_thread`（默认 32），`wg_size` 受 `num_warp`（默认 8，每个 SM 能承载的 warp 数）约束。见 [parameters.scala:L7-L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7-L9) 与 [README.md:L129-L152](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L129-L152)。

---

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个「全景定位」任务：

1. **画一张完整的顶层关系图**（手绘或用工具均可），必须包含：Host、CTA Scheduler、SM（≥2 个）、SM 内部的 L1I/L1D/SharedMem、L1Cache2L2Arbiter、SM2clusterArbiter、l2Distribute、cluster2L2Arbiter、L2 Cache、AXI4Adapter、DDR。
2. **在每个方块上标注**：对应的源码目录或文件（参考 4.1.4 的对照表）。
3. **用两种颜色的箭头**分别画出：
   - 「任务/控制流」：Host → CTA → SM（派发 kernel 与回收完成信号）。
   - 「数据/访存流」：SM 的 LSU → {D$, SharedMem} → L2 → DDR（及响应返回路径）。
4. **在图旁写一段 100 字左右的说明**，回答：默认配置下 Ventus 是多大规模（几个 SM、每 SM 几个 warp、每 warp 几线程）？MMU 默认开还是关？

**验收标准**：

- 图中的方块名能与 `GPGPU_top.scala` 里的 `Module(new ...)` 对上号。
- 数据流箭头方向正确（请求向下、响应向上）。
- 能准确说出默认规模 = 2 SM / 8 warp / 32 thread，MMU 默认关闭。

完成这张图后，你就拥有了整本学习手册的「导航地图」，后续每读一篇讲义都能定位到地图上的具体位置。

---

## 6. 本讲小结

- **Ventus（乘影）** 是一个用 Chisel 实现、支持 RISC-V 向量扩展（RVV）的开源 GPGPU，核心思路是「用 RVV 向量指令充当 GPU 的 SIMT 指令」。
- **顶层 `GPGPU_top`** 由四大类部件拼成：CTA 接口、若干 SM（`SM_wrapper`）、L2 缓存（`Scheduler`）、以及 SM↔L2 之间的三层集群互联（`SM2clusterArbiter`/`l2Distribute`/`cluster2L2Arbiter`）。
- **GPU 对外两组接口**：`host_req/host_rsp` 接 CPU，`out_a/out_d`（TileLink-Lite，经 `AXI4Adapter` 转 AXI4）接 DDR。
- **一个 SM 内部** = `CTA2warp` + `pipe`（流水线）+ `InstructionCache` + `DataCache` + `SharedMemory` + `L1Cache2L2Arbiter`。
- **默认规模**：2 个 SM、每 SM 8 个 warp、每 warp 32 个线程；MMU 默认关闭、GVM 协同仿真默认关闭。
- **职责边界**：本仓库只做硬件 RTL；编译器在 `ventus-llvm`，模拟器/运行时在各自的姊妹仓库，`ventus-env` 把它们打包成一站式环境。

---

## 7. 下一步学习建议

本讲建立了「宏观地图」，接下来应该解决「怎么把它跑起来」：

- **下一篇（u1-l2 构建系统与 Verilog 生成）**：动手执行 `make init` 和 `make verilog`，理解 Mill 构建系统和 `build.sc`，亲眼看到 `GPGPU_top.v` 被生成出来。这是从「看图」到「能跑」的第一步。
- **再下一篇（u1-l3 目录结构与源码组织）**：更系统地遍历 `ventus/src` 各子目录，把本讲的对照表细化到文件级别。
- **第 1 单元末篇（u1-l4 Verilator 仿真与测试用例）**：用 `sim-verilator` 跑通第一个测试用例，看到真实的程序输出（`warp 3 0x... ` 那种日志）。

> 阅读建议：在读后续讲义时，把本讲的「顶层关系图」打印或常开在旁边，每讲一个模块就在图上圈出来，形成空间记忆。
