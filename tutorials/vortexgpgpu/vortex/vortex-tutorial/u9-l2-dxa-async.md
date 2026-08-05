# DXA 异步拷贝与多播

## 1. 本讲目标

本讲讲解 Vortex 的 **DXA（Direct eXecution Accelerator）**——一个类似 NVIDIA Hopper TMA 的**异步批量拷贝 + 多播**单元。DXA 让一条 warp 指令就能启动一次多周期、分块的 **GMEM（全局内存）→ LMEM（本地内存/共享内存）** 异步拷贝，发起 warp 立即获释，拷贝落地后通过屏障「事务（transaction, tx）」通知消费方。

学完后你应当掌握：

1. DXA 的**异步 DMA 执行模型**：一条指令发起、warp 不阻塞、完成靠 tx 释放屏障。
2. **多播（multicast）**如何「读一次 GMEM，把同一份数据重放到多个共驻 CTA 的本地内存」。
3. 设备侧前端 `DxaUnit` 如何解码 4-lane wgather 指令，集群引擎 `DxaCore` 如何驱动 GMEM 读 + LMEM 写 + 多播重放 + 完成通知。
4. kernel 侧 `vx_dxa.h` 的 issue 内联函数与 `vortex::dxa_multicast_*d` C++ 帮助类如何封装「两栅栏惯用法」。

> ⚠️ 术语澄清：本讲的「多播」是把一份数据送到**一个 multicast group（一组 `cluster_dim` 个共驻 CTA）**的各自 LMEM 区域，**不是**送到多个硬件 `VX_cluster`（L2 共享单元）。DXA 引擎本身是 cluster 级共享（每个 `VX_cluster` 一个 `DxaCore`），多播目标则是同一核上连续 CTA 槽里的若干 CTA。注意区分 **CTA cluster group**（Hopper 风格的线程块簇）与 **硬件 cluster**（Vortex 的 L2 共享层级）。

---

## 2. 前置知识

本讲为 advanced 层级，承接以下已建立的认知（不重复展开）：

- **本地内存 LMEM ≈ CUDA shared memory**（u8-l3）：每核私有、由 LSU 地址译码直达、绕过缓存栈、单周期近存储。DXA 的写目标就是 LMEM。
- **屏障与 expect_tx**（u4-l3）：`vortex::barrier` 的 `arrive_and_wait()` 同时等待「到达数 == warps」**且**「events == 0」。`expect_tx(1)` 预注册一个 tx 事件；DXA 完成时调用 `barrier_event_release` 抵消一个事件，从而解锁消费方。
- **SFU 是分派器而非执行单元**（u6-l4）：DXA 指令经 `FUType::SFU` 路由，`op_type == DxaType` 时 SFU 把它交给 `DxaUnit` 子单元。
- **基数规则**（u5-l3）：模块只通过 channel 通信。DXA 读写都走真实的 `MemReq`/`MemRsp` channel 链（GMEM 经 L2、LMEM 经每核直连 channel），不跨所有权层级走后门——这是 SimX 担当 RTL 预言机的基础。
- **model_parity**（u7-l4）：SimX 的 `DxaCore` 与 RTL 的 `VX_dxa_core` 是同一架构的两种实现，必须逐拍一致。
- **TCU WGMMA**（u9-l1）：DXA 与 TCU 共享 LMEM-DMA 端口；DXA 的 **K-major 转置**拷贝能直接产出 WGMMA B-tile 所需的 SMEM 布局，是 DXA↔TCU 的核心衔接。

补充两个本讲用到的基础概念：

- **DMA（Direct Memory Access）**：由专用引擎（而非运算核）搬运数据的机制，CPU/GPU 核心在搬运期间可继续做别的计算，从而**用搬运隐藏访存延迟**。
- **CTA cluster group**：一组被调度器保证**共驻在同一核、占用连续 `cta_local_id` 槽位**的 CTA（由 launch 时的 `cluster_dim` 声明）。多播依赖这个共驻契约。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`docs/designs/dxa_async_copy_multicast.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md) | DXA 设计总文档：执行模型、描述符、RTL 模块清单、端到端数据流、SimX 模型说明 |
| [`sw/kernel/include/vx_dxa.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h) | **设备侧 kernel API**：`vx_dxa_issue_{1..5}d_wg` / `..._multicast_wg` issue 内联函数，`vortex::dxa_multicast_*d` C++ 帮助类 |
| [`sw/runtime/include/dxa.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h) | **主机侧 API**：`vortex::dxa::program_{1..5}d` 编程描述符、`set_multicast`、`set_layout` |
| [`sw/common/dxa_meta.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/dxa_meta.h) | 描述符 META 字段的位宽/位偏移（主机编码与 SimX 解码共享） |
| [`sim/simx/dxa/dxa_unit.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.h) / [`.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp) | **设备侧前端**：每核 SFU 子单元，解码 4-lane wgather → `DxaReq` |
| [`sim/simx/dxa/dxa_core.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.h) / [`.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp) | **集群引擎 SimObject**：聚合并分发请求、驱动 GMEM 读 + LMEM 写 + 多播重放 |
| [`sim/simx/cluster.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp) | 在 cluster 层把 `DxaCore` 接到 L2、每核 SFU、每核 LMEM，并挂完成回调 |
| `tests/regression/dxa_copy_mcast/`、`tests/regression/sgemm2_dxa_mcast/` | 两个多播回归测试（kernel + 主机驱动） |

---

## 4. 核心概念与源码讲解

### 4.1 DXA 执行模型：异步分块 GMEM→LMEM 拷贝 + 多播

#### 4.1.1 概念说明

DXA 是一个 RISC-V ISA 扩展（`MISA` bit 10），由 `VX_CFG_EXT_DXA_ENABLE` 开启。它的核心思想是：**让一条 warp 指令描述一次复杂的内存搬运，由专用硬件引擎异步执行，warp 自己继续往下跑**。

设计文档把 DXA 的能力归纳为三类（[dxa_async_copy_multicast.md:24-34](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L24-L34)）：

- **异步分块拷贝**：GMEM→LMEM，支持 1–5 维，带越界（OOB）钳位与常量填充（`cfill`）。
- **多播**：`cta_mask` 多于 1 位时，GMEM 只读一次，把 LMEM 写重放到多个共驻 CTA。
- **K-major 转置**：rank ≤ 2 时可把数据按 K-major 散布，直接产出 WGMMA 消费的 B-tile 布局。

关键约束：**DXA 是单向的 GMEM-read / LMEM-write，没有 software→global 路径**（[dxa_async_copy_multicast.md:34-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L34-L35)）。这正是 DMA 的典型形态——把全局数据搬进近存储的 LMEM，供后续计算（如 TCU）高速消费。

#### 4.1.2 核心流程

一次 DXA 拷贝的生命周期如下（对应设计文档 §4 的 9 步）：

```
┌─────────── kernel (warp) ───────────┐
│ 1. vx_dxa_issue_*  →  一条 custom0 指令
│    （发起 warp 立即获释，不等拷贝完成）
└───────────────┬─────────────────────┘
                │ SFU 路由 (DxaType)
┌───────────────▼─────────────────────┐  每核前端
│ 2. DxaUnit: 解码 4-lane wgather      │
│    → DxaReq (slot/bar/smem/coords/   │
│      cta_mask)                       │
└───────────────┬─────────────────────┘
                │ channel (DxaReq)
┌───────────────▼─────────────────────┐  集群引擎
│ 3. DxaCore: req_arb → req_queue(16)  │
│    → 读描述符表 → dispatch 给空闲 worker│
│ 4. setup: 枚举 work_list（每条=1次GMEM CL读）│
│ 5. gmem_req: 发 MemReq 读 GMEM（≤8 inflight）│
│ 6. smem_wr:   收 MemRsp → 拼 LMEM 写  │
│ 7. multicast: 若 popcount(cta_mask)>1, │
│      每个 receiver 重放一组 LMEM 写    │
└───────────────┬─────────────────────┘
                │ channel (MemReq, ST, LMEM)
┌───────────────▼─────────────────────┐  每核 LMEM
│ 8. LMEM 写入 + 完成回调:              │
│    最后一次写携带 notify_done →        │
│    barrier_event_release 解锁消费方    │
└─────────────────────────────────────┘
```

多播的核心数学：

- **数据重放**：第 \(r\) 个接收 CTA 的 LMEM 目标地址为
  \[
  \text{dest}[r] = \text{issuer\_smem} + r \cdot \text{smem\_stride}
  \]
  其中 `smem_stride` 由主机用 `set_multicast` 预先编程。
- **屏障定向**：每个接收 CTA 在自己的 `cta_no` 上等待同一个屏障号。接收 \(k\) 的完成通知发往
  \[
  \text{notify\_bar\_id}(k) = \text{raw\_bar\_id} + k
  \]
  低位 `cta_no` 加 \(k\) 即指向第 \(k\) 个接收 CTA（详见 4.4.3）。

#### 4.1.3 源码精读

DXA 的 ISA 入口编码为 RISC-V `custom0`、`funct7=0x3`，译码成 `INST_SFU_DXA`（[dxa_async_copy_multicast.md:40-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L40-L44)）。RTL 中 `VX_dxa_core` 每个 cluster 一个，把单个队列扇出到 `NUM_DXA_UNITS` 个 worker（[dxa_async_copy_multicast.md:74-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L74-L76)）。

配置规模由 `VX_config.toml` 决定（[dxa_async_copy_multicast.md:57-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L57-L60)）：`NUM_DXA_UNITS = max(1, ceil(NUM_CORES/8))`、队列深 16、最大 8 路 inflight、16 个描述符槽位。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立 DXA 端到端数据流的直觉。

1. 打开 [dxa_async_copy_multicast.md §4](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L95)，逐条对照上面的流程图阅读 9 个步骤。
2. 注意第 8 步「Multicast fan-out」：设计文档明确写出多播是**串行**的（`popcount(mask)` beats/word），这是当前实现的重要特征（§7 的「未实现」项里提到并行化是未来工作）。

**预期结果**：你能用自己的话回答「为什么发起 warp 不会被拷贝阻塞？」（答：SFU 把 `DxaReq` 推上 channel 后立即写回 trace、释放 warp，拷贝由 `DxaCore` 异步执行；同步交给软件侧的 `expect_tx` + `arrive_and_wait`）。

#### 4.1.5 小练习与答案

- **练习**：DXA 为什么不允许 software→global 方向？
- **答案**：DXA 的定位是「喂数据进 LMEM 供计算消费」的 DMA 引擎，GMEM 读 / LMEM 写单向。反向写全局会破坏缓存一致性模型，且与「LSU 是唯一访存功能单元」的职责划分冲突。

---

### 4.2 描述符与编程接口：主机 dxa.h + kernel vx_dxa.h

#### 4.2.1 概念说明

一次 DXA 拷贝的全部几何信息（基址、各维 size、行 stride、tile 大小、元素大小、布局、多播 stride……）打包在一个**描述符（descriptor）**里。描述符不随指令搬运，而是**主机预先通过 DCR 写进描述符表**（16 个槽位），kernel 指令只携带一个 `desc_slot` 索引。

因此编程接口天然分两层：

- **主机侧 `dxa.h`**：`vortex::dxa::program_{1..5}d` 用一组 `vx_dcr_write` 填描述符表；`set_multicast` 写多播 stride；`set_layout` 改目标布局。
- **kernel 侧 `vx_dxa.h`**：`vx_dxa_issue_{1..5}d_wg` / `..._multicast_wg` 发出那条 `custom0` 指令，把「槽位 + 屏障 + smem 地址 + 坐标 + cta_mask」打包进寄存器。

描述符的 `meta` 字段用几个子域编码 rank（DIM）、元素大小（ELEMSZ）、目标布局（LAYOUT），这些子域的位宽/位偏移定义在 [dxa_meta.h:21-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/dxa_meta.h#L21-L33)，主机编码器与 SimX 解码器共享同一份契约（注意 `SWIZZLE`/`INTERLEAVE`/`L2PROMO` 已分配但未接线，是预留位）。

#### 4.2.2 核心流程

**主机编程一次 2D 描述符**（以多播测试为例）：

```
slot 基址 = VX_DCR_DXA_DESC_BASE + slot * VX_DCR_DXA_DESC_STRIDE
依次写: BASE_LO, BASE_HI, SIZE0, SIZE1, STRIDE0, META, ESTRIDE*, TILESIZE*, CFILL
最后: set_multicast(slot, smem_stride_bytes)   // 写 SMEM_STRIDE_OFF
```

`meta` 由 `detail::pack_meta(rank, elem_size_enc)` 组装（[dxa.h:44-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L44-L47)），元素大小必须是 2 的幂（1/2/4/8 字节），编码成 log2。

#### 4.2.3 源码精读

主机侧 2D 描述符编程（多播测试用的就是它）：

[dxa.h:127-148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L127-L148) — `program_2d` 写 10 个 DCR，把 `base_addr`、两个 size、行 stride、`meta`、tile 大小、`cfill` 填入描述符槽位。

[dxa.h:260-265](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L260-L265) — `set_multicast` 单独写 `SMEM_STRIDE_OFF`，即上面公式里的 `smem_stride`。

kernel 侧的 issue 内联函数。先看指令如何把多个参数塞进两条源寄存器——靠的是 `vx_wgather` 把参数分发到 per-lane 寄存器槽位：

[vx_dxa.h:38-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L38-L46) — wgather 的 4-lane 编码约定：lane0=smem_addr、lane1=meta、lane2/3=坐标，1D/2D 时 rs2=x0，3D–5D 才需要第二个 wgather 承载 coord2..coord4。

`meta` 的打包（屏障 id 与槽位）：

[vx_dxa.h:48-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L48-L50) — `vx_dxa_pack_meta` 把 4 位 `desc_slot` 与屏障 payload 打包成 `meta`，放进 lane1。

最简单的 1D issue：

[vx_dxa.h:53-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L53-L67) — 组装一个 wgather 进 `a0`，发出 `.insn r custom0, 0, 0x3, x0, a0, x0`。注意是 `volatile` 且 clobber `"memory"`，防止编译器把这次有副作用的异步拷贝优化掉。

多播版的关键差异——`cta_mask` 走第二个 wgather 的 lane3：

[vx_dxa.h:161-180](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L161-L180) — 1D 多播 issue。第二个 wgather 把 `cta_mask` 放进 lane3，而 1D 的坐标本来就为 0，所以前三 lane 填 0。`cta_mask` 多于 1 位即触发多播。

#### 4.2.4 代码实践

**目标**：用源码确认「多播开关就是 `cta_mask` 的位数」。

1. 对比 [vx_dxa.h:53-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L53-L67)（普通 1D）与 [vx_dxa.h:161-180](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L161-L180)（多播 1D）：普通版只有 1 个 wgather、`cta_mask` 为 0；多播版多了第二个 wgather 承载 `cta_mask`。
2. 阅读 [dxa.h:99-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L99-L120)（`program_1d`），数一数它写几个 DCR，并解释为何 `base_addr` 必须用 `VX_MEM_PHYS` 分配（提示：见 [dxa.h:94-97](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L94-L97)，DXA 的 AXI master 绕过每核 MMU）。

**预期结果**：`cta_mask == 0`（或仅 1 位）即单播；`popcount(cta_mask) > 1` 即多播。这条判据会在 4.4 的 `DxaCore` 里再次出现。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `elem_bytes` 必须是 2 的幂？
- **答案**：因为它被编码成 `ELEMSZ` 的 log2（`elem_size_enc`，[dxa.h:54-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/dxa.h#L54-L60)），解码侧 `1u << enc` 还原（[dxa_core.cpp:255-259](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L255-L259)）。非 2 的幂无法用这个编码表示。
- **练习 2**：`smem_stride` 在 SimX 里会被对齐到什么粒度？为什么？
- **答案**：向上取整到 `VX_CFG_MEM_BLOCK_SIZE`（见 4.4.1）。因为 LMEM 模型把请求视为 block 对齐 + byteen 掩码，非对齐 stride 会截断地址、写进错误的 block。

---

### 4.3 设备侧前端 DxaUnit：4-lane 解码

#### 4.3.1 概念说明

`DxaUnit` 是 **SFU 的一个普通（非 SimObject）子单元**，每核一个，由 `SfuUnit` 持有（[dxa_unit.h:44-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.h#L44-L59)）。它不做任何真实的内存读写——它的唯一职责是**把那条 wgather 打包的 4-lane 指令解码成一个 `DxaReq` 包，推到 SFU 的出站 channel 上**，真正的搬运交给集群级的 `DxaCore`。

这种「前端只解码、后端才干活」的切分，正是 u6-l4 讲过的 SFU「分派器」本性：DXA 指令在 SFU 这一级是 fire-and-forget，warp 被立即释放。

#### 4.3.2 核心流程

```
trace->src_data[0] (rs1):  lane0=smem_addr, lane1=meta, lane2=coord0, lane3=coord1
trace->src_data[1] (rs2):  lane0=coord2,    lane1=coord3, lane2=coord4, lane3=cta_mask
        │
        ▼  拆解
DxaReq { core, uuid, wid, desc_slot=meta&0xf, bar_id=meta>>4 (raw),
         cta_mask, smem_addr, coords[5] }
        │
        ▼  req_out_.send(req)   （满了就返回 nullptr，下拍重试）
```

注意 `bar_id` 故意保留**原始编码态**，不在此处解码——因为多播的屏障定向算术 `bar_id + cta_idx` 依赖低位 `cta_no` 的编码形式（见 [dxa_unit.h:31-36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.h#L31-L36) 的注释）。

#### 4.3.3 源码精读

[dxa_unit.cpp:21-70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L21-L70) — `DxaUnit::process` 全貌。关键点：

- [dxa_unit.cpp:22-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L22-L24)：channel 满则返回 `nullptr`，SFU 调用方据此「下拍无副作用重试」——这是幂等背压。
- [dxa_unit.cpp:31-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L31-L43)：从 `src_data[0]`/`src_data[1]` 取出 4-lane 各字段，与 4.2 讲的 wgather 编码一一对应。
- [dxa_unit.cpp:44-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L44-L45)：`desc_slot = meta & 0x0f`（4 位）、`raw_bar = (meta >> 4) & 0x07ffffff`。
- [dxa_unit.cpp:60-63](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L60-L63)：注释点明屏障预注册是 kernel 的责任（`vx_barrier_expect_tx`），DXA 流水线只在完成时**发**释放事件。

SFU 侧的分发与背压处理：

[sfu_unit.cpp:565-572](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L565-L572) — `op_type == DxaType` 时调 `dxa_unit_->process(trace)`，返回 `nullptr` 就 `continue`（下拍重试），否则放行写回、释放 warp。

#### 4.3.4 代码实践

**目标**：验证 wgather 编码与 DxaUnit 解码的对称性。

1. 对照 [vx_dxa.h:38-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L38-L46)（编码端，kernel C++）与 [dxa_unit.cpp:31-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_unit.cpp#L31-L43)（解码端，SimX）。
2. 画一张表：`smem_addr/meta/coord0/coord1/coord2/coord3/coord4/cta_mask` 各自来自哪个 `src_data` 的哪个 lane。

**预期结果**：两侧 8 个字段的位置完全对齐——这是软硬契约的关键，错一格就会把 `cta_mask` 当成坐标、把坐标当成 smem 地址。

#### 4.3.5 小练习与答案

- **练习**：`process()` 返回 `nullptr` 时为何是「无副作用」？
- **答案**：它在 `req_out_.send(req)` 之前就 `return`，没有修改任何状态、没有推进 `ag_idx` 之类的游标。SFU 因此可以安全地每拍重试同一条 trace，直到 channel 不满。

---

### 4.4 集群引擎 DxaCore：GMEM 读 + 多播重放 + 完成通知

#### 4.4.1 概念说明

`DxaCore` 是 cluster 级共享的 `SimObject`（每个 `VX_cluster` 一个），是真正干活的后端引擎。它把每核 `DxaUnit` 推来的 `DxaReq` 汇聚、分发到若干 worker，每个 worker 跑一个**两步式**（flat）引擎：先把这次拷贝要读的 GMEM cache line 全部枚举进一个 `work_list`，再驱动「GMEM 读 → 收响应 → 拼 LMEM 写」的流水。

> 设计文档特别说明（[dxa_async_copy_multicast.md:137-147](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L137-L147)）：SimX 的 worker 是**预枚举 `work_list` 的 flat 两步引擎**，不是 RTL 那种逐阶段（setup/addr_gen/gmem_req/rsp_buf/smem_wr）对象。它匹配 RTL 的**行为**与三条 SimX 设计规则，但不追求逐模块结构对应。

`DxaCore` 的端口（[dxa_core.h:53-63](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.h#L53-L63)）：

- `dxa_req_in`（每核一个）：收 `DxaReq`。
- `gmem_req_out` / `gmem_rsp_in`（经 `gmem_arb_` 仲裁后接 L2）：读 GMEM。
- `lmem_req_out`（每核一个，只写）：写 LMEM，写完靠 channel 的 `tx_callback` 触发屏障释放。

所有读写都走真实 `MemReq`/`MemRsp` channel——LMEM 写还携带 `mem_block_t` 数据载荷与 `flags.dxa_notify_done`/`dxa_notify_bar_id` 标志，严格遵守基数规则。

#### 4.4.2 核心流程

`DxaCore::Impl::tick()` 每拍按**逆流水线顺序**排空（让每级读到上游本拍刚产出的数据，模拟正向流动，[dxa_core.cpp:205-243](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L205-L243)）：

```
每拍:
  1) 收 GMEM 响应 → 标记 inflight 槽 rsp_arrived
  2) 各 worker: smem_wr 排空 → gmem_req 发新读
  3) 从 queue_ 派发请求到空闲 worker (start_worker)
  4) 从每核 dxa_req_in 轮询拉新 DxaReq 进 queue_
```

一个 worker 的生命周期：

```
start_worker(req):
  - is_multicast = popcount(cta_mask) > 1
  - 若多播: cta_indices = cta_mask 中所有置位的位
  - smem_stride 向上对齐到 MEM_BLOCK_SIZE
  - enumerate_work_list(): 预枚举每条 GMEM CL 读 → work_list
  - work_list.back().last = true   // 最后一条携带完成标志

运行期 (每 worker 至多 VX_CFG_DXA_MAX_INFLIGHT=8 路在途):
  gmem_req: 取 work_list[ag_idx], 分配 inflight 槽, 发 MemReq 读 GMEM
            (OOB 行直接合成到达、不走总线, 用 cfill)
  smem_wr : 按发出顺序 (issued_order FIFO) 等响应到达
            → 把 scatter 元素聚成 block 写 → 发 LMEM MemReq
            → 多播: 对每个 cta_indices[mc_cta_idx] 在 +cta*smem_stride 重放
            → 最后一条 work 的最后一个 receiver 的最后一个 block 置 notify_done
```

#### 4.4.3 源码精读

**描述符与多播 stride 对齐**。[dxa_core.cpp:187-196](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L187-L196) — `SMEM_STRIDE` 写入时向上取整到 `kLmemWordSize`（= `VX_CFG_MEM_BLOCK_SIZE`），保证 `issuer_addr + r*smem_stride` 始终 block 对齐。调度器对每 CTA 的 LMEM 分配施以同样的对齐，使「接收方在各自 LMEM 的相同偏移处看到同一份数据」这个端到端契约成立。

**多播 setup**。[dxa_core.cpp:342-349](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L342-L349) — `is_multicast = popcount(cta_mask) > 1`，把 mask 中所有置位展开成 `cta_indices` 列表（0..31 扫一遍）。

**GMEM 读（gmem_req）**。[dxa_core.cpp:527-573](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L527-L573) — 每拍至多发一条 GMEM 读（注意是 `if` 不是 `while`，单端口单请求）。OOB 行直接合成「立即到达」、`rsp_data` 留空（后续用 `cfill` 填充），不产生总线流量（[dxa_core.cpp:545-553](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L545-L553)）。

**LMEM 写与多播重放（smem_wr）**。[dxa_core.cpp:579-700](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L579-L700) 是本讲最核心的一段。要点：

- [dxa_core.cpp:609-614](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L609-L614)：多播时本次写目标偏移 `cta_off = cta_indices[mc_cta_idx] * smem_stride`，这正是 §4.1.2 的重放公式 `dest[r] = issuer_smem + r·smem_stride`。
- [dxa_core.cpp:671-677](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L671-L677)：仅当「最后一条 work 的最后一个 scatter 元素的最后一个 receiver」时置 `dxa_notify_done`，并把 `notify_bar_id = raw_bar_id + cta_warp_idx`（多播时加接收者索引）。这是 §4.1.2 屏障定向公式的落点。
- [dxa_core.cpp:685-699](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L685-L699)：先推进 scatter 游标 `km_elem_idx`，再推进多播游标 `mc_cta_idx`，全部重放完才释放 inflight 槽——这是**串行多播**：同一份数据的每个 receiver 占用独立的写周期。

**屏障解码**。`notify_bar_id` 的解码在 [types.h:80-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L80-L84)：原始编码为 `cta_no(低8位) | (bar_no << 8)`，扁平化为 `cta_no * NUM_BARRIERS + bar_no`。于是 `raw_bar_id + k` 指向第 k 个接收 CTA 的同一个 `bar_no`——每个接收者各等各的屏障，互不串扰。

**接线与完成回调**（cluster 层）。[cluster.cpp:133-144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L133-L144) 把 `DxaCore` 的 GMEM 端口接到 L2 仲裁器的第 1 行；[cluster.cpp:146-153](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L146-L153) 把每核 SFU 的 `dxa_req_out` 绑到 `DxaCore::dxa_req_in[cid]`；[cluster.cpp:155-177](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L155-L177) 把 `lmem_req_out[cid]` 绑到该核 LMEM 的 `port_dxa` 端口，并挂一个 `tx_callback`：每当一笔带 `dxa_notify_done` 的写到达 LMEM，就调 `barrier_event_release(bar_decode_id(...))` 释放该核的屏障事件——这是 RTL `VX_dxa_completion` 嗅探 bank 写、推 txbar 的 SimX 对应物。

#### 4.4.4 代码实践

**目标**：跟踪一次多播写从 `DxaCore` 到屏障释放的完整路径。

1. 在 [dxa_core.cpp:671-677](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L671-L677) 确认 `notify_done` 只在「最后一个 receiver」置位——说明每个接收 CTA 各收到一次释放，且只释放一次。
2. 顺着 [cluster.cpp:168-175](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L168-L175) 的 `tx_callback` 看释放如何触发：`bar_decode_id` 把 `raw_bar_id + cta_warp_idx` 解码成扁平屏障号，再 `core->barrier_event_release(decoded)`。
3. 用一个 2 接收者的例子手算：`raw_bar_id = (bar_no << 8) | 0`，则接收 0 解码为 `0·NB + bar_no`，接收 1 解码为 `1·NB + bar_no`——两个不同的扁平屏障号，分别解锁两个 CTA。

**预期结果**：你理解了「一次 GMEM 读 → K 次 LMEM 写（串行重放）→ K 次屏障释放（每个接收者一次）」的多播落地表象。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `smem_wr` 要按 `issued_order`（FIFO）排空，而不是按响应到达顺序？
- **答案**：要保证最后一次写（携带 `notify_done`）确实是该 receiver 该 work 的真正最后一次写；同时保持写顺序确定，便于与 RTL 逐拍对齐（model_parity）。GMEM 响应可能乱序，但 LMEM 写必须有序。
- **练习 2**：OOB（越界）元素如何处理？会引发 GMEM 读吗？
- **答案**：不会。OOB 的 `LineWork` 在 gmem_req 阶段直接合成 `rsp_arrived=true`、不发 `MemReq`（[dxa_core.cpp:545-553](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L545-L553)）；smem_wr 阶段因 `rsp_data` 为空而用 `cfill` 常量填充（[dxa_core.cpp:661-664](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp#L661-L664)）。

---

### 4.5 多播同步契约：两栅栏惯用法与 C++ 帮助类

#### 4.5.1 概念说明

多播的难点不在数据搬运，而在**同步**：K 个接收 CTA 必须先各自 `expect_tx(1)` 预注册好「我要等一次到达」，再让**且仅让** rank-0 的那个 CTA 发射多播指令，否则要么死锁、要么过度释放屏障。`vx_dxa.h` 用一组 C++ 帮助类 `vortex::dxa_multicast_*d` 把这套「两栅栏惯用法」封装起来，让 kernel 作者无法手写出违反不变量的代码。

两个栅栏各司其职：

- **local_bar**（每 CTA 私有 `vortex::barrier`）：接收「我这一个 CTA 的多播到达事件」。构造时 `expect_tx(1)`，DXA 完成时释放一次。
- **group_bar**（跨 multicast group 的 `vortex::group_barrier`）：K 个成员在此会合，确保**每个接收者的 `expect_tx` 都已对硬件可见**后，rank-0 才开火。

> 关键契约（[vx_dxa.h:299-302](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L299-L302)）：调度器必须保证 K 个成员**共驻在一个核、占用从发起者起连续的 `cta_local_id` 槽位**。否则多播的 `bar_id + cta_idx` 定向与 `+cta·smem_stride` 偏移都会错位。这正是主机 launch 时必须设 `cluster_dim = K` 的原因。

#### 4.5.2 核心流程

```
构造 dxa_multicast_2d(slot, K, local_bar, group_bar):
  mask_ = (1 << K) - 1            // K 位全 1 的 cta_mask
  local_bar.expect_tx(1)          // 预注册一次到达事件

sync_and_issue(my_smem, coord0, coord1):
  group_bar.arrive_and_wait()     // K 个成员会合
  if (get_cluster_rank() == 0):   // 仅 rank-0 发射
     vx_dxa_issue_2d_multicast_wg(slot, local_bar.id(),
                                  my_smem, coord0, coord1, mask_)

// 该 CTA 的所有 warp（不只是 loader warp）：
local_bar.arrive_and_wait()       // 等到达==warps 且 events==0
```

`get_cluster_rank()` 返回 `CTA_ID % cluster_size`，对每个 cluster group 的发起者都是 0；若误用 `get_local_group_id()`（只有全局第一个 CTA 为 0），后续 cluster 会因没人发射而死锁——这是 [vx_dxa.h:325-327](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L325-L327) 注释反复强调的陷阱。

#### 4.5.3 源码精读

帮助类构造与 `expect_tx`：[vx_dxa.h:314-338](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L314-L338)（1D 版）——构造函数算出 `mask_ = (1<<num_members)-1` 并 `local_bar_.expect_tx(1)`。`sync_and_issue` 在 [vx_dxa.h:323-332](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L323-L332)。2D 版（多播测试用的）在 [vx_dxa.h:340-365](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L340-L365)，结构完全一致，只是坐标多一个。

真实 kernel 用法（dxa_copy_mcast 测试）：

[dxa_copy_mcast/kernel.cpp:16-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/kernel.cpp#L16-L56) — `num_recv` 个单 warp CTA 共驻一核，每个 CTA 经同一个多播取同一块 tile。`local_bar`（id=0）等本 CTA 的多播到达，`group_bar`（id=1，K=num_recv）做会合。loader warp 构造 `dxa_multicast_2d` 并 `sync_and_issue`，**所有 warp** 调 `local_bar.arrive_and_wait()` 才能继续（因为 `arrive_and_wait` 同时等到达数与 events 归零）。

主机侧关键设置：

[dxa_copy_mcast/main.cpp:124-131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/main.cpp#L124-L131) — `program_2d` 编程 tile 描述符，`set_multicast(slot, local_mem)` 把多播 stride 设为每 CTA 的 LMEM 大小（即接收 k 的区域起点在 `k*local_mem`）。

[dxa_copy_mcast/main.cpp:161-169](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/main.cpp#L161-L169) — launch 时**必须**显式设 `cluster_dim[0] = num_recv`，把 K 个 CTA 钉成一个 cluster group。注释解释了不设的后果：默认 cluster_size=1，每个 CTA 都以为自己是 rank-0 而各自开火，过度释放接收者的屏障、撑爆屏障号空间。

#### 4.5.4 代码实践

**目标**：跑通多播测试，观察「一份 tile 到 K 份 LMEM 副本」。

1. 在 build 目录运行（DXA 需显式开扩展）：
   ```bash
   ./ci/blackbox.sh --driver=simx --app=dxa_copy_mcast \
       CONFIGS="-DVX_CFG_EXT_DXA_ENABLE"
   ```
   `blackbox.sh` 会把 `--app=dxa_copy_mcast` 定位到 `tests/regression/dxa_copy_mcast`，其 Makefile 会自动补 `-DVX_CFG_EXT_DXA_ENABLE`（见该测试 `Makefile` 第 4 行）。
2. 程序默认用 `num_recv = VX_CFG_NUM_WARPS` 个单 warp CTA 共驻一核，每个接收者把各自 SMEM 副本写回 `dst[cta_id*...]`，主机校验所有接收者拿到**相同**的 tile（[dxa_copy_mcast/main.cpp:179-193](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/main.cpp#L179-L193)）。
3. 用 `--debug=3 --perf=6` 重跑（参见 `ci/testcases/dxa.yaml` 中 `dxa_copy-1` 的 flags），在 trace 里找到 `dxa-unit submit` 与 `complete` 行，确认一次 submit 对应 K 个接收者的完成事件。

**需要观察的现象**：程序打印 `PASSED`（退出码 0），说明 K 个接收 CTA 都从同一次 GMEM 读得到了相同的 tile 副本。

**预期结果（待本地验证）**：在默认 `NUM_WARPS`（如 4）下，`num_recv=4`，4 个 CTA 各得到同一份 16×4 的 tile。若误删 `main.cpp:167-168` 的 `cluster_dim`，预期会观察到屏障过度释放导致的错误或死锁。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `local_bar.arrive_and_wait()` 必须由**所有 warp** 调用，而不能只在 loader warp 里调？
- **答案**：`arrive_and_wait` 同时等待「到达数 == 该 CTA 的 warp 数」**和**「events == 0」。只让 loader warp 调用，到达数永远凑不齐，CTA 会死锁（[vx_dxa.h:294-297](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_dxa.h#L294-L297)）。
- **练习 2**：`sgemm2_dxa_mcast` 里 A 用普通 issue、B 用多播 issue（[sgemm2_dxa_mcast/kernel.cpp:59-62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/sgemm2_dxa_mcast/kernel.cpp#L59-L62)），为什么？
- **答案**：每个 CTA 算 C 的一行，A 的一行是**每 CTA 私有**（各取各行），所以用普通 `vx_dxa_issue_2d_wg`；而 B 的一个列块被同列的 K 个 CTA **共享**，所以用多播读一次、分发 K 份，省下 K-1 次重复 GMEM 读——这正是多播的收益场景。

---

## 5. 综合实践

**任务**：用 `dxa_copy_mcast` 和 `sgemm2_dxa_mcast` 两个测试，把本讲的知识串成一条完整的「主机编程 → kernel 发射 → 引擎搬运 → 多播重放 → 屏障释放」追踪链。

步骤：

1. **主机侧**（读 [dxa_copy_mcast/main.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/main.cpp)）：列出 `program_2d` 写了哪些描述符字段、`set_multicast` 设的 stride 值是多少、`cluster_dim` 为何必须等于 `num_recv`。
2. **kernel 侧**（读 [dxa_copy_mcast/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/dxa_copy_mcast/kernel.cpp)）：画出 `local_bar`/`group_bar` 的时序——构造（expect_tx）→ group 会合 → rank-0 issue → 全员 arrive_and_wait。
3. **引擎侧**（读 [dxa_core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.cpp)）：对一次 4 接收者的多播，标注 `cta_indices`、每个 receiver 的 `cta_off`、以及哪一次写置 `notify_done`、`notify_bar_id` 各是多少。
4. **运行验证**：
   ```bash
   ./ci/blackbox.sh --driver=simx --app=dxa_copy_mcast
   ./ci/blackbox.sh --driver=simx --app=sgemm2_dxa_mcast
   ```
   两个都应 `PASSED`。
5. **进阶思考**：把 `--perf=6`（DXA 性能类，[dxa_async_copy_multicast.md:61-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L61-L64)）打开，读 `DxaCore::PerfStats`（[dxa_core.h:34-49](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dxa/dxa_core.h#L34-L49)）里的 `gmem_reads` / `gmem_dedup` / `lmem_writes`，验证「多播时 `lmem_writes ≈ K × gmem_reads`」——即一次 GMEM 读被重放成 K 次 LMEM 写。

> 若无法在本地运行，步骤 1–3 的源码追踪部分仍可独立完成，相关结论标注「待本地验证」即可。

---

## 6. 本讲小结

- **DXA 是单向异步 DMA**：一条 warp 指令发起 GMEM→LMEM 的分块拷贝，warp 立即获释，完成靠 tx 事件释放屏障；没有 software→global 路径。
- **描述符是真相、指令是索引**：主机用 `dxa.h` 的 `program_Nd`/`set_multicast` 预写描述符表，kernel 用 `vx_dxa_issue_*` 只发槽位号 + 坐标 + cta_mask。
- **前端只解码、后端才搬运**：每核 `DxaUnit` 把 4-lane wgather 解码成 `DxaReq`（幂等背压），集群级 `DxaCore` 才真正读 GMEM、写 LMEM。
- **多播 = 读一次 + 串行重放 K 份**：`cta_mask` 多于 1 位触发多播，第 r 个接收者写到 `issuer_smem + r·smem_stride`，屏障定向用 `raw_bar_id + r` 解码。
- **完成通知走 channel 回调**：最后一次 LMEM 写带 `notify_done`，cluster 的 `tx_callback` 据此 `barrier_event_release`，对应 RTL 的 `VX_dxa_completion`。
- **同步靠两栅栏惯用法**：`vortex::dxa_multicast_*d` 帮助类用 `local_bar`（expect_tx）+ `group_bar`（会合）封装「仅 rank-0 开火、全员等待」的不变量，依赖 K 个 CTA 共驻连续槽位（`cluster_dim`）的调度契约。

---

## 7. 下一步学习建议

- **DXA↔TCU 衔接**：本讲的 K-major 转置布局直接产出 WGMMA 的 B-tile。建议接着读 [tensor_core_wgmma_engine.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md) 与 `tests/regression/sgemm_tcu_wg_dxa_mcast`，理解「DXA 多播喂 B-tile → TCU WGMMA 消费」的端到端流水。
- **RTL 一侧**：本讲聚焦 SimX。要对照硬件实现，读 [`hw/rtl/dxa/`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/dxa/) 下的 `VX_dxa_core.sv`/`VX_dxa_worker.sv`/`VX_dxa_smem_wr.sv`/`VX_dxa_completion.sv`，体会设计文档 §3 列出的逐模块对应，并回到 u7-l4 的 model_parity 纪律。
- **未实现方向**：[dxa_async_copy_multicast.md §7](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/dxa_async_copy_multicast.md#L168-L207) 列出了「per-socket 迁移」「多播并行化（bank-broadcast 取代串行重放）」「SimX 5 阶段对象化」等未来工作，是做二次开发（u14-l3）的好选题。
- **屏障机制深挖**：若对 `expect_tx`/`arrive_and_wait`/`bar_decode_id` 的底层仍有疑问，可回看 u4-l3（设备侧 barrier 与系统调用）与 `sim/simx/barrier_unit.cpp`。
