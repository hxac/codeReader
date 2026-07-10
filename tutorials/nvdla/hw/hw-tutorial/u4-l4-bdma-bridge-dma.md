# BDMA：桥 DMA 与存储间搬运

> 承接：本讲建立在 [u4-l1 存储接口架构总览](u4-l1-memif-architecture.md) 之上。你已经知道 NVDLA 有两套同构的存储接口——MCIF（接片外主存 DBB）与 CVIF（接片上 CVSRAM），二者同例化于中央枢纽 `partition_o`。本讲的主角 BDMA（Bridge DMA）正是唯一一个「同时站在两套接口之间」的引擎：它把数据从一套存储搬到另一套存储，全程不需要 CPU 逐字节插手。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 BDMA 在 NVDLA 中的定位：它是一个独立的、描述符驱动的「桥 DMA」，用于在 DBB 与 CVSRAM 之间批量搬运数据，把 CPU 从内存拷贝里解放出来。
- 画出 BDMA 的内部数据通路：`load`（取数）→ `cq`（命令队列）→ `store`（写数），以及它们如何经 `MCIF`/`CVIF` 访问两套存储。
- 解释 BDMA 如何用 `src_ram_type`/`dst_ram_type` 两个比特选择源端与目的端分别走哪套存储接口。
- 读懂 BDMA 的 3D 搬运模型（cube→surface→line→transaction），知道地址如何按 `line_stride`/`surf_stride` 步进。
- 掌握 BDMA 的编程模型：写一组配置寄存器 → 写 `OP_ENABLE` 入队 → 写 `LAUNCH0/1` 启动并归属到 group 0/1。
- 追踪一次搬运完成后，`done` 信号如何从 `store` 一路上报到 GLB 的 `done_source`。

## 2. 前置知识

- **DMA（Direct Memory Access）**：一种「不经过 CPU、由专用硬件搬运数据」的机制。CPU 只需把「源地址、目的地址、长度」写成几条配置，DMA 引擎就会自己把数据搬完。
- **描述符（descriptor）**：把一次搬运所需的全部参数（源/目的地址、大小、步长等）打包成一个「数据包」。CPU 一次写一串寄存器就等于「填好一张描述符」。
- **MCIF / CVIF / DBB / CVSRAM**：见 [u4-l1](u4-l1-memif-architecture.md)。MCIF 挂片外主存 DBB（大但慢），CVIF 挂片上 CVSRAM（小但快）。BDMA 是它们之间的「搬运工」。
- **CSB 寄存器配置总线**：见 [u2-l1 CSB 总线协议与 apb2csb 桥](u2-l1-csb-bus-apb2csb.md)。CPU 通过 CSB 读写各引擎的寄存器。
- **GLB 中断聚合**：见 [u2-l4 GLB 全局配置与中断聚合](u2-l4-glb-config-interrupts.md)。每个引擎做完事会向 GLB 上报 `done`，GLB 把 16 路中断源聚合成 `done_source[15:0]`。

> 一句话直觉：**CPU 不亲自搬数据，它只填「搬家单」；BDMA 按「搬家单」把数据从 DBB 搬进 CVSRAM（或反向），搬完按一声门铃（done 中断）。**

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `vmod/nvdla/bdma/NV_NVDLA_bdma.v` | BDMA 顶层，例化 `csb/gate/load/store/cq` 五个子模块并对外连出 MCIF/CVIF 接口。 |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v` | 配置与控制中枢：寄存器文件、命令 FIFO（`csb_fifo`）、`OP_ENABLE`/`LAUNCH` 逻辑、done 中断上报。 |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v` | 由 RDL/Ordt 自动生成的寄存器文件：地址译码、字段拼装、读写接口。 |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v` | **取数引擎**：消费描述符、游走源端 3D 地址、向 MCIF/CVIF 发读请求、把目的端上下文写进 `cq`。 |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_cq.v` | **命令队列**：load 与 store 之间的 20 深上下文 FIFO，解耦取数与写数。 |
| `vmod/nvdla/bdma/NV_NVDLA_BDMA_store.v` | **写数引擎**：接收读返回数据、游走目的端地址、向 MCIF/CVIF 发写请求、产生 done。 |
| `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v` | 中央配置路由器，给出 BDMA 的基址 `0x4000`（承接 [u2-l2](u2-l2-csb-master-router.md)）。 |
| `vmod/nvdla/glb/NV_NVDLA_GLB_ic.v` | GLB 中断控制器，BDMA 的 done 占 `done_source[7:6]`（承接 [u2-l4](u2-l4-glb-config-interrupts.md)）。 |

> 命名约定：`xxx2cvif_*` 表示「这次访问走 CVSRAM（secondary memif）」，`xxx2mcif_*` 表示「这次走 DBB（primary memif）」。BDMA 顶层同时有 `bdma2mcif_rd/wr` 与 `bdma2cvif_rd/wr` 四组接口，因为它的源端和目的端可以分别落在任意一套存储上。

## 4. 核心概念与源码讲解

### 4.1 BDMA 是什么：桥 DMA 的定位与顶层结构

#### 4.1.1 概念说明

NVDLA 的计算引擎（CDMA/CSC/CMAC/...）都只关心「算」，数据怎么进、怎么出由各自的 DMA 子模块经 MCIF/CVIF 搬运。但有一类工作纯粹是「搬家」：例如推理开始前，把权重从片外 DRAM（DBB）预取到片上 CVSRAM，让后续卷积以低延迟读取。这种「两套存储之间」的搬运如果交给 CPU 逐次编程各引擎 DMA，既慢又繁琐。

BDMA 就是为这类需求设计的**独立搬运引擎**：

- 它不参与任何计算，只做 `存储 → 存储` 的拷贝。
- 它同时挂在 MCIF 和 CVIF 上，源端、目的端可各自二选一（DBB 或 CVSRAM），因此能实现 DBB→CVSRAM、CVSRAM→DBB、甚至 DBB→DBB、CVSRAM→CVSRAM 四种组合。
- 它是**描述符驱动**的：CPU 填好「搬家单」就放手，BDMA 自己排着队把一串搬运任务做完，每完成一批按一次 done。

#### 4.1.2 顶层结构

BDMA 顶层 `NV_NVDLA_bdma` 例化 5 个子模块，构成一条「取数—缓冲—写数」的流水：

```
            CSB 配置
               │
               ▼
        ┌─────────────┐
        │   u_csb     │  寄存器 + 命令FIFO(csb_fifo) + 启动/中断逻辑
        └─────┬───────┘
              │ 描述符(csb2ld_*)
              ▼
   读请求  ┌─────────────┐  目的端上下文(ld2st_wr_*)
 MCIF/CVIF │   u_load    │ ───────────────────┐
 ◄──────── │  (取数)     │                    │
   读返回  │             │                    ▼
 ────────► └─────────────┘            ┌─────────────┐
              读返回(mcif/cvif2bdma)   │   u_cq      │ 20深命令队列
              ───────────────────────► │ (上下文FIFO)│
                                       └─────┬───────┘
                                             │ ld2st_rd_*
                                             ▼
   写请求                            ┌─────────────┐
 MCIF/CVIF ◄──────────────────────── │  u_store    │  (写数) + 产生done
   写完成  ────────────────────────► │             │
                                     └─────┬───────┘
                                           │ st2csb_grp0/1_done
                                           ▼
                                     (回 u_csb → bdma2glb_done_intr)
```

要点：

1. `u_load` 负责**读源端**：它消费描述符、游走源地址、向 MCIF 或 CVIF 发读请求。
2. `u_store` 负责**写目的端**：它接收读返回的数据、游走目的地址、向 MCIF 或 CVIF 发写请求，并在写完后产生 done。
3. `u_cq`（context queue）夹在中间：因为「读」和「写」速度不一致，需要一个队列把「这个命令的目的端该怎么写」缓存起来，等 store 慢慢消费。
4. `u_csb` 是大脑：寄存器、入队、启动、done 上报都在这里；`u_gate` 做时钟门控（空闲关钟省电，原理见 [u6-l1](u6-l1-clock-reset-car.md)）。

#### 4.1.3 源码精读

顶层端口清楚展示了 BDMA「同时拥有四组存储接口」的身份——两组读、两组写，分别面向 MCIF 与 CVIF，外加 CSB 配置口与 done 中断输出：

[NV_NVDLA_bdma.v:L11-L46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_bdma.v#L11-L46) — 顶层端口：`bdma2mcif_rd_*` / `bdma2cvif_rd_*`（两路读请求）、`bdma2mcif_wr_*` / `bdma2cvif_wr_*`（两路写请求）、`mcif2bdma_*` / `cvif2bdma_*`（读返回与写完成）、`csb2bdma_*`/`bdma2csb_*`（配置）、`bdma2glb_done_intr_pd[1:0]`（完成中断）。

顶层例化五个子模块，关键连线一目了然：load 既向存储发读请求，又把目的端上下文写进 cq；store 从 cq 取上下文、向存储发写请求：

[NV_NVDLA_bdma.v:L190-L225](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_bdma.v#L190-L225) — `u_load` 实例：`bdma2mcif/cvif_rd_req_*` 发读请求，`ld2st_wr_*` 把目的端上下文推进 cq。

[NV_NVDLA_bdma.v:L227-L257](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_bdma.v#L227-L257) — `u_store` 实例：接收 `mcif/cvif2bdma_rd_rsp_*` 读返回，发 `bdma2mcif/cvif_wr_req_*` 写请求，输出 `st2csb_grp0/grp1_done`。

[NV_NVDLA_bdma.v:L259-L270](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_bdma.v#L259-L270) — `u_cq` 实例：左侧 `ld2st_wr_*`（load 写入），右侧 `ld2st_rd_*`（store 读出）。

BDMA 在顶层 `partition_o` 中被例化为 `u_NV_NVDLA_bdma`，其 done 中断接到 GLB：

[NV_NVDLA_partition_o.v:L2294-L2306](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2294-L2306) — BDMA 在中央枢纽 partition_o 内例化，`bdma2glb_done_intr_pd` 连到 GLB。

#### 4.1.4 代码实践

1. **目标**：确认 BDMA 的「四接口」身份与它在地址空间中的位置。
2. **步骤**：
   - 在 `NV_NVDLA_bdma.v` 中数一数顶层与 MCIF、CVIF 相关的端口各有几组（读请求、写请求、读返回、写完成）。
   - 打开 `csb_master.v`，找到 BDMA 的地址译码。
3. **观察**：
   - 顶层应有 mcif 读写、cvif 读写共四组数据通路端口。
   - 地址译码里 BDMA 的基址。
4. **预期结果**：`select_bdma = ((core_byte_addr & addr_mask) == 32'h00004000)` 表明 **BDMA 寄存器页基址为 0x4000**（csb_master.v 第 1422 行）。
5. 此为静态阅读，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：BDMA 顶层为什么需要同时有 `bdma2mcif_*` 和 `bdma2cvif_*` 两组接口，而不是只有一组？

**参考答案**：因为 BDMA 的源端和目的端可以各自独立地落在 DBB 或 CVSRAM 上。只有同时挂两组接口，才能实现 DBB↔CVSRAM（以及 DBB↔DBB、CVSRAM↔CVSRAM）之间的任意搬运。

**练习 2**：BDMA 的 done 中断是几位？为什么是这么多位？

**参考答案**：`bdma2glb_done_intr_pd[1:0]` 共 2 位。因为 BDMA 有两个组（group 0 / group 1，即影偶双缓冲），两位分别表示「group 0 这批搬完了」和「group 1 这批搬完了」，让 CPU 能区分是哪一组完成。

---

### 4.2 load/store 搬运通路与 3D 地址游走

#### 4.2.1 概念说明

BDMA 不是只能搬一段连续内存。它支持**三维搬运**，可以把一个「立方体」形状的数据块从源搬到目的：

- 一个 **cube**（立方体）由多个 **surface** 组成；
- 一个 surface 由多根 **line** 组成；
- 一根 line 由多个 **transaction**（一次 DMA 请求）组成。

对应的配置参数（在描述符里）：

| 参数 | 含义 |
| --- | --- |
| `line_size` | 一根 line 的长度，以 **32 字节**为单位（一次 transaction 就搬一根 line）。 |
| `line_repeat_number` | 一个 surface 里有几根 line（计数从 0 开始，0 表示只有 1 根）。 |
| `surf_repeat_number` | 一个 cube 里有几个 surface（0 表示只有 1 个）。 |
| `src_line_stride` / `dst_line_stride` | 源/目的端相邻两根 line 起始地址之间的字节距离。 |
| `src_surf_stride` / `dst_surf_stride` | 源/目的端相邻两个 surface 起始地址之间的字节距离。 |

这套模型正好契合张量的存储布局：line = 一行像素，surface = 一个通道平面，cube = 多通道。源端和目的端的 stride 可以不同，于是 BDMA 还能在搬运的同时做**布局转换**（如把紧凑排列的数据散开，或反过来）。

#### 4.2.2 核心流程

源端地址的游走（在 `u_load` 内）按下面伪代码推进：

```
addr  = src_base            # 起始：源基址
saddr = src_base
for surf in 0 .. surf_repeat_number:        # 遍历 surface
    for line in 0 .. line_repeat_number:    # 遍历 line
        发一次读请求(addr, size = line_size * 32B)
        addr = addr + src_line_stride        # 跳到下一根 line
    saddr = saddr + src_surf_stride          # 跳到下一个 surface
    addr  = saddr
```

三个结束判断层层嵌套（见源码）：`is_last_req_in_line` 恒为 1（一根 line = 一次请求）；`is_surf_end` = 一根 line 搬完且 line 计数到顶；`is_cube_end` = 一个 surface 搬完且 surf 计数到顶。

> 一个值得注意的设计约束（源码里有一条断言）：一根 line 的实际字节长度 `line_size<<5` 必须不大于 `line_stride`，否则相邻两根 line 会**重叠**，断言会报错。即同一命令内不允许 line 之间互相覆盖。

#### 4.2.3 源码精读

`u_load` 把高/低地址与步长拼装成 64 位字节地址，低 5 位恒 0（32 字节对齐）：

[NV_NVDLA_BDMA_load.v:L283-L284](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v#L283-L284) — `reg2dp_dst_addr = {dst_addr_high_v8, dst_addr_low_v32, 5'd0}`，源端同理。故字节地址 = `{高32位, 低27位, 5'b0}`，低地址按 32 字节粒度。

3D 模型的层级定义与三个结束标志：

[NV_NVDLA_BDMA_load.v:L391-L397](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v#L391-L397) — cube←surf←line←tran 的嵌套定义，以及 `is_surf_end`/`is_cube_end` 的逐层与逻辑。

地址游走状态机：`line_addr` 每搬完一根 line 加 `line_stride`，到 surface 末尾时 `surf_addr` 加 `surf_stride` 并把 `line_addr` 拉回新 surface 起点：

[NV_NVDLA_BDMA_load.v:L399-L435](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v#L399-L435) — `tran_addr`（每次请求的地址）取自 `line_addr`；搬完一根 line 后 `line_addr += reg_line_stride`，surface 末尾时回跳到 `surf_addr + reg_surf_stride`。

每次读请求的大小就是 `line_size`（以 32 字节块计），并由 `src_ram_type` 决定向哪套存储发请求：

[NV_NVDLA_BDMA_load.v:L476-L491](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v#L476-L491) — `dma_rd_req_type = reg_cmd_src_ram_type`；`type==0` 走 CVIF、`type==1` 走 MCIF。

> ⚠️ **关于 ram_type 的取值，请以源码为准**：本仓库 RTL 中 `ram_type == 0` 对应 **CVIF（CVSRAM）**、`ram_type == 1` 对应 **MCIF（DBB）**，读写两侧一致（store 侧见 4.3 节）。这与某些版本的对外寄存器文档措辞相反——阅读源码时一律以这两处 `cv_*_req_vld = ... & (type==0)` / `mc_*_req_vld = ... & (type==1)` 的译码为准。

写数侧 `u_store` 用同样的方式按 `dst_ram_type` 选择写往哪套存储，并接收读返回数据：

[NV_NVDLA_BDMA_store.v:L684-L689](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_store.v#L684-L689) — `dma_wr_req_ram_type = reg_cmd_dst_ram_type`；`type==0` 写 CVIF、`type==1` 写 MCIF。

#### 4.2.4 代码实践

1. **目标**：用一个具体数字，把 3D 搬运的地址游走算清楚。
2. **步骤**：假设要从 DBB 把一个 `64 行 × 64 字节` 的二维数据块搬到 CVSRAM，源基址 `src_base = 0x1000_0000`，目的基址 `dst_base = 0x0001_0000`（CVSRAM 内），行间无空隙。
3. **推算**：
   - 一根 line = 64 字节 = 2 个 32 字节块，故 `line_size = 2`。
   - 共 64 行，故 `line_repeat_number = 63`（从 0 计数）。
   - 只有 1 个 surface，故 `surf_repeat_number = 0`。
   - 行间 64 字节，故 `src_line_stride = dst_line_stride = 64`。
   - `src/dst_surf_stride` 无关紧要（只 1 个 surface）。
4. **观察**：在源码中确认 `line_size` 以 32 字节为单位、`line_stride` 以字节为单位（低 5 位会被丢弃）。
5. **预期结果**：`line_size=2`、`line_repeat_number=63`、`surf_repeat_number=0`、stride=64 是一组自洽的配置；断言 `(line_size<<5) > line_stride`（即 `64 > 64`）不成立，不会触发 line 重叠告警。
6. 实际效果「待本地验证」（需接入 MCIF/CVIF 仿真模型跑 trace）。

#### 4.2.5 小练习与答案

**练习 1**：如果只想搬一段连续的 4KB 数据，`line_repeat_number` 和 `surf_repeat_number` 该填多少？

**参考答案**：分别填 `0` 和 `0`——即 1 个 surface、每个 surface 1 根 line，整个搬运退化为单次 transaction，`line_size = 4096/32 = 128`。

**练习 2**：源端的 `src_line_stride` 和目的端的 `dst_line_stride` 为什么是两个独立参数？

**参考答案**：因为源和目的的存储布局可能不同。例如源在 DBB 里行间有 padding（stride 较大），而搬到 CVSRAM 后想紧凑排列（stride 较小）。两个 stride 独立可配，BDMA 就能在搬运的同时重排布局。

---

### 4.3 命令队列 cq：解耦取数与写数

#### 4.3.1 概念说明

「读源端」和「写目的端」是一对速度不匹配的操作：MCIF 读 DBB 可能要等很多拍，CVIF 写 CVSRAM 又可能被反压。如果 load 必须等 store 写完才能取下一根数据，整条通路就会时快时慢、效率低下。

`u_cq`（context queue）就是用来**解耦**二者的：load 每开始搬一个命令，就把「这个命令的目的端该怎么写」打包成一个上下文条目塞进 cq；store 再按自己的节奏从 cq 里取出上下文、配合读返回的数据去写目的端。这样 load 可以一路向前取数，store 在后面慢慢写，二者只需通过 cq 的「满/空」做背压。

注意区分 NVDLA 里**两个**不同的「命令队列」：

| 队列 | 位置 | 宽度/深度 | 作用 |
| --- | --- | --- | --- |
| `csb_fifo` | `u_csb` 内 | 289 位 × 20 深 | 缓存 CPU 已入队、尚未被 load 消费的**完整描述符**（源+目的全部参数）。 |
| `cq` (`u_cq`) | load 与 store 之间 | 161 位 × 20 深 | 缓存已开始取数、尚未写完的**目的端上下文**（地址、步长、ram_type、中断标志等）。 |

#### 4.3.2 核心流程

cq 本质是一个 20 深、161 位宽的同步 FIFO：

```
load  ──ld2st_wr_pvld/prdy/pd(161b)──►  [ cq: 20 项 ]  ──ld2st_rd_pvld/prdy/pd(161b)──►  store
```

- **写侧**（`ld2st_wr_*`）由 load 驱动：load 每消费一个描述符、开始取数，就向 cq 写入这个命令的目的端上下文。
- **读侧**（`ld2st_rd_*`）由 store 驱动：store 取出上下文后，用它配合读返回数据组织写请求。
- 两侧各有计数与背压：写满（count 到上限）时 `ld2st_wr_prdy` 拉低反压 load；读空时 `ld2st_rd_pvld` 拉低让 store 等待。

161 位上下文里装了什么？正是 store 写数所需的全部信息（见 load 的打包代码）：目的地址、line_size、src/dst ram_type、中断标志与中断组指针、目的端 line/surf stride、line/surf 重复数。

#### 4.3.3 源码精读

cq 的模块接口只有一对写口、一对读口加空闲指示：

[NV_NVDLA_BDMA_cq.v:L13-L36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_cq.v#L13-L36) — `ld2st_wr_pvld/prdy/pd`（写侧，来自 load）与 `ld2st_rd_pvld/prdy/pd`（读侧，去往 store），加 `ld2st_wr_idle`。

cq 用一块 20 深、161 位的「触发器 RAM」存放数据（仿真与综合时这种小深度 RAM 用触发器展开实现，便于时序与可综合性）：

[NV_NVDLA_BDMA_cq.v:L165-L175](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_cq.v#L165-L175) — 例化 `NV_NVDLA_BDMA_cq_flopram_rwsa_20x161`（20 行 × 161 位），`di` 接写数据、`dout` 出读数据，地址 5 位（0–19）。

[NV_NVDLA_BDMA_cq.v:L558-L577](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_cq.v#L558-L577) — 该 flop RAM 内部用 `ram_ff0`…`ram_ff19` 共 20 组 161 位寄存器展开，按写地址 `wa` 选通写入、按读地址 `ra` 多路选择读出。

load 侧把目的端上下文按固定比特位置打包进 161 位写数据：

[NV_NVDLA_BDMA_load.v:L379-L388](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_load.v#L379-L388) — `ld2st_wr_pd` 的打包：`[63:0]`=目的地址、`[76:64]`=line_size、`[77]`=src_ram_type、`[78]`=dst_ram_type、`[79]`=中断使能、`[80]`=中断组指针、随后是 stride 与 repeat。

store 侧按相同位置解包，并据此组织写请求、判定中断归属：

[NV_NVDLA_BDMA_store.v:L420-L435](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_store.v#L420-L435) — `ld2st_cmd_src/dst_ram_type`、`ld2st_cmd_interrupt`、`ld2st_cmd_interrupt_ptr` 从 `ld2st_rd_pd` 解出，锁存进 `reg_cmd_*` 供写通路使用。

#### 4.3.4 代码实践

1. **目标**：理解 cq 的「满」如何反压 load，从而体会队列的解耦作用。
2. **步骤**：
   - 阅读 cq 写侧的计数逻辑：`ld2st_wr_count`（5 位）与 `ld2st_wr_busy_next`。
   - 找到写侧满阈值（`wr_count_next == 20` 或 `wr_limit_reg`）如何驱动 `ld2st_wr_prdy`。
3. **观察**：当 load 写入速度超过 store 读出速度，`ld2st_wr_count` 会增长；到达 20 时 `ld2st_wr_prdy` 拉低，load 被迫暂停取数。
4. **预期结果**： cq 深度 20 = 同一时刻最多可有约 20 个命令「在途」（已取数、未写完）。这与 `csb_fifo` 的深度 20 一致，是 BDMA 流水深度的上限。
5. 此为源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`csb_fifo` 和 `cq` 都是 20 深，它们装的东西有何本质区别？

**参考答案**：`csb_fifo` 装的是**尚未开始**的完整描述符（源+目的全部参数，289 位），等 load 来消费；`cq` 装的是**已经开始取数但还没写完**的命令的目的端上下文（161 位），等 store 来消费。一个在「入口」排队，一个在「中间」排队。

**练习 2**：为什么 cq 里只存「目的端上下文」而不存源端参数？

**参考答案**：因为源端参数只在 load 取数时用一次——load 消费完描述符、发完读请求后源端参数就不再需要。而目的端参数必须等到「对应的读数据回来」之后，store 才能用它组织写请求；这段时间差正是 cq 存在的意义。

---

### 4.4 寄存器编程模型与 done 中断上报

#### 4.4.1 概念说明

BDMA 的寄存器页基址为 **0x4000**（由 csb_master 译码确定）。它属于「描述符 + 双组启动」式编程，而不是 CDMA/CSC 那种逐字段影偶寄存器（见 [u2-l3](u2-l3-register-files-shadow-config.md)）。完整的一次编程分三步：

1. **填描述符**：写一组配置寄存器（源/目的地址、大小、步长、ram_type）。
2. **入队**：写 `OP_ENABLE`（`CFG_OP_0`，偏移 0x30）为 1——这一写会把当前寄存器值**快照**成一个 289 位描述符，压入 `csb_fifo`（深 20）。可连续填多张描述符。
3. **启动**：写 `LAUNCH0`（偏移 0x34，group 0）或 `LAUNCH1`（偏移 0x38，group 1）为 1——把自上次启动以来入队的所有描述符作为一个**批次**交给引擎执行，并打上 group 标签。

关键寄存器一览（偏移相对 BDMA 基址 0x4000）：

| 偏移 | 寄存器 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0x00 | SRC_ADDR_LOW_0 | v32（[31:5]） | 源地址低 32 位，32 字节对齐。 |
| 0x04 | SRC_ADDR_HIGH_0 | v8（[31:0]） | 源地址高 32 位。 |
| 0x08 / 0x0c | DST_ADDR_LOW/HIGH_0 | 同上 | 目的地址（同结构）。 |
| 0x10 | LINE_0 | size[12:0] | 一根 line 长度，**32 字节为单位**。 |
| 0x14 | CMD_0 | `{dst_ram_type[1], src_ram_type[0]}` | 源/目的选哪套存储（见 4.2 警告）。 |
| 0x18 | LINE_REPEAT_0 | number[23:0] | 每 surface 的 line 数 − 1。 |
| 0x1c / 0x20 | SRC/DST_LINE_0 | stride | 行间字节步长。 |
| 0x24 | SURF_REPEAT_0 | number[23:0] | 每 cube 的 surface 数 − 1。 |
| 0x28 / 0x2c | SRC/DST_SURF_0 | stride | 面间字节步长。 |
| 0x30 | OP_0 | en[0] | 写 1 = 入队一张描述符。 |
| 0x34 / 0x38 | LAUNCH0/1_0 | grp_launch[0] | 写 1 = 启动并归属 group 0/1。 |
| 0x40 | STATUS_0（只读） | free_slot/idle/grp0_busy/grp1_busy | 空闲槽位、各 group 忙闲。 |

`free_slot` 是个很实用的字段：`free_slot = 20 − csb_fifo_wr_count`，告诉 CPU「还能再入队几张描述符」。

#### 4.4.2 核心流程

完成中断的产生与上报链路：

```
store 写完一批的最后一拍
   └─► st2csb_grp0_done 或 st2csb_grp1_done   （按 interrupt_ptr 归组）
          └─► u_csb: bdma2glb_done_intr_pd[0]/[1]   （2 位脉冲）
                 └─► GLB ic: done_source[6] (grp0) / done_source[7] (grp1)
                        └─► 经掩码/状态逻辑 → core_intr → 顶层 dla_intr
```

- `interrupt_ptr` 在**入队启动时**就被打上：写 `LAUNCH0` → `gather_ptr=0`；写 `LAUNCH1` → `gather_ptr=1`，一路随描述符传到 store。
- 一个批次里有多张描述符时，**只有最后一张**的最后一拍才产生 done（`is_last_cmd` 判定），避免一次启动报多次中断。
- 两个 group 让 CPU 可以「group 0 在跑时，group 1 继续入队」，实现 BDMA 的双缓冲、让搬运尽量不停顿。

`STATUS_0` 里的 `grp0_busy`/`grp1_busy` 分别在 `LAUNCH0/1` 时置位、在对应 group done 时清零，CPU 据此知道每组是否还在忙。

#### 4.4.3 源码精读

寄存器地址译码与 CMD 字段拼装（自动生成文件，四段式模板，见 [u2-l3](u2-l3-register-files-shadow-config.md)）：

[NV_NVDLA_BDMA_reg.v:L148-L168](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v#L148-L168) — 各寄存器的写使能译码，偏移如 `0x4000`(SRC_LOW)/`0x4004`(SRC_HIGH)/`0x4008`(DST_LOW)/`0x4010`(LINE)/`0x4014`(CMD)/`0x4030`(OP)/`0x4034`(LAUNCH0) 等。

[NV_NVDLA_BDMA_reg.v:L170-L174](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v#L170-L174) — `CMD_0 = {30'b0, dst_ram_type, src_ram_type}`；地址低字段拼装为 `{v32, 5'b0}`（32 字节对齐）。

`OP_ENABLE` 写入即把当前寄存器值快照压入 `csb_fifo`（注意：`csb_fifo` 存的是**快照副本**，所以 CPU 写完 op_en 后可立刻改寄存器填下一张描述符，不会覆盖在途命令）：

[NV_NVDLA_BDMA_csb.v:L372-L384](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L372-L384) — `csb_fifo_wr_pd` 把 src/dst 地址、size、ram_type、各 stride/repeat 全部打包成 289 位描述符。

[NV_NVDLA_BDMA_csb.v:L386-L396](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L386-L396) — `csb_fifo` 实例（`NV_NVDLA_BDMA_LOAD_csb_fifo`，20 深 × 289 位），并带 `wr_count` 用于算 `free_slot`。

[NV_NVDLA_BDMA_csb.v:L477](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L477) — `csb_fifo_wr_pvld = op_en_trigger & op_en`：写 `OP_ENABLE=1` 即触发一次入队。

[NV_NVDLA_BDMA_csb.v:L461](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L461) — `free_slot[7:0] = 20 − csb_fifo_wr_count`。

启动与组归属、批次中断判定：

[NV_NVDLA_BDMA_csb.v:L583-L588](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L583-L588) — `LAUNCH0` → `gather_ptr=0`，`LAUNCH1` → `gather_ptr=1`，确定本批次归属哪个 group。

[NV_NVDLA_BDMA_csb.v:L747-L748](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L747-L748) — `reg2dp_cmd_interrupt = is_last_cmd_rdy`（仅批次最后一拍置中断）、`reg2dp_cmd_interrupt_ptr = launch_ptr`（组指针随命令下发）。

done 上报到 GLB：

[NV_NVDLA_BDMA_csb.v:L753-L756](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L753-L756) — `status_grp0_clr = st2csb_grp0_done`、`status_grp1_clr = st2csb_grp1_done`：store 完成脉冲清对应 group 的 busy。

[NV_NVDLA_BDMA_csb.v:L964-L973](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_csb.v#L964-L973) — `bdma2glb_done_intr_pd[0] <= status_grp0_clr`、`[1] <= status_grp1_clr`：把 group done 翻成 2 位脉冲送 GLB。

GLB 侧聚合（承接 [u2-l4](u2-l4-glb-config-interrupts.md)）：BDMA 占 `done_source` 的 bit6（grp0）/bit7（grp1）：

[NV_NVDLA_GLB_ic.v:L168](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_ic.v#L168) — `done_source = {cacc[1:0], cdma_wt[1:0], cdma_dat[1:0], rubik[1:0], bdma[1:0], pdp[1:0], cdp[1:0], sdp[1:0]}`，BDMA 落在 `[7:6]`。

#### 4.4.4 代码实践

1. **目标**：为「把权重从 DBB 预搬到 CVSRAM」这个典型用例，列出所需的寄存器写入序列与中断路径。
2. **步骤**：假设权重在 DBB 的 `0x2000_0000`，要搬到 CVSRAM 的 `0x0000_8000`，权重是连续的 8KB（256 个 32 字节块）。
3. **配置序列（CSB 写，地址 = BDMA 基址 0x4000 + 偏移）**：

   | 写入地址 | 值 | 说明 |
   | --- | --- | --- |
   | 0x4000 (SRC_ADDR_LOW) | 0x2000_0000 | 源 = DBB 低地址 |
   | 0x4004 (SRC_ADDR_HIGH) | 0x0000_0000 | 源 = DBB 高地址 |
   | 0x4008 (DST_ADDR_LOW) | 0x0000_8000 | 目的 = CVSRAM 低地址 |
   | 0x400c (DST_ADDR_HIGH) | 0x0000_0000 | 目的 = CVSRAM 高地址 |
   | 0x4010 (LINE) | 0x0000_0100 | line_size = 256（8KB/32B） |
   | 0x4018 (LINE_REPEAT) | 0x0000_0000 | 单 line |
   | 0x4024 (SURF_REPEAT) | 0x0000_0000 | 单 surface |
   | 0x4014 (CMD) | 0x0000_0001 | **src_ram_type=1(DBB/MCIF), dst_ram_type=0(CVSRAM/CVIF)** |
   | 0x4030 (OP_ENABLE) | 0x0000_0001 | 入队这张描述符 |
   | 0x4034 (LAUNCH0) | 0x0000_0001 | 启动，归属 group 0 |

4. **中断路径**：store 写完最后一拍 → `st2csb_grp0_done` → `bdma2glb_done_intr_pd[0]=1` → GLB `done_source[6]=1` → `bdma_done_status0` 置位 → 经 GLB 掩码/状态逻辑进 `core_intr` → 顶层 `dla_intr`。CPU 也可改用 group 1（写 LAUNCH1）在 group 0 还没跑完时继续塞下一批，实现不停顿搬运。
5. **预期结果**：8KB 权重被从 DBB 复制到 CVSRAM，完成后 GLB 的 BDMA group0 done 位被置起。具体时序「待本地验证」。
6. **重要提醒**：`CMD` 的 `src_ram_type=1`(DBB)、`dst_ram_type=0`(CVSRAM) 是依据本仓库 RTL 译码（见 4.2.3）得出的；若你参照的对外文档与之矛盾，**以 RTL 译码为准**并在本地 trace 中验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么写完 `OP_ENABLE` 之后，CPU 可以立刻改写 SRC_ADDR 等寄存器去填下一张描述符，而不会破坏正在排队的命令？

**参考答案**：因为 `csb_fifo_wr_pd` 在写 `OP_ENABLE` 那一拍已经把当时所有寄存器的值**快照**成一个 289 位副本压入队列（csb.v 第 372–384 行）。后续 load/store 消费的是这份快照副本，而非实时寄存器，所以改写寄存器不影响在途命令。

**练习 2**：一次 `LAUNCH0` 启动了 3 张描述符，会报几次 done 中断？

**参考答案**：只报 1 次。因为 `reg2dp_cmd_interrupt = is_last_cmd_rdy` 只在批次最后一张描述符的最后一拍拉高（csb.v 第 747 行）。这正是 gather/launch 批次机制的意义：把多张描述符合并成「一次启动、一次完成」。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成一次完整的「权重预取」并验证状态机与中断。

**场景**：推理开始前，用 BDMA 把一段权重从 DBB 预取到 CVSRAM，让后续卷积引擎（CDMA）能低延迟地从 CVSRAM 读权重。

**任务**：

1. **算参数**：权重为 `H=16 行 × 每行 128 字节` 的二维块（共 2KB）。算出 `line_size`、`line_repeat_number`、`surf_repeat_number`、源/目的 `line_stride`，要求行间紧凑无 padding。
2. **写序列**：参照 4.4.4，写出从 `SRC_ADDR_LOW` 到 `LAUNCH0` 的完整 CSB 写序列，标出 `CMD` 寄存器里 `src_ram_type`/`dst_ram_type` 的取值与理由。
3. **画通路**：在 `NV_NVDLA_bdma.v` 中标注这条搬运走的是「load → MCIF 读 → cq → store → CVIF 写」，并指出 `csb_fifo` 与 `cq` 各自缓存了什么。
4. **追中断**：在源码中确认完成脉冲从 `st2csb_grp0_done` 到 `bdma2glb_done_intr_pd[0]` 再到 GLB `done_source[6]` 的逐级传递。
5. **双缓冲思考**：若 group 0 还在搬运时，CPU 想立刻排队下一段权重的搬运，应该写 `LAUNCH0` 还是 `LAUNCH1`？为什么？通过 `STATUS_0` 的哪几个位可以观察两个 group 的忙闲？

**预期产出**：一张寄存器写入表 + 一张 BDMA 内部数据通路草图 + 一段中断传递链路说明。

> 涉及实际波形与数据比对的结论「待本地验证」（需用 `verif/sim` 跑含 BDMA 的 trace，建议配合 [u7-l2 CSB 激励序列与 trace 格式](u7-l2-csb-sequence-trace.md) 阅读现成 trace 中的 BDMA 配置段）。

## 6. 本讲小结

- BDMA 是 NVDLA 唯一「横跨两套存储」的引擎：源端、目的端可各自选 MCIF（DBB）或 CVIF（CVSRAM），专做存储间批量搬运，把 CPU 从内存拷贝里解放出来。
- 内部是「取数—缓冲—写数」三段流水：`u_load` 发读请求、`u_cq` 缓存目的端上下文、`u_store` 发写请求并产生 done；`u_csb` 是寄存器与控制中枢。
- `src_ram_type`/`dst_ram_type` 各一个比特选择走哪套存储；**本仓库 RTL 中 0=CVIF、1=MCIF**（读写两侧一致），阅读时以源码译码为准。
- BDMA 支持三维搬运（cube←surface←line←transaction），`line_size` 以 32 字节为单位，`line_stride`/`surf_stride` 以字节为单位，源/目的 stride 独立可配，搬运时还能顺便重排布局。
- 编程模型是「描述符 + 双组启动」：写一组配置寄存器 → 写 `OP_ENABLE` 把快照压入 20 深 `csb_fifo` → 写 `LAUNCH0/1` 启动并归属 group 0/1；`free_slot = 20 − count` 指示剩余容量。
- 完成中断按 group 上报：`store` → `st2csb_grp0/1_done` → `bdma2glb_done_intr_pd[1:0]` → GLB `done_source[7:6]`；两个 group 实现 BDMA 的双缓冲不停顿搬运。

## 7. 下一步学习建议

- **追读后处理引擎的 DMA**：BDMA 是「纯搬运」，而 [u5-l1 SDP](u5-l1-sdp-single-point.md)、[u5-l2 PDP](u5-l2-pdp-pooling.md) 各有 `rdma/wdma` 子模块。对比它们的 DMA 与 BDMA 的异同（BDMA 双向两套存储、计算引擎 DMA 多为单向取数/写数）。
- **回到存储接口内部**：BDMA 的读/写请求最终进入 MCIF/CVIF 的 IG→cq→eg 三级通路，建议进入 [u4-l2 MCIF](u4-l2-mcif-primary-memif.md) / [u4-l3 CVIF](u4-l3-cvif-cvsram.md) 看请求被仲裁与重排的细节。
- **看真实 trace**：结合 [u7-l2 CSB 激励序列与 trace 格式](u7-l2-csb-sequence-trace.md)，在 `verif/traces` 里找一个含 BDMA 配置的 trace，对照本讲的寄存器表逐行解读它的搬运意图。
- **端到端串联**：在 [u8-l4 端到端集成](u8-l4-end-to-end-integration.md) 中，BDMA 是「先于卷积启动」的关键一步，体会它在整条推理流水里的位置。
