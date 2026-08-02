# 数据缓存 dcache 与 MSHR

## 1. 本讲目标

在 [u5-l1（访存单元 LSU）](u5-l1-lsu.md) 中我们看到，LSU 把一条向量访存拆成多个子请求发给 D-cache，并在自己的 LSU MSHR 里把乱序响应合并回向量寄存器堆。但那里有意回避了一个问题：**当请求真的在 D-cache 里查不到（cache miss）时，硬件怎么办？** 本讲就回答这个问题。

读完本讲，你应当能够：

1. 说清 L1 D-cache（`l1_dcache`）的组相联几何结构（组数、路数、块字数）以及它对外的四个接口（`core_req`/`core_rsp`/`mem_req`/`mem_rsp`）各自承载什么。
2. 描述一次读缺失的完整往返：tag miss → 分配 MSHR entry → 向 L2 发 `GET` → 数据返回填入 cache → 唤醒等待的 LSU 请求。
3. 理解 MSHR 如何用「主表项（entry）× 子表项（subentry）」的二级结构合并多个对同一 cache 块的缺失，并能说出 `DCACHE_MSHRENTRY`/`DCACHE_MSHRSUBENTRY` 对并发缺失能力的意义。
4. 理解写缓冲（wshr）如何按块地址去重，以及 TileLink 风格的操作码（`TLAOP_GET`/`PUTFULL`/`PUTPART`/`FLUSH`）在 `mem_req` 上如何编码。

> 本讲是 expert 层的第一篇，默认你已经学过 u5-l1（LSU 的地址计算与 LSU MSHR）和 u3-l2（icache 的组相联与 MSHR 思路）。D-cache 的 MSHR 与 icache 的 MSHR 思想同源，但结构更复杂，请随时对照。

---

## 2. 前置知识

### 2.1 为什么需要 MSHR

一个 naïve 的 cache 缺失处理是「停住整条流水线，等数据回来再继续」。这在 GPU 上不可接受——一个 SM 里同时驻留多个 warp，目的就是用 warp 切换隐藏延迟。如果一次缺失就锁死 cache，后续 warp 的命中请求也会被堵死，延迟隐藏就失效了。

**MSHR（Miss Status Holding Register，缺失状态保持寄存器）** 解决的就是这个问题：cache 遇到缺失时，不阻塞后续请求，而是把「这个缺失请求在等谁」记在一张表里，然后放行后续请求；等下级存储把数据送回来，再凭记录把当初等待的请求一一唤醒。这样 cache 在缺失期间仍能服务其他命中请求。

### 2.2 主缺失与次缺失

假设 cache 向某 cache 块 A 发出 `GET` 后、数据还没回来期间，又来了几个同样访问块 A 的请求。如果每个都单独向 L2 发一次 `GET`，既浪费带宽又可能乱序。MSHR 把它们区分为：

- **主缺失（primary miss）**：块 A 当前没有任何在途的 `GET`，这是第一个，需要新分配一个 MSHR **entry**，并向 L2 真正发一次 `GET`。
- **次缺失（secondary miss）**：块 A 已经有一个在途的 `GET` 了，本次只需把请求挂到那个 entry 下，**不**再发新的 `GET`，等同一个填充结果回来一起满足。

这就引出了本讲 MSHR 的「entry × subentry」二级结构。

### 2.3 TileLink 一句话

D-cache 对外（经 `l1cache_arb` 到 L2）用的是 TileLink 风格的 A/D 通道（详见 [u7-l1](u7-l1-tilelink-protocol.md)）。本讲只需要知道：A 通道发请求，带一个 3 位的 `opcode`（如 `GET`=读、`PUTFULL`/`PUTPART`=写、`FLUSH`=刷回）；D 通道收响应，`opcode` 标识这是读数据（`AccessAckData`）、写确认（`AccessAck`）还是 hint 确认。`source` 字段则用来在响应回来时「认领」是哪个请求。

---

## 3. 本讲源码地图

本讲全部源码位于 `src/gpgpu_top/sm/l1cache/dcache/`，参数定义在 `src/define/define.v`。

| 文件 | 作用 |
| --- | --- |
| [l1_dcache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v) | D-cache 顶层。只做例化与连线，串起 tag_access、l1_mshr、dcache_wshr、data SRAM 和各级 FIFO/仲裁器，是本讲的「主板」。 |
| [dcache_control.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_control.v) | 纯组合译码器：把 `{opcode,param}` 译成 `is_read/is_write/is_flush/is_invalidate/...`。 |
| [tag_access/tag_access_top_v2.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v) | tag 体 SRAM + 有效位/脏位 + LRU 替换 + tag 命中判定。 |
| [tag_access/tag_checker.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_checker.v) | 纯组合：并行比较各路 tag，输出命中与 waymask。 |
| [l1_mshr/l1_mshr.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v) | D-cache 的 MSHR：entry×subentry 二级表、5 态状态机、targetinfo 暂存。 |
| [l1_mshr/get_entry_status_req.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_req.v) | 分配辅助：统计 valid 位，给出 `full/alm_full/next`（下一个空闲位）。 |
| [l1_mshr/get_entry_status_rsp.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_rsp.v) | 回收辅助：给出 `next2cancel`（下一个待取消位）和已用计数。 |
| [dcache_wshr.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v) | 写缓冲（Write SHR）：按块地址去重，回填写响应的 `source`。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 全部规模与编码宏的总开关。 |

上游衔接：`sm_wrapper.v` 中，LSU 的请求经 `lsu2d_q`（LSU-to-dcache 队列）送入 dcache 的 `core_req_*` 接口；CTA 完成时 `cache_invalid` 也会产生一个 `opcode=3'b011` 的请求（见 `sm_wrapper.v` 的 `pipe_dcache_req_opcode_comb`）。dcache 的 `mem_req/mem_rsp` 则对接 `l1cache_arb`，最终到 L2。

---

## 4. 核心概念与源码讲解

### 4.1 l1_dcache 顶层：四接口与命中/缺失主流程

#### 4.1.1 概念说明

把 `l1_dcache` 想象成一个「自带快递柜台的小仓库」：

- **`core_req`（核心侧请求）**：LSU 进货/取货的窗口，每个请求携带 `instrid`（属于哪条向量访存指令）、地址（拆成 `tag`+`setidx`）、各 lane 的 `activemask`、块内偏移 `blockoffset`、字内字节使能 `wordoffset1h`、写数据 `data`，以及 TileLink 风格的 `opcode/param`。
- **`core_rsp`（核心侧响应）**：把读到的数据（或写完成标志）连同 `instrid` 还给 LSU。
- **`mem_req`（存储侧请求）**：cache miss 或刷回时，向 L2 发的 A 通道请求（`GET`/`PUT`/`FLUSH`）。
- **`mem_rsp`（存储侧响应）**：L2 回送的 D 通道响应（读数据 / 写确认 / hint 确认）。

cache 自身流水分为几级 stage：`st0`（拍 0，读 tag/data SRAM 与 MSHR probe）、`st1`（拍 1，判命中/缺失并决定动作）、`st2`（响应组装）、`st3`（`mem_req` 出口）。

#### 4.1.2 核心流程

一次**读命中**的主线：

1. `core_req` 经入口 FIFO（`core_req_q`，深度 1）打一拍进 `st1`。
2. `st0` 用 `setidx` 读 tag SRAM；`st1` 由 `tag_checker` 判定命中并给出 `waymask`。
3. 命中即读 data SRAM，结果经 `core_rsp_st2` → `core_rsp_q` 回送 LSU。

一次**读缺失**的主线（本讲重点）：

1. `st1` 判定 `cache_miss_st1 && is_read_st1`。
2. 向 MSHR **probe**：查这个块地址是否已有在途 `GET`。
3. probe 结果 + tag miss → 向 MSHR 发 `missreq`（分配 entry 或挂 subentry）。
4. 经 `memreq_arb`（3 选 1）把 `GET` 请求送进 `mem_req_q` → `mem_req` 接口发往 L2；`source` 字段里塞进 MSHR entry 号与 setidx，供响应认领。
5. L2 数据回来（`mem_rsp`，`opcode=AccessAckData`）：填 data SRAM、写新 tag（`allocateWrite`）、回填 MSHR entry。
6. MSHR 通过 `missrsp_out` 把当初记下的 `targetinfo`（instrid/activemask/偏移）吐出，组装成 `core_rsp` 唤醒 LSU。

#### 4.1.3 源码精读

**模块端口——四个接口一目了然**：[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L19-L57](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L19-L57)。注意 `core_req_opcode_i` 是 3 位、`core_req_param_i` 是 4 位，与下游 TileLink 的 3 位 opcode 不完全相同——dcache 内部用 `{opcode,param}` 二维编码区分 read/write/lr/sc/amo/flush/invalidate/wait_mshr（见 4.2）。

**入口 FIFO 与 st1 切片**：请求先入一个深度为 1 的 `stream_fifo_pipe_true` 缓冲，再切片成各字段（`core_req_*_st1`）。[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L82-L95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L82-L95)

**命中/缺失判定**：[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L374-L379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L374-L379)。`read_hit/read_miss/write_hit/write_miss` 都由 `cache_hit_st1`（= `tag_hit_st1`）与 `is_*_st1` 组合得到，这是整条主流程的分叉点。

**核心状态机 `core_req_st1_ready`**：这个 `always@(*)` 块是 dcache 的「调度大脑」，按请求类型决定 st1 何时算「处理完一拍」，并把各种反压条件串起来。[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L1086-L1113](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1086-L1113)。读缺失分支要求 `mshr_missreq_ready && memreq_arb_in1_ready && (probe_out_mshr_status==000||010)`——即 MSHR 能收、仲裁器能放、且 probe 结果允许（主缺失或次缺失可挂）。

**三类 memReq 的 3 选 1 仲裁**：固定优先级 `fixed_pri_arb`，优先级 `in0 > in1 > in2`。[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L599-L612](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L599-L612)。三路分别是：`in0`=脏块替换写回（dirty replace）、`in1`=读/写缺失请求（miss mem req）、`in2`=invalidate/flush 请求。它们的连接见 [L1747-L1776](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1747-L1776)。

**读缺失请求组装（`GET`）**：[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L782-L789](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L782-L789)。`opcode=TLAOP_GET`，`source={3'b001, mshr_entry_id, setidx}`——那个 `3'b001` 是「类型标签」，告诉 L2 这是 D-cache 的读缺失。

**最终输出**：[src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v:L1925-L1941](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1925-L1941)。`core_req_ready_o` 把所有反压条件（probe/allocate 冲突、在途写缺失、MSHR 满、memReqQ 接近满等）相与，是理解「cache 何时拒绝新请求」的入口。

#### 4.1.4 代码实践：跟踪一次读缺失的信号链

**实践目标**：在不跑仿真的前提下，靠阅读源码把「读缺失」这条数据通路在脑中跑通，并标注每一段对应的行号。

**操作步骤**：

1. 从 [L1335](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1335) 的 `mshr_missreq_valid` 出发，确认它由 `read_miss_st1 && mshr_probe_status` 触发——即「读缺失且 MSHR probe 完成」。
2. 跟到 [L786](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L786) 的 `read_miss_req_a_source`，看清 `source` 里塞了哪三段（类型标签、MSHR entry 号、setidx）。
3. 跟到 [L937-L939](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L937-L939) 的 `mem_rsp_is_read`（`opcode==3'b001` 即 AccessAckData），这是 L2 数据回来的判据。
4. 跟到 [L1342](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1342) 的 `mshr_missrsp_in_valid`，看响应如何凭 `source` 里的 entry 号（[L1343](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1343)）回填 MSHR。
5. 最后看 MSHR 吐出的 `missrsp_out` 如何在 [L1466-L1470](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1466-L1470) 变成 `core_rsp_st2_enq_valid`，唤醒 LSU。

**需要观察的现象**：`source` 字段是「请求—响应」之间的身份纽带，读缺失用它把 entry 号带给 L2、又从 L2 的响应里取回。

**预期结果**：你能画出 `read_miss_st1 → mshr_missreq → memreq_arb.in1 → mem_req_q → mem_req(GET) … mem_rsp(AccessAckData) → mshr_missrsp_in → mshr.missrsp_out → core_rsp` 这条闭合链路。

**待本地验证**：若你有 VCS 环境，可在 `testcase/test_gpgpu_axi_top/tc_vecadd` 下 `make run-vcs-4w4t`，用 Verdi 抓 `l1_dcache` 的 `mshr_missreq_valid` 与 `mem_req_valid_o`，观察读缺失时它们的先后因果关系。

#### 4.1.5 小练习与答案

**练习 1**：`core_req_ready_o`（[L1925](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1925)）里有一项 `!mem_req_q_alm_full`。为什么 memReqQ 快满时要反压核心侧新请求？

**参考答案**：读/写缺失、脏块替换、flush 都要经 `mem_req_q` 下发 L2。若该队列满，缺失请求无法送出，`core_req_st1_ready` 会卡在缺失分支，进而 st1 无法推进，所以入口必须提前反压（队列深度 32，阈值 `>27` 即「接近满」，见 [L1679](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1679)）。

**练习 2**：读缺失的 `source` 高位是 `3'b001`。结合 [L1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853) 的写请求 `source` 高位 `3'b000`，说明这个标签的作用。

**参考答案**：它是「请求来源/类型」的命名空间标签，供 L2（或回送路径）区分该 A 通道事务来自 D-cache 的读缺失（`001`）还是写（`000`，且后续位是 wshr 表项号）。响应回来时凭 `source` 整体路由回正确的 dcache 内部结构（MSHR entry 或 wshr 表项）。

---

### 4.2 dcache_control：请求类型译码

#### 4.2.1 概念说明

D-cache 收到的请求不止「读/写」两种。CTA 调度器在一个 workgroup 跑完时，会经 `cache_invalid` 触发 cache 刷回/无效（见 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md)）；LSU 还可能发 LR/SC/AMO 原子操作。dcache 用一个 3 位 `opcode` + 4 位 `param` 的二维编码来统一定义这些请求种类，再由 `dcache_control` 这个**纯组合译码器**把它们翻成一组布尔标志位（`is_read`/`is_write`/`is_flush`/`is_invalidate`/`is_wait_mshr`/...），供后续控制逻辑直接使用。

把译码单独抽成一个无状态模块的好处是：后续 `always` 块只需读布尔位，不必到处重复 `opcode==... && param==...` 的魔法数字。

#### 4.2.2 核心流程

译码真值表（按 `dcache_control` 实现）：

| opcode | param | 含义 | 输出标志 |
| --- | --- | --- | --- |
| 3'b000 | 0000 | 普通读（GET） | `is_read` |
| 3'b000 | 0001 | LR（带保留读） | `is_lr` |
| 3'b001 | 0000 | 普通写（PUT） | `is_write` |
| 3'b001 | 0001 | SC（条件写） | `is_sc` |
| 3'b010 | * | AMO 原子操作 | `is_amo` |
| 3'b011 | 0000 | 无效（invalidate） | `is_invalidate` |
| 3'b011 | 0001 | 刷回（flush） | `is_flush` |
| 3'b011 | 0010 | 等待 MSHR 排空 | `is_wait_mshr` |

#### 4.2.3 源码精读

整个模块就是 8 行 `assign`：[src/gpgpu_top/sm/l1cache/dcache/dcache_control.v:L28-L35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_control.v#L28-L35)。

在顶层，这些标志先在 st0 被 `core_req_valid_i&&core_req_ready_o` 门控成 `is_*_st0`（[L296-L303](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L296-L303)），再随请求入一个深度 1 的控制 FIFO 到 st1（`is_*_st1`），保证控制位与数据同拍到达。

> 注意 `is_amo` 分支在 `core_req_st1_ready` 里目前标注 `TODO: AMO`（[L1107](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1107)），即原子操作的支持尚未完整实现，是已知待完善项。

#### 4.2.4 代码实践：用 upstream 确认 invalidate 的来源

**实践目标**：验证「invalidate 请求」确实来自 workgroup 完成时的 `cache_invalid`。

**操作步骤**：在 `sm_wrapper.v` 中检索 `cache_invalid_valid` 与 `pipe_dcache_req_opcode_comb`（本讲开头提到的上游连线）。

**需要观察的现象**：当 `cache_invalid_valid` 有效时，`pipe_dcache_req_opcode_comb` 被设为 `'d3`（即 3'b011），`param` 为 `'d0`（即 invalidate）。

**预期结果**：这与 `dcache_control` 真值表的 `opcode=011,param=0000 → is_invalidate` 完全对应，闭环了「wg 完成 → 刷 cache」的控制链。

**待本地验证**：可在仿真中抓 `cache_invalid_valid` 与 dcache 的 `core_req_opcode_i/param_i` 同拍对照。

#### 4.2.5 小练习与答案

**练习**：`is_wait_mshr`（`opcode=011,param=0010`）表示「等 MSHR 排空」。结合 [L1104-L1105](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1104-L1105)，这种请求什么时候 ready？

**参考答案**：`core_req_st1_ready = mshr_empty`，即 MSHR 所有 entry 都空时才放行。它的用途是在做某些不可与在途缺失并行的操作前，先确保所有未完成 GET 落定。

---

### 4.3 tag_access：组相联、命中判定与替换

#### 4.3.1 概念说明

`tag_access_top_v2` 是 D-cache 的「目录管理员」，负责三件事：

1. **命中判定**：给定 `{tag,setidx}`，查这组里有没有某路的 tag 匹配且有效。
2. **替换决策**：缺失填充时，若该组已满，按 LRU（最近最少使用）选一路替换；若该路脏，还需先写回（产生 dirty replace 请求）。
3. **脏/有效位与 flush 支持**：维护每路每组的 `way_valid`/`way_dirty`，支持 invalidate（整表清有效）与 flush（逐脏行写回）。

D-cache 的几何由 `define.v` 决定：`DCACHE_NSETS=32` 组、`DCACHE_NWAYS=2` 路、`DCACHE_BLOCKWORDS=2` 字/块。32 位地址按 `tag | setidx | blockoffset | wordoffset` 划分（[define.v:L69-L87](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L69-L87)）。

#### 4.3.2 核心流程

**读命中**：`probeRead` 用 `setidx` 读 tag SRAM（拍 0），拍 1 由 `tag_checker` 并行比较两路 tag + 有效位，命中即给出 `waymaskHit`。

**缺失填充（allocateWrite）**：L2 数据回来时，写新 tag 进 SRAM。若该组已满，先用 LRU 矩阵选出 victim 路；若 victim 路脏（`needReplace`），则先发起一次脏块写回（dirty replace，走 `memreq_arb.in0`）。

**替换地址计算**：victim 的完整地址由其 tag + setidx 拼成，低位补零：

\[
\text{addr}_{\text{replace}} = \{\text{tag}_{\text{victim}},\ \text{setIdx},\ 0_{\text{BLOCKOFFSETBITS}+\text{WORDOFFSETBITS}}\}
\]

#### 4.3.3 源码精读

**有效位/脏位寄存器**：`way_valid`、`way_dirty` 都是 `NUM_WAY*NUM_SET` 位（2×32=64 位）的平面数组。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L126-L127](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L126-L127)

**tag SRAM 读仲裁（3 选 1）**：probe（核心请求）、allocate（填充写新 tag）、hasDirty（flush 选脏行）三者竞争一个读端口，固定优先级 `allocate > probe > hasDirty`。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L194-L209](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L194-L209)

**tag_checker 并行比较**：纯组合，对每路 `tag_of_set==tag_from_pipe && valid`，再或起来得 `cache_hit`。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_checker.v:L31-L37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_checker.v#L31-L37)

**脏位更新**：写命中置脏；flush 选中行或替换时清脏。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L310-L324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L310-L324)

**有效位更新**：allocateWrite 时置 victim 路有效（除非该组已满走 LRU 替换——此时有效位本就为 1）；`invalidateAll` 时整表清零。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L393-L403](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L393-L403)

**替换路选择**：若该组未满，选第一个无效路（`way_nvalid` 经优先编码）；若已满，用 LRU 矩阵选最久未用路。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L379-L386](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L379-L386)。`needReplace_o`（[L326](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L326)）= victim 路当前是脏的，触发先写回。

**flush 选脏行**：用 `lzc`（前导零计数）在 `set_dirty` 位图里挑第一个含脏行的组，再在该组里挑脏路，供上层逐行写回。[src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v:L425-L434](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L425-L434)

#### 4.3.4 代码实践：核算 D-cache 容量与替换路径

**实践目标**：用 `define.v` 算出 D-cache 的总容量，并理清「脏替换」何时发生。

**操作步骤**：

1. 容量公式：一块 = `DCACHE_BLOCKWORDS × XLEN/8` 字节 = \(2 \times 4 = 8\) 字节；总路数 = `NSETS × NWAYS` = \(32 \times 2 = 64\) 块；总容量 = \(64 \times 8 = 512\) 字节。
2. 在 [L326](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L326) 确认 `needReplace_o` 依赖 `way_dirty[...] && allocateWrite_fire_q`。
3. 跟到 l1_dcache.v 的 [L1216-L1223](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1216-L1223)：dirty replace 请求用 `TLAOP_PUTFULL`，地址是 victim 的 `tag+setidx`。

**需要观察的现象**：脏替换请求是 `memreq_arb` 的 `in0`，优先级最高——因为填充必须等脏块让出物理行后才能写入。

**预期结果**：每 SM 的 L1 D-cache 仅 512 字节（很小），这正是 GPU 依赖大量 warp 切换与 MSHR 隐藏访存延迟的背景。

#### 4.3.5 小练习与答案

**练习**：`invalidateAll_i` 把 `way_valid` 整表清零（[L398-L399](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/tag_access/tag_access_top_v2.v#L398-L399)），但不清 `way_dirty`。这安全吗？

**参考答案**：需配合 flush 流程看。`invalidate` 请求在 `core_req_st1_ready` 里要求 `!core_req_tag_hasdirty_st1 && mshr_empty && wshr_empty`（[L1099](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1099)），即脏行已先被逐行写回（经 `waitfor_l2_flush` 流程）才会走到 `invalidateAll`，此时清有效位即可；脏位残留不会丢数据，因为对应行已无效、不会再被命中读出。

---

### 4.4 l1_mshr：缺失合并与二级表项

#### 4.4.1 概念说明

`l1_mshr` 是本讲最精巧的部分。它用「entry × subentry」二维表实现主/次缺失合并：

- **entry（主表项）**：每个 entry 记录一个**distinct 块地址**（`blockaddr_access`），代表一次在途的 `GET`。`DCACHE_MSHRENTRY=4`，即同一时刻最多 4 个不同 cache 块的 GET 在飞。
- **subentry（子表项）**：每个 entry 下挂 `DCACHE_MSHRSUBENTRY=2` 个子项，每个子项记录一个**等待该块的请求**的 `targetinfo`（instrid、activemask、块内偏移、字节使能）。

主缺失 → 新分配一个 entry（并真的发 GET）；次缺失 → 在已有 entry 下挂一个 subentry（不再发 GET）。数据回来时，按 entry 把所有挂着的 subentry 逐个满足（`missrsp_out` 一次吐一个 targetinfo）。

容量关系：可同时跟踪的等待请求数为

\[
\text{容量} = \text{DCACHE\_MSHRENTRY} \times \text{DCACHE\_MSHRSUBENTRY} = 4 \times 2 = 8
\]

但其中最多只有 4 个**不同块**的 GET 在飞。

#### 4.4.2 核心流程

**probe（探查）**：每个新请求在发 `missreq` 前，先用块地址查 `entry_match_probe`——有匹配则次缺失，无匹配则主缺失。probe 结果还给出 `subentry_status_next`（挂到哪个子项）。

**5 态状态机**（`mshr_status`，注释见 [L164-L169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L164-L169)）：

| 编码 | 名称 | 含义 |
| --- | --- | --- |
| 000 | PRIMARY_AVAIL | 主表项还可分配 |
| 001 | PRIMARY_FULL | 主表项已满（不能再接新块的主缺失） |
| 010 | SECONDARY_AVAIL | 当前块可挂子项 |
| 011 | SECONDARY_FULL | 当前块的子项已满 |
| 100 | SECONDARY_FULL_RETURN | 子项满后又回收了一些（过渡态） |

这个状态机被顶层用来反压：`core_req_ready_o` 在状态为 `001`/`011` 时拒绝新请求（[L246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v) 对应 l1_dcache.v 的反压条件）。

**回收**：L2 响应回来（`missrsp_in`，带 entry 号），用 `get_entry_status_rsp` 选出该 entry 下一个待满足的 subentry，吐出其 targetinfo（`missrsp_out`），并清该子项 valid；子项全清后 entry 释放。

#### 4.4.3 源码精读

**二维存储**：`blockaddr_access`（每 entry 一个块地址）、`targetinfo_access`（每 entry×subentry 一个 targetinfo）、`subentry_valid`（每子项一个 valid 位）。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L42-L45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L42-L45)

**entry 命中/probe**：`entry_valid[i]` = entry i 下有任一子项有效；`entry_match_probe` = 块地址匹配且 entry 有效。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L55-L61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L55-L61)

**分配辅助（请求侧）**：`get_entry_status_req` 用 `pop_cnt` 数 valid 位给 `full/alm_full`，用 `find_first` 在反转位图里找首个空闲位作 `next`。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_req.v:L39-L58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_req.v#L39-L58)。它分别例化两次：一次参数 `NUM_ENTRY=DCACHE_MSHRENTRY`（选 entry），一次 `NUM_ENTRY=DCACHE_MSHRSUBENTRY`（选 subentry，见 l1_mshr.v [L89-L112](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L89-L112)）。

**回收辅助（响应侧）**：`get_entry_status_rsp` 给 `next2cancel`（下一个要清的子项）和 `used`（剩余计数）。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_rsp.v:L34-L51](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/get_entry_status_rsp.v#L34-L51)

**5 态状态机**：[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L184-L233](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L184-L233)。读它时抓住三条线索：`probe_valid_i`（探查触发状态评估）、`missreq_valid_i && missreq_ready_o`（真正分配）、`missrsp_in_valid_i`（回收）。

**targetinfo 写入**：分配时按 `(real_sram_addr_up, real_sram_addr_down)` 定位子项写入。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L314-L332](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L314-L332)

**块地址写入**：仅主缺失且状态为 `PRIMARY_AVAIL` 时，把块地址写进新 entry。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L335-L351](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L335-L351)

**子项 valid 更新**：四种情形分别置 1/清 0（主缺失建子项、回收清子项、次缺失建子项）。[src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v:L420-L447](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L420-L447)

**关键输出**：`missreq_ready_o = !(status==001 || status==011)`（[L450](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L450)）；`empty_o = !(|entry_valid)`（[L457](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L457)）；`probe_out_a_source_o` 把分配到的 entry 号回送给顶层塞进 `source`（[L461](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L461)）。

#### 4.4.4 代码实践：参数实验——并发缺失能力

**实践目标**：弄清 `DCACHE_MSHRENTRY`/`DCACHE_MSHRSUBENTRY` 如何决定并发缺失能力。

**操作步骤**：

1. 在 [define.v:L89-L91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L89-L91) 读到 `DCACHE_MSHRENTRY=4`、`DCACHE_MSHRSUBENTRY=2`。
2. 推演：若 LSU 同时对 **3 个不同块** 发起读缺失，需要 3 个 entry（≤4，可接受，3 个 GET 并行在飞）；若对**同一块**连续 3 次读缺失，需要 3 个 subentry（>2，第 3 次会触发 `SECONDARY_FULL=011` 反压）。
3. 在 [L450](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L450) 确认 `missreq_ready_o` 在 011 时为 0——即子项满时拒绝新次缺失。

**需要观察的现象**：增大 `DCACHE_MSHRSUBENTRY` 能让同一块挂更多等待请求；增大 `DCACHE_MSHRENTRY` 能让更多不同块并行缺失。

**预期结果**：默认 4×2=8 容量，可同时跟踪 8 个等待请求、4 个不同块在飞。若把 `DCACHE_MSHRENTRY` 改为 8，则 `blockaddr_access`/`targetinfo_access` 寄存器位宽翻倍，并发缺失能力增强但面积增大。

**待本地验证**：修改 `define.v` 后重新仿真（注意 `D_SOURCE`/`A_SOURCE` 等含 `$clog2(DCACHE_MSHRENTRY)` 的派生宏会自动跟随，见 [define.v:L111-L113](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L111-L113)），确认编译通过且 `tc_vecadd` 仍 PASSED。

#### 4.4.5 小练习与答案

**练习 1**：为什么次缺失不向 L2 再发一次 `GET`？

**参考答案**：同一块已经有 GET 在途，再发只会浪费带宽并可能引入乱序。次缺失挂 subentry 即可，等同一个填充结果回来一次性满足该块所有等待请求。

**练习 2**：`mshr_status==001`（PRIMARY_FULL）和 `011`（SECONDARY_FULL）都会反压新请求（[l1_dcache.v:L246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L246)）。它们各自表示什么资源耗尽？

**参考答案**：`001` 表示 4 个 entry 全被不同块占用（不能再接新块的任何缺失）；`011` 表示当前 probe 命中的那个块的 2 个 subentry 全满（不能再往这个块挂新的次缺失，但其他块若 entry 有空仍可主缺失）。

---

### 4.5 dcache_wshr：写缓冲与块地址去重

#### 4.5.1 概念说明

**WSHR（Write SHR）** 是 D-cache 的写缓冲状态表，与 MSHR 对偶：MSHR 跟踪「等待读填充」的请求，WSHR 跟踪「已下发 L2、等待写确认」的写事务。

它的核心功能是**按块地址去重**：当一个新的写请求要下发时，先查 WSHR 里有没有同一块地址、尚未收到确认的写——若有（`conflict`），说明对该块的写还在路上，本次需暂缓（`wshr_protect`），避免对同一块的多个写乱序到达 L2 造成数据错乱。每个写请求下发时分配一个 `pushedIdx`（WSHR 表项号），塞进 `source` 字段；L2 写确认（`AccessAck`）回来时凭 `source` 里的表项号 pop 该项。

`DCACHE_WSHR_ENTRY=4`（[define.v:L75](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L75)），且注释要求「不大于 `DCACHE_MSHRENTRY`」。

#### 4.5.2 核心流程

1. **push**：写请求经 `mem_req_q` 出口（st3）时，若不冲突且 wshr 不满，分配一个表项，记下块地址，`pushedIdx` 塞进 `source`。
2. **conflict 检测**：用新写请求的块地址与所有有效表项并行比较，任一匹配即 `conflict`。
3. **pop**：L2 写确认回来，从 `source` 取表项号，清该表项 valid。
4. **同拍 push+pop 复用**：若同一拍既 pop 一个旧表项又 push 一个新请求，直接把 pop 出的表项号 `popReq_bits_i` 复用给新 push（`pushedIdx = popReq_bits_i`），避免无谓翻动。

#### 4.5.3 源码精读

**冲突检测与满判定**：`pushMatchMask[i]` = 表项 i 的块地址 == 新请求块地址 且表项有效；`conflict_o = |pushMatchMask`；`pushReq_ready_o = !(&validEntries)`（不满才能 push）。[src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v:L50-L59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v#L50-L59)

**同拍复用**：[src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v:L61-L62](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v#L61-L62)

**表项更新**：四种情形（同拍 pop+push 复用写、纯 push、纯 pop 清 valid、保持）。[src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v:L64-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v#L64-L80)

**选空闲表项**：对 `~validEntries` 做固定优先级仲裁再转二进制，得 `nextEntryIdx`。[src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v:L82-L89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_wshr.v#L82-L89)

**在顶层如何使用**：`wshr_pushedIdx` 在写请求时塞进 `source`（[l1_dcache.v:L1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853)）；写确认回来时从 `source` 取表项号 pop（[L1803-L1804](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1803-L1804)）。`wshr_conflict` 触发 `wshr_protect` 暂缓写请求（[L1791](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1791)）。`is_invalidate`/`is_flush` 还要求 `wshr_empty`（[L1099-L1102](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1099-L1102)），即所有写确认落定后才能刷缓存——这与 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md) 讲的「wg 完成须等缓存刷净才回报主机」一致。

#### 4.5.4 代码实践：跟踪一次写缺失的 source 闭环

**实践目标**：把写请求的 `source` 从 push 到 pop 走通，理解 wshr 如何闭环。

**操作步骤**：

1. 写缺失用 `TLAOP_PUTPART`（[l1_dcache.v:L777](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L777)），写回/脏替换用 `TLAOP_PUTFULL`（[L1218](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1218)），flush 用 `TLAOP_FLUSH`（[L1209](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1209)）。
2. 看写请求在 st3 把 `wshr_pushedIdx` 塞进 `source`（[L1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853)）。
3. 看写确认（`mem_rsp_is_write`，opcode=AccessAck）回来时，从 `source` 取 wshr 表项号 pop（[L1803-L1804](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1803-L1804)）。

**需要观察的现象**：`source` 在写事务里承载的是 wshr 表项号（而非 MSHR entry 号），两者共用同一 `source` 字段，靠高位类型标签区分（写=`3'b000`，读缺失=`3'b001`）。

**预期结果**：你能说清「push 分配表项号 → 写进 source → L2 确认 → 凭 source pop」的闭环，以及为何同块写冲突时要 `wshr_protect` 暂缓。

#### 4.5.5 小练习与答案

**练习**：为什么 `DCACHE_WSHR_ENTRY` 注释要求「不大于 `DCACHE_MSHRENTRY`」？结合 [L1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853) 的 `WM_ENTRY_EQUAL` 处理思考。

**参考答案**：`source` 字段的位宽按较大的 `DCACHE_MSHRENTRY` 设计（`$clog2(DCACHE_MSHRENTRY)` 位）。若 wshr 表项号位数少于 entry 号位数，需在高位补零对齐（[L1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853) 的 `WM_ENTRY_EQUAL ? ... : {...补零...}` 三目）。约束 wshr ≤ mshr 是为了简化这个位宽对齐逻辑，避免 pop 时取错位（见 [L1804](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1804) 的注释 `ATTENTION: DCACHE_MSHRENTRY > DCACHE_WSHR_ENTRY`）。

---

## 5. 综合实践：把一次「读缺失 + 脏替换」全程串起来

设计一个贯穿本讲的任务：假设某 SM 的 D-cache 当前某组已满且两路皆脏，LSU 现在对一个**新块**发起读缺失。请画出并叙述完整的时序与数据流，要求覆盖以下全部环节，并标注每步对应的关键源码行号：

1. **请求入场**：LSU 请求经 `core_req_q` 到 st1，`dcache_control` 译出 `is_read`，`tag_checker` 判 miss（[l1_dcache.v:L374-L379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L374-L379)）。
2. **MSHR probe + 分配**：块地址查 MSHR 无匹配 → 主缺失 → 分配 entry 0、subentry 0，记下 targetinfo（[l1_mshr.v:L314-L332](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_mshr/l1_mshr.v#L314-L332)）。
3. **发 GET**：`read_miss_req` 组装 `TLAOP_GET`，`source={3'b001, entry0, setidx}`，经 `memreq_arb.in1` → `mem_req_q` → L2（[l1_dcache.v:L786](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L786)）。
4. **脏替换（与 GET 并行）**：填充前 tag_access 发现 victim 路脏（`needReplace`），先经 `memreq_arb.in0` 用 `TLAOP_PUTFULL` 把脏块写回 L2（[l1_dcache.v:L1216-L1223](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1216-L1223)）；该写回也走 wshr 闭环。
5. **数据回来填充**：L2 回 `AccessAckData`，`mem_rsp_is_read` 成立 → 写 data SRAM、`allocateWrite` 写新 tag、清脏位、回填 MSHR entry 0（[l1_dcache.v:L1320-L1324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1320-L1324)）。
6. **唤醒 LSU**：MSHR `missrsp_out` 吐出 entry0 子项的 targetinfo，组装成 `core_rsp` 还给 LSU（[l1_dcache.v:L1466-L1470](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1466-L1470)）。

**验收标准**：你能指出第 3 步的 GET 与第 4 步的脏替换写回**共用同一个 `mem_req` 接口、靠 `memreq_arb` 分时复用**，且 GET 的 `source` 用 entry 号、写回的 `source` 用 wshr 表项号，二者靠高位标签区分。这正是 D-cache 在单一 TileLink A 通道上承载多类事务的关键设计。

**待本地验证**：在有 VCS 的环境，构造一个会触发脏替换的访存序列（连续访问超过 cache 容量的不同地址），用 Verdi 同时观察 `tag_needReplace`、`mshr_missreq_valid`、`mem_req_a_opcode_o`（区分 `GET`=4 与 `PUTFULL`=0）与 `mem_rsp_d_opcode_i`，验证上述时序。

---

## 6. 本讲小结

- `l1_dcache` 是 D-cache 顶层「主板」，靠四接口（`core_req/core_rsp/mem_req/mem_rsp`）对接 LSU 与 L2，自身几乎不做运算，靠例化 tag_access、l1_mshr、dcache_wshr 与各级 FIFO/仲裁器拼出完整数据通路。
- 请求类型由 `dcache_control` 把 `{opcode,param}` 译成布尔标志，覆盖 read/write/lr/sc/amo/flush/invalidate/wait_mshr；wg 完成时的 `cache_invalid` 即产生 invalidate 请求。
- `tag_access_top_v2` 提供 32 组×2 路组相联的命中判定、LRU 替换与脏/有效位管理；脏替换会先用 `TLAOP_PUTFULL` 写回 victim 块。
- `l1_mshr` 用 entry×subentry 二维表合并主/次缺失：主缺失发 `GET` 并占一个 entry，次缺失只挂 subentry；5 态状态机刻画资源余量并驱动反压；默认 4×2 容量决定并发缺失能力。
- `dcache_wshr` 是写事务的对偶状态表，按块地址去重（`conflict`），用 `source` 里的表项号在写确认时闭环 pop；flush/invalidate 须等 wshr 排空。
- TileLink 操作码（`GET`/`PUTFULL`/`PUTPART`/`FLUSH`）与 `source` 字段（类型标签 + entry/wshr 号 + setidx）是多类事务在同一 A 通道复用的编码基础。

---

## 7. 下一步学习建议

- **向「外」走**：本讲的 `mem_req/mem_rsp` 出了 D-cache 后，先经 `l1cache_arb`（[u6-l3](u6-l3-l1cache-arbiter.md)）与 icache/shared_memory 仲裁，再到 L2。建议下一讲学 `l1cache_arb`，看清三类 L1 请求如何汇聚。
- **向「协议」走**：若对 `source` 编码、A/D 通道、GET/PUT/ACQUIRE 的完整语义感兴趣，转 [u7-l1（TileLink 协议）](u7-l1-tilelink-protocol.md) 和 [u7-l2（L2 Scheduler）](u7-l2-l2cache-scheduler.md)，看 L2 如何凭 `source` 把响应路由回正确的 SM/cache。
- **源码延伸阅读**：想看 D-cache 如何与 shared_memory 协同，可对照 [u6-l2](u6-l2-shared-memory.md) 的 `sharemem`；想精读数据对齐与字节重排，可看本目录下未被本讲展开的 `gen_data_map_per_byte.v`、`gen_data_map_same_word.v`、`get_data_access_banken.v` 三个辅助模块。
- **动手建议**：综合实践若能在仿真中验证，可顺带测量一次读缺失的延迟周期数，体会 MSHR「不阻塞后续命中」带来的吞吐收益。
