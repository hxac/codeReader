# LSU 流水线设计

## 1. 本讲目标

Load/Store Unit（LSU，访存单元）是 Vortex 核心里唯一与内存层次打交道的功能单元。它既是「地址生成器」，也是「内存级并行（MLP）引擎」，还承担了 fence 屏障与原子操作（AMO）的入口职责。本讲学完后，你应当能够：

- 说出 LSU 在 6 级流水线 Execute 级中的位置，以及它在 RTL 与 SimX 两套实现里的对应模块。
- 画出一条 store 指令从 AGU 地址生成、字节使能、数据移位，到进入缓存层次的完整数据通路。
- 解释 packed load（`PACKLB`/`PACKLH`）宏指令如何被展开成多条普通 load，以及 RTL 与 SimX 两侧各自如何计算「基址 + 微操作序号 × 步长」这一地址。
- 理解后端调度器用 index buffer 实现非阻塞访存、用请求队列与读写解耦来支撑 MLP 的设计。

本讲承接 u8-l3（访存合并、本地内存与 DRAM 模型），把视线从「缓存层次」上移到「发出访存请求的功能单元」本身。

## 2. 前置知识

- **SIMT 与 warp**：Vortex 每周期发射一个 warp，warp 内多条 thread 共享 PC、靠 thread mask（`tmask`）控制写回。一条 load/store 指令实际上是「一整排 lane 的并行访存」。
- **Execute 级与 FuncUnit**：见 u6-l4。Execute 级是若干功能单元（ALU/FPU/LSU/SFU/TCU）的容器，LSU 是其中之一，靠 `fu_type` 路由。
- **channel 即流水线**：见 u5-l1。SimX 里模块之间只通过 `SimChannel` 通信；RTL 里对应的是带 valid/ready 握手的 `*_if` 接口。
- **缓存层次**：见 u8-l1。LSU 的请求最终经由本地内存开关（`lmem_switch`）进入 L1 dcache / 本地内存（LMEM）。
- **model_parity**：见 u7-l4。SimX 与 RTL 必须功能与时序一致，LSU 这种带状态的单元尤其需要逐拍对齐。

> 术语提示：本讲的「slice（切片）」指每个 issue slot 实例化的一份 LSU 前端；「lane（通道）」指 warp 内的一条 thread 槽位；「beat（拍）」指一拍内并行发射的一组 lane 请求。`NUM_LSU_LANES` 通常等于 `NUM_THREADS`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [hw/rtl/core/VX_lsu_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv) | LSU 的 RTL 顶层：把分发来的指令按 lane 拆开（`lane_dispatch`）、每块实例化一个 slice、再把结果按 lane 汇聚（`lane_gather`）。 |
| [hw/rtl/core/VX_lsu_agu.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv) | 每通道地址生成单元（AGU），纯组合逻辑，拥有 LSU 的全部地址算术。 |
| [hw/rtl/core/VX_lsu_slice.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv) | LSU 前端：地址分类、字节使能、store 数据移位、fence 锁、多 PID 跟踪、响应格式化（符号扩展 / NaN-box）。 |
| [hw/rtl/core/VX_lsu_scheduler.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_scheduler.sv) | LSU 侧后端调度器外壳：多客户端（LSU/TCU_LD）仲裁后接入共享的 `VX_mem_scheduler` 与唯一 dcache 端口。 |
| [hw/rtl/libs/VX_mem_scheduler.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv) | 通用内存侧调度器（与 cache 共用）：请求队列、index buffer（非阻塞 MLP）、可选 coalescer、批次派发、响应解复用。 |
| [sim/simx/lsu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp) / [lsu_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h) | SimX 侧 LSU：`compute_addrs` 即 AGU，`process_request_step`/`process_response_step` 即前后端，`LsuUopGen` 负责 packed-load 微操作展开。 |
| [docs/designs/lsu_pipeline_design.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/lsu_pipeline_design.md) | LSU 设计文档：把 LSU 描述成「前端 slice + 后端 scheduler」两段流水线，并给出 MLP 分析。 |

> 注意：设计文档里把地址算术记在 `VX_lsu_slice.sv` 的 55-58 行，但当前代码已把全部地址算术抽到独立的 [VX_lsu_agu.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv)，slice 只负责实例化它。本讲以当前源码为准。

## 4. 核心概念与源码讲解

### 4.1 LSU 在流水线中的位置与 RTL 顶层结构

#### 4.1.1 概念说明

LSU 是 Execute 级里专门处理 `LOAD` / `STORE` / `FENCE` / `AMO` 的功能单元。和 ALU/FPU「算一次即完成」不同，LSU 的延迟是**数据相关**的——一次 dcache 命中可能只要几拍，一次 DRAM 未命中则要几十拍。因此 LSU 是流水线里少数带**内部状态**（在途请求表、fence 锁）的单元。

Vortex 的 LSU 在结构上是一个「分—算—聚」的容器：

- 入口把一个 warp 宽度的指令按 lane 拆给若干并行的 **slice**；
- 每个 slice 独立处理自己那一份请求/响应；
- 出口再把各 slice 的结果按 lane 汇聚成一条 commit 消息。

#### 4.1.2 核心流程

RTL 顶层 `VX_lsu_unit` 的数据通路可以画成：

```
dispatch_if[ISSUE_WIDTH]                         commit_if[ISSUE_WIDTH]
        │                                                  ▲
        ▼                                                  │
  ┌──────────────┐   per_block_execute_if   ┌─────────────┴──────┐
  │ lane_dispatch├────────────────────────► │  VX_lsu_slice × N   │
  └──────────────┘                          │  （每 issue slot 一个）│
                                            └─────────────┬───────┘
                   per_block_client_if                    │
        ┌─────────────────────────────────────────────────┘
        ▼
  VX_lsu_scheduler ──► VX_mem_scheduler ──► dcache / LMEM
        ▲
  per_block_result_if  ◄── lane_gather ◄── 各 slice 结果
```

其中 `N = NUM_LSU_BLOCKS`（物理块数），每个块内含 `NUM_LSU_LANES` 条 lane。各 slice 共享下游的 `VX_lsu_scheduler` 与唯一 dcache 端口。

#### 4.1.3 源码精读

模块声明与端口：[VX_lsu_unit.sv:16-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv#L16-L33) 定义了入口 `dispatch_if[ISSUE_WIDTH]`、出口 `commit_if[ISSUE_WIDTH]`，以及「每块连到 `VX_lsu_scheduler` 的客户端接口」`per_block_client_if[NUM_LSU_BLOCKS]`。局部参数 `BLOCK_SIZE = NUM_LSU_BLOCKS`、`NUM_LANES = NUM_LSU_LANES`（[L34-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv#L34-L35)）。

入口 lane 拆分：[VX_lsu_unit.sv:43-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv#L43-L52) 实例化 `VX_lane_dispatch`，把 `ISSUE_WIDTH` 个发射通道的指令分发到 `NUM_LSU_BLOCKS` 个块的 `per_block_execute_if`。

每块一个 slice：[VX_lsu_unit.sv:58-70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv#L58-L70) 用 `for (genvar block_idx ...)` 为每个块实例化一个 `VX_lsu_slice`，把它的 `client_if` 接到对外的 `per_block_client_if[block_idx]`。

出口 lane 汇聚：[VX_lsu_unit.sv:72-81](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv#L72-L81) 实例化 `VX_lane_gather`，把各块的 `per_block_result_if` 汇聚成对外的 `commit_if`。`lane_dispatch` 与 `lane_gather` 都带 `OUT_BUF=3` 的输出缓冲以改善时序。

#### 4.1.4 代码实践

**实践目标**：建立「LSU = lane_dispatch + N×slice + lane_gather」的实例化心智模型。

**操作步骤**：

1. 打开 [VX_lsu_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_unit.sv)。
2. 数出三个子模块：`lane_dispatch`、`g_blocks` 里的 `VX_lsu_slice`、`lane_gather`。
3. 在 `VX_config.toml` 里查 `NUM_LSU_BLOCKS`、`NUM_LSU_LANES`、`ISSUE_WIDTH` 三个值（典型配置下 `NUM_LSU_BLOCKS=1`、`NUM_LSU_LANES=NUM_THREADS`）。

**需要观察的现象**：当 `NUM_LSU_BLOCKS=1` 时，`lane_dispatch`/`lane_gather` 退化为直通，整个 LSU 就是一个 slice。

**预期结果**：能口述「LSU 按 issue slot 切 slice、按 lane 切通道，二者是正交的两个维度」。

#### 4.1.5 小练习与答案

- **练习**：为什么 `lane_dispatch` 和 `lane_gather` 都加了 `OUT_BUF=3` 的弹性缓冲？
- **答案**：LSU 的下游（dcache）延迟波动大，缓冲可以吸收下游反压、避免每拍都把停顿反传到调度器，改善时序与吞吐。
- **练习**：`per_block_client_if` 连到哪个模块？
- **答案**：连到 `VX_core` 里的 `VX_lsu_scheduler`（见 [VX_lsu_scheduler.sv:39-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_scheduler.sv#L39-L43)），后者再接入 `VX_mem_scheduler` 与 dcache。

---

### 4.2 地址生成单元 AGU：plain 与 pack 两种形态

#### 4.2.1 概念说明

AGU（Address Generation Unit）负责为每条 lane 算出最终访存地址。它是**纯组合电路**、无状态，被 slice 用 `for (genvar i ...)` 为每条 lane 实例化一份。

Vortex 的 AGU 支持两种地址形态：

- **plain（普通 load/store/fence）**：地址 = 基址 + 符号扩展的立即数偏移，对应 RISC-V 的 `rs1 + sext(offset)`。
- **pack（packed load 微操作）**：地址 = 基址 + 微操作序号 × 步长，用于把一条 `PACKLB`/`PACKLH` 宏指令拆出的每条微操作送到不同地址。

两种形态在电路上**并行计算**，最后用一个 `is_pack` 选择器二选一，避免串行延迟。

#### 4.2.2 核心流程

设基址为 `base`（=rs1）、步长寄存器为 `stride`（=rs2）、12 位立即数为 `offset`、pack 序号为 `uop_idx = offset[1:0]`，则：

\[
\text{addr}_{\text{plain}} = \text{base} + \text{sext}(\text{offset})
\]

\[
\text{addr}_{\text{pack}} = \text{base} + \text{uop\_idx} \times \text{stride}
\]

pack 形态里的乘法用一个巧妙的展开来避免真正的乘法器。因为 `uop_idx` 只有 2 位（取值 0..3），所以：

\[
\text{uop\_idx} \times \text{stride} = (\text{uop\_idx}[0] \wedge \text{stride}) + (\text{uop\_idx}[1] \wedge (\text{stride}\ll 1))
\]

即把乘法变成「按位与 + 左移 + 加法」。再用一个进位保存加法器（CSA，Carry-Save Adder）把 `base`、`uop_idx[0]?stride:0`、`uop_idx[1]?(stride<<1):0` 三项压缩成 sum/carry 两路，最后做一次普通加法。这样关键路径里只有一个 CSA + 一个加法器，没有多周期乘法器。

#### 4.2.3 源码精读

模块端口：[VX_lsu_agu.sv:24-30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L24-L30) 声明 `base`、`stride`、`offset`、`pack` 输入与 `addr` 输出。文件头注释（[L16-23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L16-L23)）明确「slice 不含任何地址算术，全部地址形态都在本模块」。

pack 路径的按位与 + CSA：[VX_lsu_agu.sv:31-48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L31-L48)。`t0` 是 `uop_idx[0]` 选通的 `stride`，`t1` 是 `uop_idx[1]` 选通的 `stride<<1`，三者经 `VX_csa_32` 压缩成 `csa_sum`/`csa_carry`，再相加得到 `pack_addr`。

plain 路径：[VX_lsu_agu.sv:50-51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L50-L51)，`offset_addr = base + SEXT(XLEN, offset)`。

二选一：[VX_lsu_agu.sv:53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L53)，`assign addr = is_pack ? pack_addr : offset_addr;`。

slice 里每条 lane 实例化一个 AGU：[VX_lsu_slice.sv:63-72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L63-L72)，把 `rs1_data[i]` 当 base、`rs2_data[i]` 当 stride、指令里的 `op_args.lsu.offset`/`op_args.lsu.pack` 当控制信号。

#### 4.2.4 代码实践

**实践目标**：理解 pack 地址如何避免乘法器。

**操作步骤**：

1. 读 [VX_lsu_agu.sv:33-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L33-L47)。
2. 手算 `uop_idx=3`、`stride=1`（PACKLB 的第 4 个字节）时的 `t0`、`t1`、`pack_addr`：`t0=1`、`t1=(1<<1)=2`、`pack_addr = base + 1 + 2 = base+3`。
3. 对比：若直接写 `base + uop_idx * stride`，综合工具可能推断一个乘法器；这里只用移位+CSA+加法。

**需要观察的现象**：`uop_idx` 取值范围被限制在 0..3（2 位），这是「PACKLB 最多 4 字节、PACKLH 最多 2 半字」的硬件体现。

**预期结果**：能解释「2 位序号 → 两个按位与项 → CSA」这条等价变换链。

#### 4.2.5 小练习与答案

- **练习**：为什么 `uop_idx` 取自 `offset[1:0]`，而不是单独的输入端口？
- **答案**：复用已有的 12 位 `offset` 字段携带 pack 序号，节省指令编码位与连线；plain 形态下这几位被当作普通偏移的低 2 位使用。
- **练习**：CSA（进位保存加法器）相比普通行波进位加法器的好处是什么？
- **答案**：CSA 把三个操作数压缩成 sum/carry 两路，延迟与位宽无关（无进位传播），最后只留一次真正的进位加法，从而缩短关键路径。

---

### 4.3 Slice 前端：从地址到内存请求

#### 4.3.1 概念说明

slice 是 LSU 的「指令侧适配层」。它的职责是把一条 warp 指令**翻译成下游缓存能理解的内存请求**，再把缓存返回的原始数据**格式化回寄存器需要的形态**。具体包括：

- **地址分类**：判断每个地址是 I/O、本地内存（LMEM）还是常规全局内存，附加 AMO 元数据。
- **字节使能（byteen）**：根据访存宽度和地址低位，生成「本通道要写/读哪些字节」的掩码。
- **store 数据移位**：把要写入的数据按地址对齐到字内的正确字节位置。
- **响应格式化**：对 load 返回的数据做符号扩展或 NaN-box（32 位浮点装入 64 位寄存器的高 32 位填 1）。

slice 还负责把请求 tag 打包、把响应 tag 解包，并在多 PID（packet ID）场景下跟踪宏加载的逻辑 SOP/EOP。

#### 4.3.2 核心流程

一条 store 指令在 slice 内的流程：

```
execute_if.data
   │  rs1/rs2/op_args
   ▼
[AGU × NUM_LANES] ──► full_addr[i]
   │
   ├── 地址分类 (mem_req_attr: is_addr_io / is_addr_local / amo)
   ├── 地址格式化: req_align = addr低位, mem_req_addr = addr高位
   ├── 字节使能 (mem_req_byteen): 由 wsize + req_align 决定
   └── store 数据移位 (mem_req_data): rs2 按 req_align 左移到对齐位置
   │
   ▼  打包 tag = {header, op_type, req_align, pkt_waddr, is_fence}
client_if.req_*  ──►  VX_lsu_scheduler  ──►  dcache / LMEM
```

load 响应则反向流动：缓存返回 `mem_rsp_data` 后，slice 用 `rsp_align` 选出正确的字节/半字/字，按 `inst_lsu_fmt` 做符号扩展或 NaN-box，写回 `result_if`。

#### 4.3.3 源码精读

地址分类与 AMO 元数据：[VX_lsu_slice.sv:78-116](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L78-L116)。对每条 lane，把 `full_addr` 的高位（块地址）与 `VX_MEM_IO_BASE_ADDR/END`、`VX_MEM_LMEM_BASE_ADDR`（+`LMEM_LOG_SIZE`）比较，分别得到 `is_addr_io`、`is_addr_local` 两个标志位；AMO 使能时还打包 `amo_valid`/`amo_op`/`amo_unsigned`/`hart_id`（`hart_id = cid << (NW+NT) | wid << NT | tid`）。

地址格式化：[VX_lsu_slice.sv:195-198](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L195-L198)。`req_align` 取地址低 `REQ_ASHIFT` 位（字内偏移），`mem_req_addr` 取去掉字内偏移后的高位。

字节使能：[VX_lsu_slice.sv:201-226](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L201-L226)。按 `inst_lsu_wsize(op_type)`（0=8 位、1=16 位、2=32 位、3=64 位）和 `req_align` 设置对应字节位；例如 16 位访问置相邻 2 个字节位。

store 数据移位：[VX_lsu_slice.sv:237-253](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L237-L253)。按 `req_align` 把 `rs2_data[i]` 左移到字内正确位置，使下游能直接按 `byteen` 写入。

对齐检查：[VX_lsu_slice.sv:229-234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L229-L234) 用 `RUNTIME_ASSERT` 检查地址按访存宽度对齐——Vortex **不支持非对齐访存**，非对齐会触发仿真期断言（不是硬件异常）。

请求/响应接口打包：[VX_lsu_slice.sv:336-366](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L336-L366)。请求侧把 `mem_req_*` 信号汇入 `client_if.req_data`，响应侧从 `client_if.rsp_data` 解出 `mem_rsp_*` 与 tag。

load 响应格式化：[VX_lsu_slice.sv:370-407](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L370-L407)。先用 `rsp_align` 从 64 位数据里选出 32/16/8 位，再按 `inst_lsu_fmt`（`LSU_FMT_B/H/BU/HU/W/WU/D`）做符号扩展；`LSU_FMT_W` 且目标是浮点寄存器时做 NaN-box（高 32 位填 `0xFFFFFFFF`）。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 32 位 store 的字节使能与数据移位。

**操作步骤**：

1. 读 [VX_lsu_slice.sv:201-226](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L201-L226) 与 [L237-253](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L237-L253)。
2. 假设 `LSU_WORD_SIZE=8`（64 位字）、`op_type` 表示 32 位 store、`req_align=4`（即地址低 3 位为 4，字内偏移 4 字节）。
3. 手推：`inst_lsu_wsize` = 2，走 `case 2` 分支，置字节 4..7 → `mem_req_byteen = 0xF0`；`mem_req_data` 把 `rs2` 左移 32 位到高字位置。

**需要观察的现象**：字节使能的置位位置与数据移位量**由同一个 `req_align` 决定**，二者天然一致。

**预期结果**：能说出「`byteen` 告诉缓存写哪些字节，`data` 已预先移位到对齐位置，二者配套」。

#### 4.3.5 小练习与答案

- **练习**：为什么 AMO 的 `hart_id` 要把 `tid` 放在最低位？
- **答案**：多缓存 AMO 一致性要求把同一地址的 AMO 路由到同一个固定的缓存点（见 u11-l2），`hart_id` 作为请求者标识参与该路由，低位放 `tid` 让同一 warp 内不同线程的 hart_id 连续、便于聚合（见 [VX_lsu_slice.sv:103-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L103-L107)）。
- **练习**：非对齐访存会怎样？
- **答案**：仿真期触发 `RUNTIME_ASSERT`（[L229-234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L229-L234)），软件契约要求地址必须按访存宽度对齐，硬件不做拆分。

---

### 4.4 fence 屏障、多 PID 跟踪与后端 MLP

#### 4.4.1 概念说明

slice 与后端调度器还要解决三个「带状态」的问题：

- **fence 全屏障**：一条 `FENCE` 指令要求「之前所有访存完成、之后访存才能开始」。slice 用一个 `fence_lock` 位实现**每 slice 的总屏障**——锁定期间既不接受新请求、也不让新响应干扰，直到在途响应排空。
- **多 PID 跟踪**：一条宽 load（如来自 VPU）可能被展开成多个 PID（packet）子包，它们共享同一个 `wid`/`tag`/`rd`，但响应可能乱序返回。slice 需要为整条宏 load 维护「逻辑 SOP/EOP」，使写回路径只看到一次起止。
- **非阻塞 MLP**：后端用 index buffer 让多达 `CORE_QUEUE_SIZE` 条读请求同时在途、响应任意顺序返回，这是 LSU 吞吐的根本来源。

#### 4.4.2 核心流程

**fence 锁的生命周期**（[VX_lsu_slice.sv:145-162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L145-L162)）：

```
fence 最后一个 PID 请求发射 (mem_req_fire && eop)  ──► fence_lock = 1
   ↓  锁定：mem_req_valid=0, execute_if.ready=0（排空在途响应）
fence 对应响应包完成 (mem_rsp_fire && eop_pkt)     ──► fence_lock = 0
```

**多 PID 跟踪**（[VX_lsu_slice.sv:258-323](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L258-L323)）：用一个 `VX_allocator` 在 SOP 请求发射时分配一个槽位，`pkt_ctr` 记录该宏 load 还有多少子包未返回，每个内存级响应 EOP 时减一；当计数归零且遇到最后一个内存级响应时，置逻辑 `mem_rsp_eop_pkt = 1`。这样就把「内存级 SOP/EOP」（每通道响应包）与「逻辑 SOP/EOP」（每宏 load）解耦。

**后端 MLP**（`VX_mem_scheduler`）：

- **请求队列**：深度 `CORE_QUEUE_SIZE` 的弹性缓冲，平滑前后端反压（[VX_mem_scheduler.sv:175-188](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L175-L188)）。
- **index buffer**：每条**读**请求进队列时分配一个 `ibuf_waddr` 槽位（[L204-223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L204-L223)），槽位下标嵌入到内存 tag 里；响应带 tag 回来时按下标自路由回原槽位、恢复原始元数据。这是 O(1) 的 free-list + RAM，**没有 CAM、没有关联搜索**。

每 slice 的峰值 load 吞吐约为：

\[
\text{throughput} = \min\!\left(\frac{\text{CORE\_QUEUE\_SIZE}}{\text{avg\_load\_latency}},\ 1\right) \text{ 请求/周期}
\]

默认 `CORE_QUEUE_SIZE=8`、DRAM 延迟约 50 拍时只有约 0.16（DRAM 是瓶颈）；cache 命中 5 拍时约 1.6，受限于前端每拍 1 条的上限。

#### 4.4.3 源码精读

fence 锁：[VX_lsu_slice.sv:145-179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L145-L179)。`fence_lock` 在 fence 最后一个 PID 发射时置 1，在对应响应包 EOP 时清 0；`mem_req_valid` 与 `execute_if.ready` 都被 `~fence_lock` 门控。

多 PID 跟踪表：[VX_lsu_slice.sv:258-323](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L258-L323)。当 `PID_BITS != 0` 时实例化 `g_pid` 块：`VX_allocator` 在 `mem_req_rd_eop_fire` 时分配槽位，`pkt_ctr`/`pkt_sop`/`pkt_eop` 三张表（深度 `LSU_PENDING_SIZE`）维护每宏 load 的状态；逻辑 EOP 判定为 `mem_rsp_eop && pkt_eop[raddr] && (pkt_ctr[raddr]==1)`（[L314](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L314)）。`PID_BITS==0` 时走 `g_no_pid` 直通（[L318-323](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L318-L323)）。

请求队列：[VX_mem_scheduler.sv:175-188](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L175-L188)，深度 `CORE_QUEUE_SIZE`、带输出寄存。

index buffer：[VX_mem_scheduler.sv:199-223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L199-L223)。`ibuf_push = core_req_fire && ~core_req_rw`——只有读请求消耗 ibuf 槽位；`ibuf_pop = crsp_fire && crsp_eop`——响应 EOP 时释放。tag 自路由：`reqq_tag_u = {uuid, ibuf_waddr}`（[L169-173](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L169-L173)）。

#### 4.4.4 代码实践

**实践目标**：理解「读写共享请求队列、但不共享 index buffer」的设计。

**操作步骤**：

1. 读 [VX_mem_scheduler.sv:160-198](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L160-L198)。
2. 注意 `ibuf_push = core_req_fire && ~core_req_rw`（[L204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L204)）：写请求不分配 ibuf 槽位。
3. 思考：一长串 store 会不会被一堆在途 load 堵住？

**需要观察的现象**：写请求只走请求队列、不进 index buffer，因此即使读的 ibuf 接近满，写仍可直通。

**预期结果**：能解释「读写在请求队列里合流，但只有读消耗 MLP 槽位」这一关键解耦。

> 待本地验证：默认配置 `LINE_SIZE = WORD_SIZE` 使 `COALESCE_ENABLE=0`（见 [lsu_pipeline_design.md §3.3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/lsu_pipeline_design.md)），即 LSU 本身**不**做访存合并，合并发生在 dcache 一侧（见 u8-l3）；本步骤可在 `VX_config.toml` 中确认 `LINE_SIZE`/`WORD_SIZE` 取值。

#### 4.4.5 小练习与答案

- **练习**：为什么 fence 是「每 slice 总屏障」而不是「每地址」？
- **答案**：slice 只有一个 `fence_lock` 位，锁定后该 slice 上所有请求都停（[L167-179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L167-L179)）；设计文档 §5 把「无每地址 fence 粒度」列为已知局限。
- **练习**：index buffer 相比 CAM（内容寻址存储器）的好处？
- **答案**：free-list + RAM 的分配/释放都是 O(1)、无全标签比较，面积与功耗都更低，且能支撑乱序响应。

---

### 4.5 SimX 侧 LSU 与 packed-load 的 LsuUopGen

#### 4.5.1 概念说明

SimX 的 `LsuUnit` 继承自 `FuncUnit<VX_CFG_NUM_LSU_BLOCKS>`（见 u6-l4），用 C++ 复现 RTL LSU 的语义与时序。它把 RTL 的 slice + scheduler 两段结构映射成几个每周期钩子：

- `compute_addrs`：等价于 AGU，为每条 thread 算地址。
- `process_request_step`：等价于前端 + 后端派发，按 `NUM_LSU_LANES` 一批一批地发请求。
- `process_response_step`：等价于响应解复用 + 格式化。
- `ingest_inputs`：从输入 channel 取一条 trace 进 `req_queue`（制造一拍真实延迟）。

本模块的重点是 **`LsuUopGen`**：它把一条 packed-load 宏指令（`PACKLB` 读 4 字节、`PACKLH` 读 2 半字）展开成多条普通 load 微操作，让下游 LSU 不用专门处理「打包」语义。

#### 4.5.2 核心流程

**AGU 公式**（[lsu_unit.cpp:157](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L157)）：

\[
\text{addr}[t] = \text{rs1}[t] + \text{stride} \times \text{rs2}[t] + \text{offset}
\]

其中 `stride` 来自 `IntrLsuArgs.stride`、`rs2` 是寄存器值。AMO 时 `stride=0`、`offset=0`，退化为 `addr = rs1`。

**每周期执行顺序**（`on_tick`，[L520-532](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L520-L532)）：先消费响应（`process_response_step`），再按本拍初的 `req_queue` 派发（`process_request_step`），最后才 ingest 一条新 trace——这个「drain-before-fill」顺序让 `req_queue` 成为真实的一拍流水级，而不是同拍穿透。

**批次派发**（[L402-474](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L402-L474)）：用 `remain_addrs` 记录一条 trace 还有多少 lane 地址没发，每拍取 `min(NUM_LSU_LANES, remain_addrs)` 条为一拍（beat），跳过全不活跃的拍，直到发完才从 `req_queue` 弹出。

**packed load 展开**（`LsuUopGen::get`，[L37-72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L37-L72)）：

- `uop_count`：`width==0`（PACKLB）返回 4，否则（PACKLH）返回 2（[L30-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L30-L35)）。
- 每条微操作被设成普通的无符号 load（`LBU`/`LHU`），并把 **`stride` 字段填成 `uop_index`**、`offset=0`（[L69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L69)）。代入 AGU 公式得到 `addr = rs1 + uop_index × rs2`，与 RTL 的 pack 地址 `base + uop_idx × stride` 完全一致。

#### 4.5.3 源码精读

AGU 实现：[lsu_unit.cpp:110-173](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L110-L173)。注意每条 thread 的槽位是 **tid-stable** 的（`entry index == tid`，不活跃 thread 留 size=0 的洞，[L147-166](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L147-L166)）——这与硬件「thread → lane」的固定绑定一致，避免把同一 thread 的连续访问压到不同 lane 上而被 per-bank 仲裁重排。

packed load 微操作生成：[lsu_unit.cpp:37-72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L37-L72)。`bytesel` 由本微操作的字节掩码与（浮点目的时的）NaN-box 高位 `0xF0` 拼成（[L58-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L58-L60)），LSU 按 bytesel 把数据移到目的寄存器的正确位置，`OpcUnit::writeback` 再按掩码 OR 合并——因此多条微操作的结果能拼进同一个目的寄存器。

响应处理与不变量：[lsu_unit.cpp:218-269](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L218-L269)。每条响应 fragment 按 `tag` 找回 `pending_reqs` 里的在途项，按 width 做符号扩展/NaN-box 后写入对应 thread 的目的数据；[L231](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L231) 的断言 `LOAD response must carry line payload` 正是 u5-l3 讲过的「LOAD 响应必须携带 line 数据」不变量的运行时守卫。

每块状态：[lsu_unit.h:128-148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/../sim/simx/lsu_unit.h#L128-L148) 定义 `lsu_state_t`，含 `req_queue`（深度 `LSU_QUEUE_IN_SIZE`，对应 RTL 请求队列）、`pending_reqs`（深度 `LSU_PENDING_SIZE`，对应 index buffer）、`fence`（`FenceController`，对应 `fence_lock`）、`addr_list`/`remain_addrs`（批次派发游标）。

fence 控制器：[lsu_unit.h:46-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h#L46-L74)，`try_release` 仅在「pending 表空 且 输出 channel 可接受」时才解锁并转发 trace——与 RTL「排空在途响应后才清 fence_lock」语义一致。

#### 4.5.4 代码实践

**实践目标**：证明 SimX 与 RTL 的 packed-load 地址计算等价（model_parity 的一个具体落点）。

**操作步骤**：

1. 读测试 [tests/regression/packld/kernel.cpp:5-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/packld/kernel.cpp#L5-L28)：`vx_packlb_f(base, stride=1)` 把 4 个连续字节打包成一个 float；`vx_packlh_f(base, stride=2)` 把 2 个连续半字打包。
2. 在 SimX 侧（[lsu_unit.cpp:69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L69)）：PACKLB 拆出 4 条微操作，`stride` 字段分别为 0/1/2/3，`rs2=1` → 地址 = `base + {0,1,2,3}`。
3. 在 RTL 侧（[VX_lsu_agu.sv:33-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L33-L47)）：`stride=rs2=1`、`uop_idx=offset[1:0]=0..3` → `pack_addr = base + {0,1,2,3}`。
4. 用 `./ci/blackbox.sh --driver=simx --app=packld` 跑一遍，确认 `PASSED!`（待本地验证）。

**需要观察的现象**：两侧算出的 4 个地址完全相同，4 条微操作的结果按 bytesel 拼回同一个目的寄存器，得到 `b0 | (b1<<8) | (b2<<16) | (b3<<24)`。

**预期结果**：能复述「SimX 把 uop_index 放进 stride 字段乘以 rs2，RTL 把 uop_idx 放进 offset[1:0] 乘以 stride(=rs2)，两者数学等价」。

#### 4.5.5 小练习与答案

- **练习**：为什么 `LsuUopGen` 把每条微操作设成 `LBU`/`LHU`（无符号 load）而不是 `LB`/`LH`？
- **答案**：见 [lsu_unit.cpp:49-53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L49-L53) 注释——打包语义只搬字节、不做符号扩展，符号扩展会污染高位字节；字节选择与合并由 bytesel + OR 完成。
- **练习**：`on_tick` 里为什么先 `process_response_step` 再 `process_request_step`，最后才 `ingest_inputs`？
- **答案**：让 `req_queue` 成为真实的一拍流水级（drain-before-fill）；若用 while 循环一口气 ingest 多条，会凭空制造写带宽、把这一拍延迟折叠为零，破坏 model_parity（见 [L272-275](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L272-L275) 注释）。

## 5. 综合实践

**任务**：完整跟踪一次 `sw`（32 位 store）从 AGU 到缓存层次的路径，并在 packld 例子上验证 packed load 的展开。

**步骤**：

1. **地址生成**：假设 warp 内 thread 0 执行 `sw rs2, 4(rs1)`，`rs1=0x1000`。在 [VX_lsu_agu.sv:50-53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_agu.sv#L50-L53) 走 plain 路径：`addr = 0x1000 + sext(4) = 0x1004`。
2. **前端格式化**：在 [VX_lsu_slice.sv:195-253](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L195-L253) 推出 `req_align=4`、`mem_req_byteen=0xF0`、`mem_req_data` 为 rs2 左移 32 位、地址分类 `is_addr_local=0`（假设落在全局内存）。
3. **tag 打包与下发**：tag 按 [L326-332](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L326-L332) 打包后经 `client_if` → `VX_lsu_scheduler` → `VX_mem_scheduler` 请求队列。因为是 store（`core_req_rw=1`），**不分配 index buffer 槽位**（[VX_mem_scheduler.sv:204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/libs/VX_mem_scheduler.sv#L204)），直通到 dcache 端口。
4. **进入缓存**：请求由 `lmem_switch` 按地址分流（见 u8-l3）：全局内存走 dcache，本地内存走 LMEM。
5. **packed load 验证**：对照 4.5.4 的实践，跑 `packld` 例子，确认 PACKLB 的 4 条微操作地址为 `base+0/1/2/3`、PACKLH 的 2 条为 `base+0/2`。

**交付物**：一张「store 数据通路图」（AGU → 字节使能/数据移位 → tag 打包 → 请求队列 → dcache）加一份「packed load 地址对照表」（SimX 与 RTL 两侧的 uop_index、stride、地址三列）。

## 6. 本讲小结

- LSU 是 Execute 级里唯一带内部状态的功能单元，RTL 顶层 `VX_lsu_unit` 是「`lane_dispatch` + N×`VX_lsu_slice` + `lane_gather`」的分—算—聚容器。
- AGU 是每通道的纯组合地址生成器，plain 形态用 `base + sext(offset)`、pack 形态用 `base + uop_idx × stride`，后者用 CSA 避免乘法器。
- slice 前端把地址翻译成下游可用的请求：地址分类（IO/LMEM/AMO）、字节使能、store 数据移位、tag 打包；响应侧做符号扩展与 NaN-box；非对齐访存会触发断言。
- slice 用 `fence_lock` 实现每 slice 总屏障，用 `pkt_allocator` + `pkt_ctr` 把内存级 SOP/EOP 解耦为宏 load 的逻辑 SOP/EOP。
- 后端 `VX_mem_scheduler` 用请求队列 + index buffer 实现非阻塞 MLP：读写共享队列但只有读消耗 ibuf 槽位，响应可乱序自路由回原槽位。
- SimX 的 `LsuUnit` 用 `compute_addrs`/`process_request_step`/`process_response_step` 复现上述语义；`LsuUopGen` 把 packed load 展开成普通 load，其 `addr = rs1 + uop_index × rs2` 与 RTL 的 pack 地址数学等价，是 model_parity 的具体落点。

## 7. 下一步学习建议

- **u11-l1 虚拟内存子系统**：本讲里请求地址默认是 VA，开启 `VX_CFG_VM_ENABLE` 后由 dcache 内的 MMU/TLB 翻译成 PA，可顺藤阅读 `sim/simx/mem/mmu.cpp` 与 `mmu_tlb.cpp`。
- **u11-l2 原子操作与多缓存一致性**：本讲只点到 AMO 在 slice 端的元数据打包（`hart_id` 路由），AMO 的 read-modify-write 与多缓存一致性细节在那里展开。
- **u8-l3 访存合并与 DRAM**：本讲的 LSU 默认不做 coalescing（`LINE_SIZE=WORD_SIZE`），合并发生在 dcache 的 `mem_coalescer`；想理解 lane 请求如何折叠成 cache line 请求，回到 u8-l3 复习。
- 想做扩展实验的读者，可参考 [lsu_pipeline_design.md §6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/lsu_pipeline_design.md) 列出的改进项（加深 `CORE_QUEUE_SIZE`、加 stride 预取器等），挑一项在 SimX 与 RTL 两侧同步原型化。
