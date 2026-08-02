# GPU 编程模型与 CTA/Warp/Thread 概念

## 1. 本讲目标

本讲是理解 Ventus（乘影）软件视角的入口。学完后你应当能够：

- 说清 GPU 的 **grid / block / warp / thread** 四级编程层级，以及它们和 Ventus 硬件（SM、硬件 warp）的对应关系。
- 解释为什么 Ventus 用「32 个 thread 组成一个 warp」作为硬件调度单位，以及为什么 block 是 CTA 调度的基本单元。
- 掌握一组关键的**自定义 CSR 寄存器**（`CSR_TID`/`CSR_NUMW`/`CSR_NUMT`/`CSR_WID`/`CSR_LDS` 等）的地址与含义，知道软件如何通过它们拿到 thread id、warp id 和 sharedmem 基址。
- 理解 Ventus 如何**用地址范围区分 sharedmem 与 globalmem** 这两个地址空间。

本讲只讲「软件看到的编程模型」，不涉及流水线内部实现（那是第 4、5 单元的事）。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（来自 u1 单元）：

- **Ventus 是什么**：一个用 Chisel 实现、支持 RISC-V 向量扩展（RVV）的开源 GPGPU，核心思路是「用 RVV 向量指令充当 SIMT 指令」。
- **整体数据通路**：Host → CTA 调度器 → CTA2warp → SM 流水线 → {指令缓存、数据缓存、共享内存} → L2 → DDR。
- **默认规模**：2 个 SM、每 SM 8 个 warp、每 warp 32 个线程（`num_sm=2, num_warp=8, num_thread=32`）。

几个本讲会反复用到的术语，先做通俗解释：

| 术语 | 通俗解释 |
| --- | --- |
| **kernel（核函数）** | CPU 唤起的一个 GPU 并行任务，比如「对两个数组做向量加法」。 |
| **thread（线程）** | GPU 的最小编程单位，程序员写的是「一个 thread 要做的事」。 |
| **warp / wavefront（线程束）** | 硬件把若干 thread 绑成一组，**整体一起调度、一起执行同一条指令**。Ventus 里一个 warp = `num_thread` 个 thread。 |
| **block / workgroup（线程块 / 工作组）** | 若干 warp 组成一个 block，是 CTA 调度的基本单位。 |
| **grid（网格）** | 一个 kernel 的所有 block 构成一个 grid。 |
| **CSR（控制状态寄存器）** | RISC-V 里一类特殊寄存器，软件用 `csrr/csrrs` 等指令读取，Ventus 用它向软件传递 thread id 等运行时信息。 |

> 名词提示：Ventus 文档与代码里 **block = workgroup（WG）**、**warp = wavefront（WF）** 是同一概念的两套叫法（前者偏 CUDA，后者偏 OpenCL/AMD）。后文会混用，请记住它们等价。

## 3. 本讲源码地图

本讲主要读文档与少量「软件可见接口」源码，不深入流水线：

| 文件 | 作用 |
| --- | --- |
| [docs/Ventus-GPGPU-doc.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md) | 架构文档，含编程模型、CSR 表、地址映射的文字说明（注意：部分文字与代码已有出入，本讲会逐处指出）。 |
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md) | 仓库说明，含 `.metadata` 结构（`wf_size`/`wg_size` 字段）与内存分类说明。 |
| [ventus/src/pipeline/CSR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala) | CSR 寄存器定义与实现，是「CSR 约定」一节的权威来源。 |
| [ventus/src/pipeline/CTA2warp.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala) | 把 CTA 派发信息转成 warp 请求、分配硬件 warp id 的模块。 |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | 全局参数（`num_sm`/`num_warp`/`num_thread`/`LDS_BASE` 等）。 |
| [ventus/src/pipeline/LSU.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala) | 访存单元，其中 `AddrCalculate` 用地址范围判定 sharedmem/globalmem。 |

## 4. 核心概念与源码讲解

### 4.1 编程模型：grid / block / warp / thread

#### 4.1.1 概念说明

无论用什么指令集、什么微架构，GPU 的编程模型都是同一套。最基本的行为是：CPU（host）在某个上下文里把数据放进 GPU 内存，唤起一个任务（kernel），GPU 执行完后把结果留在 GPU 内存并给 CPU 一个完成信号，CPU 再把数据搬回来。

这个 kernel 在软件层面按 **grid → block → warp → thread** 的层级展开：

- 一个 kernel 对应一个 **grid**。
- grid 下有若干个 **block（workgroup）**。
- 每个 block 下有若干个 **warp（wavefront）**。
- 程序员写的「对单个数据的操作」抽象成一个 **thread**。

> 文档原话：[docs/Ventus-GPGPU-doc.md:L9-L16](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L9-L16) 解释了这套层级与硬件调度的对应。

关键约定：**硬件层面把 32 个 thread 组成一个 warp，作为整体在 SM 上调度**。程序员指定的 block 数、thread 数，最终都会被硬件换算成「多少个 warp」。Ventus 默认一个 warp = `num_thread = 32` 个 thread。

#### 4.1.2 核心流程：从一个 kernel 到 warp 被派发

```
CPU host 发送 kernel（含 grid 维度、每个 block 的 warp 数、寄存器/localmem/sharedmem 用量）
        │
        ▼
CTA scheduler 以 block 为基本单位接收
        │   按「block 含多少 warp + 占用寄存器/localmem/sharedmem」判断资源
        ▼
分配到剩余资源充足的 SM（同一 block 的所有 warp 只能落在同一个 SM）
        │   以 warp 为单位逐个派发给该 SM
        ▼
SM 侧 CTA2warp 接收，为每个 warp 分配一个硬件 warp id（wid）
        │   并把派发信息（wg_id、wf_tag、基址等）送给 warp scheduler
        ▼
warp scheduler 激活该 warp、预设其 CSR（thread id、warp id、sharedmem base…）
        ▼
该 warp 进入流水线取指执行
```

三条要点（来自文档 [docs/Ventus-GPGPU-doc.md:L13-L16](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L13-L16)）：

1. **block 是单个任务单元**，由 CTA scheduler 接收并分配。
2. **同一 block 的 warp 只能在同一个 SM 上运行**；但同一个 SM 可以同时容纳来自不同 block、甚至不同 grid 的若干 warp。
3. CTA scheduler 记录每个 SM 的寄存器、sharedmemory 使用情况，按资源余量分配；由于 RV 架构未做寄存器映射，**默认每个 warp 分配 32 个标量寄存器 + 32 个向量寄存器**。

#### 4.1.3 源码精读

**默认规模与资源上限**集中在 `parameters.scala`：

[ventus/src/top/parameters.scala:L7-L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7-L9) 定义了三个最基础的规模参数：

```scala
def num_sm = 2
var num_warp = 8
var num_thread = 32
```

- `num_sm = 2`：2 个 SM。
- `num_warp = 8`：每个 SM 最多同时承载 8 个 warp（即 8 个硬件 warp 槽位）。
- `num_thread = 32`：每个 warp 含 32 个 thread（也是向量运算的 lane 数）。

CTA 调度器侧的资源上限用 `CTA_SCHE_CONFIG` 描述（[ventus/src/top/parameters.scala:L164-L197](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L164-L197)），其中：

```scala
object GPU {
  val NUM_CU = num_sm
  val NUM_WG_SLOT = num_block   // 每个 CU 里的 WG 槽位数（num_block=8）
  val NUM_WF_SLOT = num_warp    // 每个 CU 里的 WF(warp) 槽位数
  val NUM_THREAD = num_thread   // 每个 WF 的 thread 数
  ...
}
object WG {
  val NUM_WF_MAX = num_warp_in_a_block   // 每个 WG 最多含多少 warp
  // WF tag = cat(wg_slot_id_in_cu, wf_id_in_wg)
  val WF_TAG_WIDTH_UINT = log2Ceil(GPU.NUM_WG_SLOT) + log2Ceil(NUM_WF_MAX)
  ...
}
```

注意那行注释 **`WF tag = cat(wg_slot_id_in_cu, wf_id_in_wg)`**：一个 warp 在硬件里用一个「tag」标识，高位是它所属 block 在该 SM 中的槽位号，低位是它在 block 内的 warp 编号。这个 tag 后面会决定 thread id（见 4.2）。

**SM 侧如何接收 warp**：`CTA2warp` 用一个位图 `idx_using` 记录当前 SM 里哪些硬件 warp 槽位被占用，用优先级编码器挑一个空槽位分配给新来的 warp（[ventus/src/pipeline/CTA2warp.scala:L54-L69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L54-L69)）：

```scala
val idx_using = RegInit(0.U(num_warp.W))  // 当前 SM 内活跃的 warp
val idx_next_allocate = PriorityEncoder(~idx_using)  // 找一个空槽位
...
io.warpReq.bits.wid := idx_next_allocate  // 这就是分配到的硬件 warp id
```

派发数据 `CTAreqData` 里携带的关键字段（[ventus/src/pipeline/CTA2warp.scala:L17-L33](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L17-L33)）包括：block 的 warp 总数 `dispatch2cu_wg_wf_count`、每 warp 的 thread 数 `dispatch2cu_wf_size_dispatch`、寄存器基址 `sgpr_base/vgpr_base`、sharedmem 基址 `lds_base`、`wf_tag`、`start_pc`、私有内存基址 `pds_base`、3D 维度 `wgid_x/y/z` 等。这些字段正是后面写进 CSR 的内容。

#### 4.1.4 代码实践：读懂一个真实 kernel 的规模

**实践目标**：用一个真实测试用例，把「kernel 维度 → warp 数 → thread 数」这套换算亲手算一遍。

**操作步骤**：

1. 打开 [sim-verilator/testcase/vecadd/vecadd_32b4w8t.metadata](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/vecadd_32b4w8t.metadata)。该文件每个 `uint64_t` 占两行（小端，低 32 位在前）。
2. 对照 [README.md:L128-L152](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L128-L152) 的 `meta_data` 结构，逐字段解码。

**解码结果（参考答案）**：

| 字段 | 行号 | 原始值 | 解码 |
| --- | --- | --- | --- |
| `start_addr` | L1-L2 | `80000000` | 指令起始地址 `0x80000000` |
| `kernel_id` | L3-L4 | `0` | kernel id = 0 |
| `kernel_size[0]` | L5-L6 | `00000020` | x 维 = **32** 个 workgroup |
| `kernel_size[1]` | L7-L8 | `00000001` | y 维 = 1 |
| `kernel_size[2]` | L9-L10 | `00000001` | z 维 = 1 |
| `wf_size` | L11-L12 | `00000008` | **每 warp 8 个 thread** |
| `wg_size` | L13-L14 | `00000004` | **每 block 4 个 warp** |
| `sgprUsage` | L21-L22 | `00000040` | 每_warp_ 64 个标量寄存器 |
| `vgprUsage` | L23-L24 | `00000040` | 每_warp_ 64 个向量寄存器 |

**需要观察的现象 / 预期结果**：

- 这个 kernel 的 grid 是 `32×1×1`，共 **32 个 block**。
- 每个 block 有 `wg_size = 4` 个 warp，每个 warp 有 `wf_size = 8` 个 thread，所以**每个 block 共 4×8 = 32 个 thread**。
- 文件名 `vecadd_32b4w8t` 正好对应「32 block、4 warp、8 thread」——这是一种**自定义规模**：它要求把 `num_thread` 从默认的 32 改成 8 才能跑（见 u2-l3 参数系统）。这也说明：`wf_size`（软件给定的每 warp 线程数）必须和硬件 `num_thread` 一致。

> 如果你手头没有仿真环境，无法实际运行，这一步作为「源码阅读型实践」即可——重点是掌握解码方法与量纲换算。运行相关命令请「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：一个 block 含 64 个 thread，硬件 `num_thread = 32`，这个 block 会被拆成几个 warp？

**答案**：64 / 32 = **2 个 warp**。每个 warp 各 32 个 thread，对应 `CSR_TID` 分别是 0 和 32。

**练习 2**：默认配置（`num_sm=2, num_warp=8, num_thread=32`）下，整个 GPU 最多同时承载多少个 thread？多少个 warp？

**答案**：每 SM 最多 8 个 warp、每 warp 32 thread → 每 SM 256 thread；2 个 SM 共 **16 个 warp、512 个 thread** 同时在片上。

**练习 3**：为什么「同一 block 的 warp 必须落在同一个 SM」？

**答案**：同一 block 的 warp 共享同一块 sharedmem（用同一个 `CSR_LDS` 基址）并通过 barrier 同步，只有物理上在同一 SM 才能共享片上存储与同步资源。

---

### 4.2 CSR 约定：软件如何拿到 thread id / warp id / 基址

#### 4.2.1 概念说明

RISC-V 的 **CSR（Control and Status Register）** 是一类特殊寄存器，软件用 `csrr rd, csr_addr` 这类指令读取。标准 RISC-V 用 CSR 存放诸如 `mstatus`、`mhartid`、浮点状态 `fcsr`、向量配置 `vtype/vl` 等。

GPGPU 多了一层需求：warp 是被硬件动态派发的，软件在编写时**并不知道**自己会是第几个 warp、分到哪块 sharedmem。这些运行时信息由硬件在 warp 启动时写进一组**自定义 CSR**，软件再读出来用。Ventus 把这些自定义 CSR 放在了 `0x800`~`0x80c` 这段地址（机器模式自定义区间）。

#### 4.2.2 核心流程：CSR 在 warp 启动时被预设

```
CTA2warp 派发 warpReq（携带 wf_tag、基址、维度等）
        │
        ▼
warp scheduler / CSRexe 接收，置 io.CTA2csr.valid
        │
        ▼
对应硬件 warp 的 CSRFile 在 CTA2csr.valid 时一次性写入一组寄存器：
   wg_wf_count   ← block 的 warp 总数        (CSR_NUMW)
   wf_size_dispatch ← 每 warp 的 thread 数    (CSR_NUMT)
   wf_tag_dispatch ← 本 warp 的 tag          (CSR_WID)
   lds_base_dispatch ← sharedmem 基址        (CSR_LDS)
   threadid      ← 本 warp 的起始 thread id  (CSR_TID)
   ... (knl_base, wg_id, wg_id_x/y/z 等)
        │
        ▼
软件在 kernel 开头用 csrr 读出这些值，用于后续寻址与分支
```

#### 4.2.3 源码精读

**自定义 CSR 地址定义**在 `CSR.scala` 的 `object CSR` 里（[ventus/src/pipeline/CSR.scala:L28-L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L28-L42)）：

```scala
// thread csr address
val threadid = 0x800.U(12.W)         // base thread id. e.g. 0 32 64
val wg_wf_count =        0x801.U(12.W) // sum of warp for this cta(workgroup).
val wf_size_dispatch =   0x802.U(12.W) // default = num_thread
val knl_base = 0x803.U(12.W)
val wg_id = 0x804.U(12.W)
val wf_tag_dispatch =    0x805.U(12.W) // warp id
val lds_base_dispatch =  0x806.U(12.W) // lds_baseaddr
val pds_baseaddr = 0x807.U(12.W)       // pds_baseaddr
val wg_id_x = 0x808.U(12.W)
val wg_id_y = 0x809.U(12.W)
val wg_id_z = 0x80a.U(12.W)
val csr_print = 0x80b.U(12.W)
val rpc = 0x80c.U(12.W)
```

把它们整理成软件视角的 CSR 表（**以源码为准**）：

| 别名（文档叫法） | 地址 | 源码常量 | 含义 |
| --- | --- | --- | --- |
| `CSR_TID` | `0x800` | `threadid` | 本 warp 的起始 thread id（`num_thread` 的倍数，如 0/32/64） |
| `CSR_NUMW` | `0x801` | `wg_wf_count` | 本 block（workgroup）的 warp 总数 |
| `CSR_NUMT` | `0x802` | `wf_size_dispatch` | 一个 warp 的 thread 总数（默认 = `num_thread`） |
| —（kernel 基址） | `0x803` | `knl_base` | kernel/metadata 基址（即 `metaDataBaseAddr`） |
| —（block id） | `0x804` | `wg_id` | 本 workgroup 的 id |
| `CSR_WID` | `0x805` | `wf_tag_dispatch` | 本 warp 在 block 内的 warp id（取 tag 低位） |
| `CSR_LDS` | `0x806` | `lds_base_dispatch` | 本 block 分配到的 sharedmem 基址 |
| 见下方⚠️ | `0x807` | `pds_baseaddr` | **私有内存（PDS）基址** |
| —（3D 维度） | `0x808/9/a` | `wg_id_x/y/z` | 本 block 在 grid 中的 x/y/z 坐标 |
| —（调试） | `0x80b` | `csr_print` | 调试打印用 |
| —（SIMT） | `0x80c` | `rpc` | SIMT-stack 的返回 PC（`setrpc` 用） |

> 文档里有一张 CSR 表（[docs/Ventus-GPGPU-doc.md:L18-L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L18-L25)），它把 `0x807` 标为 `CSR_GDS`（global memory baseaddr）。但**源码中 `0x807` 实际是 `pds_baseaddr`（私有内存基址）**，并非全局内存基址。本讲一律以源码为准。⚠️ 阅读旧文档时请注意这一出入。

**thread id 是怎么算出来的**：`CSRFile` 在 `CTA2csr.valid` 时把 warp tag 的低位左移 `depth_thread` 位得到 thread id（[ventus/src/pipeline/CSR.scala:L296-L313](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L296-L313)）：

```scala
when(io.CTA2csr.valid){
  wg_wf_count    := io.CTA2csr.bits.CTAdata.dispatch2cu_wg_wf_count
  wf_size_dispatch := io.CTA2csr.bits.CTAdata.dispatch2cu_wf_size_dispatch
  wf_tag_dispatch := io.CTA2csr.bits.CTAdata.dispatch2cu_wf_tag_dispatch(depth_warp-1,0)
  lds_base_dispatch := Cat(LDS_BASE.U(32.W)(31, LDS_ID_WIDTH + 1),
                            io.CTA2csr.bits.CTAdata.dispatch2cu_lds_base_dispatch)
  ...
  threadid := io.CTA2csr.bits.CTAdata.dispatch2cu_wf_tag_dispatch(depth_warp-1,0) << depth_thread
}
```

其中 `depth_thread = log2Ceil(num_thread)`（[ventus/src/top/parameters.scala:L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L36)）。所以 thread id 的公式是：

\[
\text{CSR\_TID} = \text{wf\_id\_in\_wg} \times \text{num\_thread} = \text{wf\_id\_in\_wg} \ll \log_2(\text{num\_thread})
\]

举例：`num_thread=32` 时，warp 0 的 `CSR_TID=0`，warp 1 为 32，warp 2 为 64…… 这与代码注释 `base thread id. e.g. 0 32 64` 完全一致。注意它给的是**本 warp 第 0 个 thread 的全局 id**，warp 内第 `i` 个 thread 还要再加上 `i`（文档建议用 `vid.v` 与 `csrr CSR_TID` 相加得到各 thread 自己的 id，见 [docs/Ventus-GPGPU-doc.md:L27-L30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L27-L30)）。

**CSR 也向 LSU 提供寻址信息**：`CSRFile` 把三个值直接输出给访存单元（[ventus/src/pipeline/CSR.scala:L315-L317](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L315-L317)）：

```scala
io.lsu_tid  := wf_tag_dispatch * num_thread.asUInt  // 私有内存寻址用的 thread id
io.lsu_pds  := pds_baseaddr                          // 私有内存基址
io.lsu_numw := wg_wf_count                           // block 内 warp 总数
```

这再次说明 `0x807` 是**私有内存（PDS）**基址，LSU 用它计算每个 thread 的私有存储位置，而非全局内存基址。

#### 4.2.4 代码实践：整理 CSR 表并推导 thread id

**实践目标**：把文档表与源码对齐，亲手算出一个 warp 的 `CSR_TID`。

**操作步骤**：

1. 读 [ventus/src/pipeline/CSR.scala:L28-L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L28-L42)，把每个自定义 CSR 的「地址 / 源码常量 / 含义」填进一张表（即上面的表格）。
2. 读 [docs/Ventus-GPGPU-doc.md:L18-L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L18-L25) 的旧 CSR 表，**标出与源码不一致的那一行**（`0x807`）。
3. 假设某 block 含 4 个 warp（`CSR_NUMW=4`），硬件 `num_thread=8`，本 warp 是 block 内第 2 号 warp（`CSR_WID=2`），用公式算 `CSR_TID`。

**预期结果**：

- `CSR_TID = 2 × 8 = 16`。即本 warp 的 8 个 thread 的全局 id 为 16~23。
- 本 block 的 4 个 warp 的 `CSR_TID` 分别是 0、8、16、24。
- 不一致项：文档把 `0x807` 标为 `CSR_GDS`（global baseaddr），源码实为 `pds_baseaddr`（私有内存基址）。

> 没有仿真器也能完成这个练习，纯靠读源码与手算即可。

#### 4.2.5 小练习与答案

**练习 1**：`num_thread=32`，`CSR_WID=3`，求 `CSR_TID`。

**答案**：`3 << 5 = 3 × 32 = 96`。

**练习 2**：软件想得到「当前 thread 在整个 block 内的绝对 id」，光读 `CSR_TID` 够吗？

**答案**：不够。`CSR_TID` 是本 warp 第 0 个 thread 的 id。还需加上 thread 在 warp 内的本地序号 `i`（0~`num_thread-1`）。文档建议用 RVV 的 `vid.v` 读出本地序号，再与 `CSR_TID` 相加（[docs/Ventus-GPGPU-doc.md:L29-L30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L29-L30)）。

**练习 3**：为什么 `CSR_NUMT` 几乎总是等于硬件 `num_thread`？

**答案**：因为硬件一个 warp 就是 `num_thread` 个 lane，软件 `wf_size` 必须与之匹配（见 4.1.4 中 `vecadd_32b4w8t` 的例子，它把 `num_thread` 改成 8 来匹配 `wf_size=8`）。所以 `CSR_NUMT` 默认 = `num_thread`（代码注释也是这么写的）。

---

### 4.3 地址映射：用地址范围区分 sharedmem 与 globalmem

#### 4.3.1 概念说明

GPGPU 的物理地址空间通常分两大块：

- **sharedmem（local memory / LDS）**：每个 SM 内部的片上共享存储，速度快、容量小，供同一 block 的各 warp 共享，block 结束即释放。
- **globalmem（global memory）**：所有 SM 共享的大容量空间，对应片外 DDR，经 L2 cache 访问。kernel 的输入/输出数据一般放这里。

在 NVIDIA PTX 里，每个地址都带属性声明它是 shared 还是 global。但 **RISC-V 指令里没有这个属性字段**，而且 Ventus 默认不启用 MMU。于是 Ventus 的方案是：**用地址所在的数值范围来区分**两类内存——落到 sharedmem 窗口的地址走片上 sharedmem，其余地址走 dcache → L2 → DDR。

文档对地址映射的描述见 [docs/Ventus-GPGPU-doc.md:L56-L60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L56-L60)，README 的内存分类见 [README.md:L170-L186](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L170-L186)。

#### 4.3.2 核心流程：一条访存指令如何判定去 sharedmem 还是 dcache

```
向量 load/store 指令带上基址（可能来自 CSR_LDS 或某个全局指针）
        │
        ▼
LSU 内的 AddrCalculate 逐 lane 计算地址 addr(x)
        │
        ▼
对每个 lane 判断 is_shared(x)：
   is_shared = (addr >= LDS_BASE) && (addr < LDS_BASE + sharemem_size)
        │
        ├─ 全部 lane 都落在 sharedmem 窗口  → 发往 SharedMemory（片上）
        └─ 否则（全局地址）                 → 发往 DCache → L2 → DDR
```

#### 4.3.3 源码精读

**sharedmem 窗口的起点**是 `LDS_BASE`（[ventus/src/top/parameters.scala:L136](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L136)）：

```scala
val LDS_BASE = 0x70000000  // LDS base address: a hyperparameter used within each SM
```

**sharedmem 的容量**（即窗口长度）由（[ventus/src/top/parameters.scala:L93-L97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93-L97)）决定：

```scala
def sharedmem_depth = 1024
def sharedmem_BlockWords = dcache_BlockWords   // = 32
def sharemem_size = sharedmem_depth * sharedmem_BlockWords * 4 // bytes
```

代入得 `sharemem_size = 1024 × 32 × 4 = 131072` 字节 = **128 KiB**。于是 sharedmem 地址窗口为：

\[
[\,\text{LDS\_BASE},\ \text{LDS\_BASE} + 128\text{KiB}) = [\,0\text{x}70000000,\ 0\text{x}70020000)
\]

> ⚠️ 文档 [docs/Ventus-GPGPU-doc.md:L59](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/Ventus-GPGPU-doc.md#L59) 写的是「地址 0-128kB 映射到 sharedmem」。这是**旧版描述**，与当前代码不符：当前代码用 `LDS_BASE = 0x70000000` 作为窗口起点，而非 0。阅读文档时请以代码中的 `LDS_BASE` 为准。

**路由判定逻辑**在 `LSU.scala` 的 `AddrCalculate` 里（[ventus/src/pipeline/LSU.scala:L162](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L162)）：

```scala
is_shared(x) := !reg_save.mask(x) ||
  ( addr(x) >= LDS_BASE.U(32.W) && addr(x) < (LDS_BASE.U(32.W) + sharedmemory_maxsize) )
```

即：地址落在 `[LDS_BASE, LDS_BASE + sharemem_size)` 内就是 sharedmem。`sharedmemory_maxsize` 由上层传入，等于 `sharemem_size`（[ventus/src/pipeline/LSU.scala:L548-L554](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L548-L554)）。当所有有效 lane 都是 shared 地址时，整条请求发往片上 SharedMemory；否则按 cacheline 合并后发往 DCache。

**每个 block 的 sharedmem 偏移**：`CSR_LDS` 不是固定的 `LDS_BASE`，而是「`LDS_BASE` 的高位 + 本 block 的偏移」，由（[ventus/src/pipeline/CSR.scala:L304](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L304)）拼出：

```scala
lds_base_dispatch := Cat(LDS_BASE.U(32.W)(31, LDS_ID_WIDTH + 1),
                          io.CTA2csr.bits.CTAdata.dispatch2cu_lds_base_dispatch)
```

含义：保留 `LDS_BASE` 的高位，把低 `LDS_ID_WIDTH` 位替换成 CTA 调度器为本 block 分配的偏移。于是同一 SM 上多个 block 各自的 `CSR_LDS` 都落在 `[LDS_BASE, LDS_BASE+128KiB)` 这段窗口内、互不重叠，软件用 `CSR_LDS + offset` 访问自己的共享内存。

**地址空间全景**（综合文档与代码）：

| 地址区间 | 含义 | 访问路径 |
| --- | --- | --- |
| `[LDS_BASE, LDS_BASE+128KiB)` 即 `[0x70000000, 0x70020000)` | 每个 SM 的片上 sharedmem（按 `CSR_LDS` 偏移分给各 block） | LSU → SharedMemory |
| 其余（如 `0x80000000` 起的指令、`0x90000000` 起的数据） | globalmem（物理地址） | LSU → DCache → L2 → DDR |
| DDR 中 `0 ~ global_baseaddr` 段 | 仅放指令 | 经 icache 访问 |

> 注：上表中 `0x80000000`（指令起始，见 4.1.4 metadata 的 `start_addr`）和 `0x90000000`（数据 buffer 基址）都是 globalmem 侧的物理地址，落出 sharedmem 窗口，因此走 dcache。

#### 4.3.4 代码实践：判定几个地址的去向

**实践目标**：给定几个地址，判断它们各走 sharedmem 还是 dcache，并算出 sharedmem 窗口。

**操作步骤**：

1. 从 [ventus/src/top/parameters.scala:L136](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L136) 读出 `LDS_BASE = 0x70000000`。
2. 从 [ventus/src/top/parameters.scala:L93-L97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93-L97) 算出 `sharemem_size = 128 KiB`。
3. 用 [ventus/src/pipeline/LSU.scala:L162](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L162) 的判定式，对下面三个地址逐一判断。

**需要观察的现象 / 预期结果**：

| 地址 | 落在 sharedmem 窗口？ | 去向 |
| --- | --- | --- |
| `0x70000000`（= `LDS_BASE`，某 block 的 `CSR_LDS` 起点） | 是 | SharedMemory |
| `0x70001000`（block 内偏移 4 KiB） | 是 | SharedMemory |
| `0x90000000`（数据 buffer 基址，见 4.1.4 metadata） | 否 | DCache → L2 → DDR |

- sharedmem 窗口上界 = `0x70000000 + 0x20000 = 0x70020000`。
- 凡 `addr >= 0x70000000 && addr < 0x70020000` 走片上 sharedmem，其余走 dcache。

> 无需运行仿真即可完成此判定；若要实测，可在 sim-verilator 里跑一个用 sharedmem 的 kernel 并用 `--dump-mem` 观察，但这部分「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`LDS_BASE = 0x70000000`，`sharemem_size = 128 KiB`，sharedmem 窗口的上下界（不含上界）是多少？

**答案**：下界 `0x70000000`，上界 `0x70020000`（`128 KiB = 0x20000`）。

**练习 2**：地址 `0x70001000` 和 `0x80000000` 分别走哪里？

**答案**：`0x70001000` 落在窗口内 → SharedMemory；`0x80000000` 在窗口外 → DCache（指令起始地址，走 icache 取指）。

**练习 3**：为什么不能用「地址 0 开始」当 sharedmem 窗口（像旧文档写的那样）？

**答案**：那样会和 globalmem 中放指令/数据的低地址段冲突。当前代码把 sharedmem 抬到 `0x70000000` 这个独立的高位窗口，与 globalmem 物理地址清晰分离，避免歧义。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「追踪一个 block 上板」的小任务：

**场景**：CPU 要派发一个 kernel，其某个 block 含 4 个 warp，每个 warp 8 个 thread（即 `vecadd_32b4w8t` 的规模）。请按顺序回答：

1. **规模换算**（4.1）：这个 block 共多少个 thread？硬件需要把 `num_thread` 配成多少？这个 block 会被 CTA scheduler 当作几个 warp 派发？
2. **CSR 预设**（4.2）：假设该 block 落在某个 SM 上，CTA2warp 为它的第 0、1、2、3 号 warp 分别分配硬件 warp id。写出每个 warp 启动时硬件写入的 `CSR_NUMW`、`CSR_NUMT`、`CSR_WID`、`CSR_TID` 取值。`0x807` 在源码中实际是什么？
3. **地址映射**（4.3）：该 block 的 `CSR_LDS = 0x70000000`。软件用 `CSR_LDS + 0x100` 访问共享内存，这个地址走 sharedmem 还是 dcache？若软件要访问全局数据 buffer（基址 `0x90000000`），又走哪里？写出 sharedmem 窗口的判定式。

**参考答案**：

1. 4×8 = **32 个 thread**；硬件 `num_thread` 须配成 **8**；该 block 被派发为 **4 个 warp**。
2. 四个 warp 的取值：

   | warp | `CSR_NUMW` | `CSR_NUMT` | `CSR_WID` | `CSR_TID` |
   | --- | --- | --- | --- | --- |
   | 0 | 4 | 8 | 0 | 0×8 = **0** |
   | 1 | 4 | 8 | 1 | 1×8 = **8** |
   | 2 | 4 | 8 | 2 | 2×8 = **16** |
   | 3 | 4 | 8 | 3 | 3×8 = **24** |

   `CSR_NUMW`/`CSR_NUMT` 对所有 warp 相同；`CSR_WID` 取 warp tag 低位；`CSR_TID = CSR_WID × num_thread`。源码中 `0x807` 实际是 `pds_baseaddr`（私有内存基址），不是文档写的 global baseaddr。

3. `0x70000000 + 0x100 = 0x70000100`，落在 `[0x70000000, 0x70020000)` 内 → **sharedmem**。全局 buffer 基址 `0x90000000` 在窗口外 → **dcache → L2 → DDR**。判定式：`is_shared = (addr >= LDS_BASE) && (addr < LDS_BASE + sharemem_size)`，其中 `LDS_BASE=0x70000000`、`sharemem_size=128KiB`。

## 6. 本讲小结

- GPU 编程模型是 **grid → block（workgroup）→ warp（wavefront）→ thread** 四级；Ventus 以 **`num_thread`（默认 32）个 thread 组成一个 warp** 作为硬件调度单位，以 **block 作为 CTA 调度基本单元**。
- 同一 block 的所有 warp 只能落在同一个 SM；同一 SM 可混跑来自不同 block/grid 的 warp；默认规模为 `num_sm=2, num_warp=8, num_thread=32`。
- Ventus 用一组 **自定义 CSR（`0x800`~`0x80c`）** 在 warp 启动时把运行时信息（thread id、warp id、sharedmem 基址、block 维度等）交给软件；其中 `CSR_TID = wf_id_in_wg × num_thread`。
- ⚠️ 文档与代码有两处出入需注意：`0x807` 在源码中是**私有内存基址 `pds_baseaddr`**（文档误标为 `CSR_GDS`）；sharedmem 窗口起点是 `LDS_BASE = 0x70000000`（旧文档写「0-128kB」已过时）。**始终以源码为准。**
- Ventus **用地址范围区分两类内存**：地址落在 `[LDS_BASE, LDS_BASE+128KiB)` 走片上 sharedmem，其余走 dcache → L2 → DDR（globalmem）。
- `.metadata` 里的 `wf_size`/`wg_size` 直接对应 `CSR_NUMT`/`CSR_NUMW`，且 `wf_size` 必须与硬件 `num_thread` 一致。

## 7. 下一步学习建议

本讲建立了「软件看到的编程模型」。接下来建议：

- **u2-l2（GPGPU_top 顶层模块）**：从硬件连线角度看 CTA 调度器如何把 block 派发给各 SM，以及 SM↔L2 之间的集群互联——这是本讲「派发流程」的硬件落地。
- **u2-l3（参数系统）**：动手改 `num_warp`/`num_thread`，理解本讲反复出现的 `parameters.scala` 与 `CTA_SCHE_CONFIG` 是如何组织的。
- **第 3 单元（CTA 调度器）**：深入本讲 4.1.2 那条「block → 分配资源 → 派发 warp」流水线的内部实现（wg_buffer、resource_table、allocator、cu_interface）。
- 想提前看「CSR 被谁写、被谁读」的完整闭环，可先扫一眼 [ventus/src/pipeline/CSR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala) 和 [ventus/src/pipeline/CTA2warp.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala)，本讲已引用其中的关键段落。
