# 顶层模块 GPGPU_top 与系统数据流

> 前置讲义：本讲承接 u1-l1（架构总览）、u1-l2（目录结构）、u1-l3（define.v 配置）。你已知道 Ventus GPGPU 由「CTA 调度 + 若干 SM 核 + L2」三大部件构成，也知道规模总开关在 `define.v`。本讲不再重复这些结论，而是真正打开顶层文件 `GPGPU_top.v`，看这些部件**在代码里是怎么连起来的**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `GPGPU_top` 顶层模块对外暴露哪些接口、内部例化了哪几大部件。
- 画出一条**控制流**：主机 `host_req` → `cta_interface` → `cta2warp` 派发 → 某个 `sm_wrapper` 的 `cta_req` 接口。
- 解释一个 workgroup 跑完后，`wg_done` / `is_flushing` / `l2cache_finish_issue` 三者如何配合，最终拉起 `host_rsp_valid_o`（核心是「必须等 L2 把缓存刷干净才回报主机」）。
- 画出一条**数据流**：SM 的 `mem_req` → `sm2cluster_arb` → `l2_distribute` → `cluster_to_l2_arb` → `Scheduler`(L2) → `out_a/out_d` 对外。
- 理解 `NO_CACHE` 宏如何用 `` `ifdef `` 在「带 L2 + TileLink 对外」与「直连 icache/dcache 接口」两种模式间切换。

## 2. 前置知识

在进入代码前，先建立三个直觉。

**(1) 顶层模块 = 一块「主板」。**
GPGPU_top 本身**几乎不做运算**，它像电脑主板一样，把 CTA 调度器、若干 SM 核、L2 缓存、对外接口这些「芯片」插上去，再用 wire 把它们的管脚连起来。读懂顶层，本质是读懂「谁连到谁」。

**(2) 控制流和数据流是两条独立的「线」。**
- **控制流**承载「让核去执行什么」：主机下发 workgroup → CTA 调度器选中一个 SM → 把 start_pc、寄存器基址、wf_tag 等参数派发过去 → SM 开始取指执行 → 全部 warp 完成后回报主机。
- **数据流**承载「指令和数据从哪来、写到哪去」：SM 里的 icache/dcache 缺失 → 经片上互联 → L2 → 再经 AXI 到外部内存。

这两条线在 GPGPU_top 里由不同的 wire 集合承载，本讲会分别拆开看。

**(3) 用 `` `ifdef NO_CACHE `` 做接口切换。**
`NO_CACHE` 是 `define.v` 里的编译宏（默认**未定义**）。定义它时，顶层把 icache/dcache 的原始接口直接引到模块外（适合早期不带 L2 的调试）；不定义它时，顶层用 TileLink 风格的 `out_a/out_d` 通道对外（这是带 L2 + AXI 的正式形态，也是仿真默认形态）。同一份源码靠宏编译出两种「壳」。

> 名词速查：`workgroup`(wg)=一个线程块；`warp`/`wf`(wavefront)=一组锁步执行的线程；`CU`(compute unit)在本项目里就是 SM 核。这些在 u1-l1 已建立，本讲直接使用。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [src/gpgpu_top/GPGPU_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v) | 系统顶层「主板」 | 端口、三大部件例化、`wg_done/is_flushing` 完成/冲刷逻辑、`NO_CACHE` 切换 |
| [src/gpgpu_top/gpgpu_axi_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv) | 带 AXI 的对外壳 | 用 `axi4lite_2_cta` 产生 host_req、用 `axi4_adapter_top` 把 L2 的 out_a/out_d 转 AXI |
| [src/gpgpu_top/cta_top/cta_interface.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v) | CTA 调度对外接口 | 把 host2cta 接入 `cta_scheduler`，把派发结果广播给所有 CU |
| [src/gpgpu_top/sm/sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v) | 单个 SM 核外壳 | `cta_req_*` 接收端、`cache_invalid_i` 触发 dcache 冲刷 |
| [src/gpgpu_top/l2cache/Scheduler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v) | L2 缓存顶层 | `sche_in_a/sche_in_d`（对片上）、`sche_out_a/sche_out_d`（对外）、`finish_issue_o` |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 配置总开关 | `NUM_CLUSTER` / `NUM_SM` / `NUM_SM_IN_CLUSTER` / `NUM_L2CACHE` / `NUMBER_CU` |

`l2_distribute.v`、`cluster_to_l2_arb.v`、`sm2cluster_arb.v` 三个互联模块本讲只看**连接关系**，内部仲裁/分发逻辑留到 u7-l3。

## 4. 核心概念与源码讲解

### 4.1 GPGPU_top：系统的「主板」

#### 4.1.1 概念说明

`GPGPU_top` 是整个 GPGPU 的最顶层（不含 AXI 外壳时）。它的职责只有三件：

1. 声明对外接口（主机接口 + 存储接口）。
2. 例化三大部件：`cta_interface`（调度）、`sm_wrapper` ×N（计算核）、`Scheduler` ×M（L2 缓存）。
3. 用 wire 把部件之间的「管脚」连起来，必要时做位宽拼接/切片。

它本身没有任何 always 块做数据处理——唯一的时序逻辑是用于「完成回报」的 `is_flushing` 状态（见 4.3）。这一点很关键：**读顶层文件，主要在读连线**。

#### 4.1.2 核心流程

GPGPU_top 的组装可以分成 4 块：

```
                  ┌─────────────── 主机接口 host_req_* / host_rsp_* ──────────────┐
                  │                                                                │
                  ▼                                                                │
          ┌───────────────┐   cta2warp_* (广播)   ┌──────────────────┐             │
主机 ───► │ cta_interface │ ───────────────────► │ sm_wrapper × NUM_SM │            │
          │  (CTA 调度)   │ ◄─────────────────── │  (SM 核 ×N)        │            │
          └───────────────┘    warp2cta_* (完成)  └──────────────────┘             │
                                                  │ mem_req/mem_rsp                  │
                                                  ▼                                  │
                              ┌─────────────── 互联 ───────────────┐                │
                              │ sm2cluster_arb → l2_distribute      │                │
                              │            → cluster_to_l2_arb       │                │
                              └──────────────────┬───────────────────┘                │
                                                 ▼                                    │
                                       ┌───────────────────┐  out_a/out_d  ┌────────┐ │
                                       │ Scheduler (L2) ×M │ ────────────► │ 对外   │ │
                                       └───────────────────┘               │ AXI/mem│◄┘
                                            finish_issue_o ───────────────► host_rsp
```

规模由 `define.v` 决定，本讲默认配置下：

- `NUM_CLUSTER = 1`，`NUM_SM = 2`，`NUM_SM_IN_CLUSTER = NUM_SM/NUM_CLUSTER = 2`
- `NUM_L2CACHE = 1`
- `NUMBER_CU = NUM_SM = 2`（一个 CU 对应一个 SM）

#### 4.1.3 源码精读

**(a) 模块端口：主机接口**

[GPGPU_top.v:22-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L22-L45) 声明了顶层端口。主机请求侧（`host_req_*`）携带一个 workgroup 的全部派发参数：

```verilog
input   host_req_valid_i,            // 主机请求有效
output  host_req_ready_o,            // 顶层可接收
input   [`WG_ID_WIDTH-1:0]    host_req_wg_id_i,                 // workgroup 编号
input   [`WF_COUNT_WIDTH-1:0] host_req_num_wf_i,                // 该 wg 含几个 warp
input   [`WAVE_ITEM_WIDTH-1:0]host_req_wf_size_i,               // 每个 warp 多少线程
input   [`MEM_ADDR_WIDTH-1:0] host_req_start_pc_i,              // 起始 PC
input   [`MEM_ADDR_WIDTH-1:0] host_req_pds_baseaddr_i,          // 参数基址
input   [`MEM_ADDR_WIDTH-1:0] host_req_csr_knl_i,               // kernel 的 CSR 基址
input   [`VGPR_ID_WIDTH:0]    host_req_vgpr_size_total_i,       // 共需多少向量寄存器
...
output  host_rsp_valid_o,            // workgroup 完成回报
output  [`WG_ID_WIDTH-1:0]  host_rsp_inflight_wg_buffer_host_wf_done_wg_id_o, // 完成的是哪个 wg
```

这一大束 `host_req_*` 信号在 GPGPU_top 里**原封不动**地连到 `cta_interface`（见 4.2），顶层不解析它们。

**(b) 模块端口：存储接口与 NO_CACHE**

[GPGPU_top.v:47-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L47-L95) 用 `` `ifdef NO_CACHE `` 给出两套端口：

- `NO_CACHE` 分支（[L47-L75](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L47-L75)）：把每个 SM 的 `icache_mem_req/rsp`、`dcache_mem_req/rsp` 原始接口直接引出，注意位宽是 `[NUMBER_CU-1:0]` 维度的「一束」。
- 非 `NO_CACHE` 分支（[L77-L94](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L77-L94)）：用 TileLink 风格的 A 通道（`out_a_*`，请求）和 D 通道（`out_d_*`，响应）对外，位宽带 `NUM_L2CACHE` 维度。这是仿真默认形态。

> 注意 `out_a`/`out_d` 是 L2 的对外通道，**不是** SM 的通道。SM 的请求先进 L2，L2 缺失后才经 `out_a` 出去。

**(c) 例化三大部件的位置索引**

| 部件 | 代码位置 | 例化数量 |
|------|---------|---------|
| `cta_interface` | [GPGPU_top.v:273-319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L273-L319) | 1 个 |
| `sm_wrapper` | [GPGPU_top.v:321-400](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L321-L400) | `NUM_CLUSTER × NUM_SM_IN_CLUSTER`（= NUM_SM） |
| `Scheduler` + `cluster_to_l2_arb` | [GPGPU_top.v:402-483](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L402-L483) | `NUM_L2CACHE` |
| `sm2cluster_arb` + `l2_distribute` | [GPGPU_top.v:485-561](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L485-L561) | `NUM_CLUSTER` |

`sm_wrapper` 用了**双层 generate**（外层 cluster、内层 cluster 内的 SM），把二维索引 `(i,p)` 映射成一维 SM 编号 `i*NUM_SM_IN_CLUSTER+p`，这是后续 `cta2warp_*` 总线切片寻址的基础（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：把 GPGPU_top 当成一张「装配图」来读，确认默认配置下到底例化了几个 SM、几个 L2。

**操作步骤**：

1. 打开 [src/define/define.v:3-7](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L3-L7) 与 [define.v:41](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L41)，确认 `NUM_CLUSTER=1, NUM_SM=2, NUM_L2CACHE=1`。
2. 在 [GPGPU_top.v:322-324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L322-L324) 的双层 `for` 循环里代入这两个值，算出 `sm_wrapper U_sm_wrapper` 实际展开成几个实例。
3. 在 [GPGPU_top.v:404-406](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L404-L406) 确认 `Scheduler` 例化数量。

**预期结果**：`NUM_CLUSTER=1 × NUM_SM_IN_CLUSTER=2 = 2` 个 SM；`NUM_L2CACHE=1` 个 L2。即默认是一块「2 核 1 个 L2」的小 GPU。**待本地验证**：若把 `NUM_SM` 改成 4，generate 会展开成 4 个 `sm_wrapper`，相应 `cta2warp_*` 总线位宽也会变宽。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GPGPU_top 里几乎看不到 `always` 数据处理逻辑？
**答**：因为顶层是「主板」，只负责例化和连线；真正的状态机、流水线、缓存都在子模块里。顶层唯一的时序逻辑（`is_flushing`）也只是用来做完成回报的协调，不做数据运算。

**练习 2**：默认配置下 `NUMBER_CU` 等于多少？它和 `NUM_SM` 是什么关系？
**答**：[define.v:149](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L149) 定义 `NUMBER_CU = NUM_SM`，默认 `=2`。在本项目里「一个 CU 就是一个 SM」，所以 CTA 调度器要往 `NUMBER_CU` 个目标里选一个派发。

---

### 4.2 控制流：host_req → cta_interface → sm_wrapper

#### 4.2.1 概念说明

`cta_interface` 是主机与 SM 之间的「调度翻译层」。它内部例化了真正的调度器 `cta_scheduler`（u2-l1 详讲），并做两件顶层关心的事：

1. 把主机送来的**单份** `host2cta_*` 派发参数，转换成对**某一个** CU 的 `cta2warp_*` 派发握手。
2. 收集所有 CU 回报的 `warp2cta_*` 完成信号，汇总成一个 `cta2host_valid_o`（即 `wg_done`）。

#### 4.2.2 核心流程

```
host_req_valid_i ──► cta_interface ──► cta_scheduler (查资源表，选 CU)
                                          │ dispatch2cu_* (单份标量参数)
                                          ▼
                          广播 generate (for i in 0..NUMBER_CU-1)
                                          │ cta2warp_valid[i] 仅被选中的 CU 为 1
                                          ▼
                                   sm_wrapper[i].cta_req_*
```

关键点：**派发参数是「广播」的，靠 valid 位「点选」**。`cta_interface` 把同一组标量参数（start_pc、基址、wf_tag 等）扇出到所有 `NUMBER_CU` 个 CU，但 `cta2warp_valid_o[i]` 只有被调度器选中的那个 CU 为 1，其余 CU 看到 valid=0 不接收。

完成方向相反：每个 SM 的 `warp2cta_valid_i[i]` 汇聚回来，调度器内部的 `inflight_wg_buffer` 判断一个 workgroup 的所有 warp 是否都完成，再经 `wf_done_interface_single` 产生 `cta2host_valid_o`。

#### 4.2.3 源码精读

**(a) cta_interface 的端口：一头是 host2cta，一头是 cta2warp**

[cta_interface.v:18-64](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L18-L64)：

```verilog
//host2cta
input  host2cta_valid_i,          // ← 直接来自 GPGPU_top 的 host_req_valid_i
output host2cta_ready_o,          //   → 回连 host_req_ready_o
...
//cta2warp  —— 注意每路都带 [NUMBER_CU-1:0] 或 NUMBER_CU*W 维度
output [`NUMBER_CU-1:0]                        cta2warp_valid_o,
input  [`NUMBER_CU-1:0]                        cta2warp_ready_i,
output [`NUMBER_CU*`WF_COUNT_WIDTH_PER_WG-1:0] cta2warp_dispatch2cu_wg_wf_count_o,
...
//warp2cta —— 完成回报汇聚
input  [`NUMBER_CU-1:0]   warp2cta_valid_i,
output [`NUMBER_CU-1:0]   warp2cta_ready_o,
```

**(b) GPGPU_top 把 host_req_* 原样接进 cta_interface**

[GPGPU_top.v:277-292](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L277-L292) 里，`host_req_valid_i` 直接连到 `cta_interface` 的 `host2cta_valid_i`：

```verilog
.host2cta_valid_i       (host_req_valid_i),   // 顶层主机请求 → CTA
.host2cta_ready_o       (host_req_ready_o),
.host2cta_host_wg_id_i  (host_req_wg_id_i),
.host2cta_host_start_pc_i(host_req_start_pc_i),
...
```

这是「控制流入口」的物理连线点。

**(c) 广播 generate：把单份参数扇出到所有 CU**

[cta_interface.v:140-163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L140-L163)：

```verilog
for(i=0;i<`NUMBER_CU;i=i+1) begin: CTA2WARP_OUTPUT
  assign cta2warp_valid_o[i]                                   = cta_sche_dispatch2cu_wf_dispatch[i]; // 仅选中 CU 为 1
  assign cta2warp_dispatch2cu_wg_wf_count_o[...*(i+1)-1-:W]    = cta_sche_dispatch2cu_wg_wf_count;   // 同一份参数
  assign cta2warp_dispatch2cu_start_pc_dispatch_o[...*(i+1)-1-:W] = cta_sche_dispatch2cu_start_pc_dispatch;
  ...
  assign warp2cta_ready_o[i] = 'd1;   // CTA 始终准备好接收完成信号
end
```

可以看到：除了 `valid_o[i]` 用一位 `wf_dispatch[i]` 点选外，其余参数对每个 `i` 都赋**同一个标量值**，这就是「广播 + 点选」。

**(d) SM 侧接收：cta_req_\***

[sm_wrapper.v:26-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L26-L45) 的 `cta_req_*` 端口名与 `cta2warp_*` 一一对应（GPGPU_top 里在 sm_wrapper 例化处改名连接）：

```verilog
output cta_req_ready_o,
input  cta_req_valid_i,                       // ← cta2warp_valid[本 SM]
input  [`MEM_ADDR_WIDTH-1:0] cta_req_dispatch2cu_start_pc_dispatch_i,  // ← 同一份广播参数
...
output cta_rsp_valid_o,                       // → warp2cta_valid[本 SM]
```

而 GPGPU_top 在 [sm_wrapper 例化 L329-L348](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L329-L348) 用带步长的切片 `[base-:W]` 从 `NUMBER_CU` 维总线里**抽取出本 SM 那一段**：

```verilog
.cta_req_valid_i (cta2warp_valid[i*`NUM_SM_IN_CLUSTER+p]),
.cta_req_dispatch2cu_start_pc_dispatch_i(
   cta2warp_dispatch2cu_start_pc_dispatch[(i*`NUM_SM_IN_CLUSTER+p+1)*`MEM_ADDR_WIDTH-1-:`MEM_ADDR_WIDTH]),
```

这就是 4.1.3 里「二维索引 `(i,p)` 映射成一维」的目的：让每个 SM 从拼接总线里切到自己的那 `MEM_ADDR_WIDTH` 位。

#### 4.2.4 代码实践

**实践目标**：手工跑一遍控制流连线，确认「主机请求 → 某个 SM」的通路是通的。

**操作步骤**：

1. 在 [GPGPU_top.v:277](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L277) 找到 `host_req_valid_i → host2cta_valid_i`。
2. 顺着 [cta_interface.v:88-92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L88-L92) 看 `host2cta_valid_i → cta_scheduler.host_wg_valid_i`。
3. 在 [cta_interface.v:143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L143) 看 `cta2warp_valid_o[i] = cta_sche_dispatch2cu_wf_dispatch[i]`。
4. 在 [GPGPU_top.v:330](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L330) 看 `cta_req_valid_i = cta2warp_valid[i*NUM_SM_IN_CLUSTER+p]`。
5. 在 [sm_wrapper.v:296-298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L296-L298) 看 sm_wrapper 把 `cta_req_*` 再转给内部 `cta2warp` 子模块（即 `warpReq`）。

**预期结果**：你应得到一条完整链路 `host_req_valid_i → host2cta_valid_i → cta_scheduler.host_wg_valid_i →(资源表判定)→ dispatch2cu_wf_dispatch[i] → cta2warp_valid[i] → sm_wrapper[i].cta_req_valid_i → cta2warp.warpReq`。

#### 4.2.5 小练习与答案

**练习 1**：为什么派发参数要广播给所有 CU，而不是只连被选中的那个 CU？
**答**：因为硬件例化是静态的（generate 在编译期展开），「被选中」是运行时由调度器决定的结果。最简单的实现是把同一份参数连到所有 CU，再用 `valid[i]` 一位「点选」。这样调度器换目标 CU 时不需要重新布线，只改变 `wf_dispatch` 的 one-hot 即可。

**练习 2**：[cta_interface.v:161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L161) 写了 `assign warp2cta_ready_o[i] = 'd1;`，说明什么？
**答**：CTA 调度侧对每个 CU 的完成回报**始终准备好接收**（ready 恒 1），不会反压 SM 上报 warp 完成。SM 只要算完就可以直接把 `warp2cta_valid` 拉起。

---

### 4.3 完成回报：wg_done / is_flushing / host_rsp_valid_o

#### 4.3.1 概念说明

一个 workgroup 跑完，主机怎么知道？最朴素的做法是「warp 都完成 → 立刻回报主机」。但本项目**没有**这么做。原因是：workgroup 结束时，SM 的 dcache 里可能还缓存着脏数据，L2 也可能有未完成的写回。如果此时就告诉主机「完成了」，主机去读外部内存可能读到旧数据。

所以本项目加了一道「**缓存冲刷握手**」：warp 完成 → 触发 dcache 无效化（把脏数据刷下去）→ 等 L2 把这些事务全部处理完 → 才拉起 `host_rsp_valid_o`。`wg_done`、`is_flushing`、`l2cache_finish_issue` 三个信号就是用来编排这个握手的。

#### 4.3.2 核心流程

```
所有 warp 完成
   └─► cta_interface.cta2host_valid_o  =  wg_done  (一个脉冲)
            │
            ├──► cache_invalid[0] = 1   → sm_wrapper[0].cache_invalid_i (触发该 SM dcache 刷)
            └──► is_flushing <= 1       (进入「冲刷中」状态)
                              │
                              ▼
        SM 把脏数据/FLUSH 请求经互联送进 L2
                              │
                              ▼
        Scheduler 处理完所有事务 ──► l2cache_finish_issue (脉冲)
                              │
                              ▼
        host_rsp_valid_o = l2cache_finish_issue && is_flushing  (脉冲)
        is_flushing <= 0   (退出冲刷状态)
```

关键公式：

\[ \text{host\_rsp\_valid\_o} = \text{l2cache\_finish\_issue} \;\wedge\; \text{is\_flushing} \]

只有「L2 确认处理完」**且**「我们确实处在冲刷状态」时，才向主机回报。

#### 4.3.3 源码精读

**(a) wg_done 的来源**

[GPGPU_top.v:295](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L295)：

```verilog
.cta2host_valid_o(/*host_rsp_valid_o*/wg_done),
```

注意：`cta_interface` 的 `cta2host_valid_o` 并**没有**直接当 `host_rsp_valid_o`（注释里特意把它注释掉了），而是接到内部 wire `wg_done`。`wg_done` 表示「workgroup 的 warp 都完成了」，但还不是给主机的最终回答。

`wg_done` 在 `cta_interface` 内部由 `wf_done_interface_single` 产生（[cta_interface.v:130-138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L130-L138)）：当一个 wg 的所有 warp 的 `wf_done` 都到齐，就发一个 `host_wf_done_valid_o` 脉冲。

**(b) 三个核心赋值**

[GPGPU_top.v:253-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L253-L270)：

```verilog
//TODO: cache_invalid can't multi SM
assign cache_invalid    = {wg_done,{(`NUMBER_CU-1){1'b0}}};   // 只有 SM[0] 会被置 invalid
assign host_rsp_valid_o = l2cache_finish_issue && is_flushing;

always@(posedge clk or negedge rst_n) begin
  if(!rst_n)              is_flushing <= 'd0;
  else if(wg_done)        is_flushing <= 1'b1;     // wg 完成 → 进入冲刷
  else if(l2cache_finish_issue) is_flushing <= 1'b0; // L2 处理完 → 退出冲刷
  else                    is_flushing <= is_flushing;
end
```

- `cache_invalid` 是 `NUMBER_CU` 位宽，但**只有最高位（对应 SM[0]）随 `wg_done` 变化**，其余位恒 0。注释 `TODO: cache_invalid can't multi SM` 明确指出：当前实现只冲刷 SM[0]，多 SM 场景是个待完善的限制。
- `is_flushing` 是一个简单的状态机：`wg_done` 置位，`l2cache_finish_issue` 复位。
- `host_rsp_valid_o` 是组合输出，必须在「冲刷中」且「L2 完成」同时成立时才脉冲。

**(c) SM 侧如何响应 cache_invalid**

`cache_invalid` 经 [sm_wrapper 例化 L378](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L378) 连到 `sm_wrapper.cache_invalid_i`。SM 内部 [sm_wrapper.v:167-186](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L167-L186)：

```verilog
always@(posedge clk or negedge rst_n) begin
  if(!rst_n)                cache_invalid_reg <= 'd0;
  else if(cache_invalid_i)  cache_invalid_reg <= 'd1;       // 锁存冲刷请求
  else if(cache_invalid_valid) cache_invalid_reg <= 'd0;    // 冲刷完成
  ...
end
assign cache_invalid_valid = cache_invalid_reg && lsu_mshr_is_empty; // 等 LSU 的 MSHR 排空
assign pipe_dcache_req_opcode_comb = cache_invalid_valid ? 'd3 : lsu2d_q_deq_opcode; // 'd3 = FLUSH
```

即 `cache_invalid_i` 拉起后，SM 先锁存，等 LSU 的 MSHR（未完成访存表）排空，再向 dcache 发一个 `opcode='d3`（FLUSH）请求，把脏块刷下去。

**(d) L2 侧的 finish_issue_o**

`l2cache_finish_issue` 来自 `Scheduler.finish_issue_o`（[GPGPU_top.v:426](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L426)）。它表示 L2 已经把（包括冲刷在内的）事务全部发完。这个信号回来后，配合 `is_flushing` 产生最终的 `host_rsp_valid_o`。

#### 4.3.4 代码实践

**实践目标**：理解「等 L2 刷完才回报」这一时序约束，并指出当前实现的一个限制。

**操作步骤**：

1. 读 [GPGPU_top.v:249-255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L249-L255)，画出 `wg_done → is_flushing → host_rsp_valid_o` 的状态时序。
2. 读 [GPGPU_top.v:253](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L253) 那行 `cache_invalid` 的拼接：`{wg_done,{(NUMBER_CU-1){1'b0}}}`，判断当 `NUMBER_CU=2` 时，哪一位是 `wg_done`。
3. 对照注释 `TODO: cache_invalid can't multi SM`，思考：如果 workgroup 跑在 SM[1] 上，它的脏数据会不会被刷？

**需要观察的现象**：`cache_invalid` 只有最高位随 `wg_done` 变化，意味着只有 `sm_wrapper[0]` 的 `cache_invalid_i` 会被触发。

**预期结果**：当 `NUMBER_CU=2` 时，`cache_invalid` 位宽为 2，`{wg_done, 1'b0}` → `cache_invalid[1]=wg_done, cache_invalid[0]=0`。而 sm_wrapper 例化里 `cache_invalid_i` 接的是 `cache_invalid[i*NUM_SM_IN_CLUSTER+p]`，所以只有 SM0 收到冲刷请求。这正是注释所指的「多 SM 限制」——**待本地验证**：在多核仿真里确认 SM1 的脏块是否被正确刷写。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `host_rsp_valid_o` 不直接等于 `wg_done`，而要 `= l2cache_finish_issue && is_flushing`？
**答**：因为 `wg_done` 只表示 warp 计算完成，但 SM/L2 里可能还有未写回外部内存的脏数据。若此时回报主机，主机读内存会读到旧值。必须等 dcache 冲刷、L2 把事务全部处理完（`l2cache_finish_issue`），才能安全地告诉主机「完成」。

**练习 2**：`is_flushing` 状态机里，`wg_done` 和 `l2cache_finish_issue` 哪个优先级高？若同一拍两者都为 1 会怎样？
**答**：代码里 `else if(wg_done)` 写在 `else if(l2cache_finish_issue)` 之前，所以 `wg_done` 优先。若同一拍都为 1，`is_flushing` 会被置 1（继续保持冲刷状态）。这种同时发生的情况很少见，因为通常要等若干拍 L2 才能完成冲刷。

---

### 4.4 数据流：SM → 互联 → L2 → 对外

#### 4.4.1 概念说明

控制流解决「让谁算」，数据流解决「数据从哪来」。每个 SM 的 icache/dcache 缺失时，会发出 `mem_req`；这些请求要经过一片「片上互联网络」汇聚到 L2，L2 命中就直接回数据，缺失再经 `out_a` 向外（外部内存/下一级）。

默认配置（`NUM_CLUSTER=1, NUM_SM=2, NUM_L2CACHE=1`）下，互联退化为「2 个 SM → 1 个 L2」，但代码用三级模块把它写成了可扩展的形态。这三个模块分别是：

- `sm2cluster_arb`：把**一个 cluster 内多个 SM** 的请求仲裁成一路 cluster 流。
- `l2_distribute`：把 cluster 流**按地址分发**到 `NUM_L2CACHE` 个 L2。
- `cluster_to_l2_arb`：在**每个 L2 的入口**，把来自多个 cluster 的请求再仲裁成一路送给 `Scheduler`。

> 这三个模块的内部实现（仲裁算法、地址分发策略）留到 u7-l3。本讲只看它们在 GPGPU_top 里的**连接拓扑**。

#### 4.4.2 核心流程

```
sm_wrapper[0].mem_req ─┐
sm_wrapper[1].mem_req ─┴─► sm2cluster_arb ─► (1 路 cluster 流)
                                              │
                                              ▼
                                    l2_distribute ──► (NUM_L2CACHE 路)
                                              │
                                              ▼
                                    cluster_to_l2_arb ─► Scheduler(L2).sche_in_a
                                              │
                                              ▼
                                    Scheduler.sche_out_a ──► out_a_* (对外 AXI/mem)
```

响应（`out_d` / `sche_in_d`）原路返回。L2 的 `finish_issue_o` 同时被 GPGPU_top 用作 4.3 的冲刷完成标志。

#### 4.4.3 源码精读

**(a) SM 的 mem_req 接口**

非 `NO_CACHE` 模式下，每个 sm_wrapper 对外是一组 TileLink 风格的 `mem_req_a_*`（请求）和 `mem_rsp_d_*`（响应）。GPGPU_top 在 [L378-394](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L378-L394) 用步长切片把它们接到 `NUMBER_CU` 维总线 `mem_req_*` / `mem_rsp_*` 上：

```verilog
.mem_req_valid_o(mem_req_valid[i*`NUM_SM_IN_CLUSTER+p]),
.mem_req_a_addr_o(mem_req_a_addr[(i*`NUM_SM_IN_CLUSTER+p+1)*`XLEN-1-:`XLEN]),
.mem_req_a_source_o(mem_req_a_source[(i*`NUM_SM_IN_CLUSTER+p+1)*`D_SOURCE-1-:`D_SOURCE]),
...
```

**(b) sm2cluster_arb：cluster 内 SM 仲裁**

[GPGPU_top.v:486-521](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L486-L521) 在 `for(k=0..NUM_CLUSTER-1)` 里为每个 cluster 例化一个 `sm2cluster_arb`：

```verilog
.mem_req_vec_in_valid_i(mem_req_valid[(k+1)*`NUM_SM_IN_CLUSTER-1-:`NUM_SM_IN_CLUSTER]),  // cluster 内所有 SM
...
.mem_req_out_valid_o(mem_req_out_valid[k]),   // 1 路 cluster 流
```

输入是 `NUM_SM_IN_CLUSTER` 维（cluster 内 SM 数），输出是单路 cluster 流（`mem_req_out_*`，带 `NUM_CLUSTER` 维）。

**(c) l2_distribute：按地址分发到各 L2**

[GPGPU_top.v:523-558](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L523-L558)：把 cluster 流 `mem_req_out_*` 分发成 `NUM_L2CACHE` 路 `mem_req_vec_out_*`：

```verilog
.mem_req_in_valid_i(mem_req_out_valid[k]),                   // 1 路 cluster 流
.mem_req_vec_out_valid_o(mem_req_vec_out_valid[(k+1)*`NUM_L2CACHE-1-:`NUM_L2CACHE]),  // NUM_L2CACHE 路
```

响应方向（`mem_rsp_vec_in_*` → `mem_rsp_out_*`）在同一模块里反向汇聚。

**(d) cluster_to_l2_arb：每个 L2 入口的仲裁**

[GPGPU_top.v:445-480](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L445-L480) 在 `for(j=0..NUM_L2CACHE-1)` 里为每个 L2 例化一个 `cluster_to_l2_arb`：

```verilog
.mem_req_vec_in_valid_i(mem_req_vec_out_valid[(j+1)*`NUM_CLUSTER-1-:`NUM_CLUSTER]),  // 来自各 cluster
.mem_req_out_valid_o(cluster_to_l2_arb_mem_req_out_valid[j]),   // 1 路送 Scheduler
```

**(e) Scheduler(L2)：接收请求、回响应、报完成**

[GPGPU_top.v:406-443](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L406-L443)：

```verilog
Scheduler l2cache(
  .sche_in_a_valid_i  (cluster_to_l2_arb_mem_req_out_valid[j]),  // ← 片上请求进来
  .sche_in_d_valid_o  (cluster_to_l2_arb_mem_rsp_in_valid[j]),   // → 片上响应回去
  .finish_issue_o     (l2cache_finish_issue[j]),                  // → 给 4.3 用
  .sche_out_a_valid_o (l2cache_out_a_valid[j]),                   // → 对外请求 out_a
  .sche_out_d_valid_i (l2cache_out_d_valid[j]),                   // ← 对外响应 out_d
  ...
);
```

**(f) L2 对外通道连到顶层端口**

[GPGPU_top.v:564-583](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L564-L583) 用一串 `assign` 把 `l2cache_out_a_*`/`l2cache_out_d_*` 连到顶层 `out_a_*`/`out_d_*` 端口：

```verilog
assign out_a_valid_o   = l2cache_out_a_valid ;
assign out_a_address_o = l2cache_out_a_address;
assign l2cache_out_a_ready = out_a_ready_i ;
...
```

这一段就是「L2 的对外通道 = GPGPU_top 的对外通道」。

#### 4.4.4 代码实践

**实践目标**：用源码确认「一个 SM 的访存请求要穿过几级模块才到 L2」。

**操作步骤**：

1. 从 [sm_wrapper 例化 L388](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L388) 的 `mem_req_valid_o` 出发，它连到 `mem_req_valid[k*NUM_SM_IN_CLUSTER+p]`。
2. 看 [sm2cluster_arb L491](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L491) 的 `mem_req_vec_in_valid_i` 消费同一总线。
3. 看 [l2_distribute L524](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L524) 消费 `mem_req_out_valid[k]`。
4. 看 [cluster_to_l2_arb L446](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L446) 消费 `mem_req_vec_out_valid`。
5. 看 [Scheduler L409](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L409) 的 `sche_in_a_valid_i` 消费 `cluster_to_l2_arb_mem_req_out_valid[j]`。

**预期结果**：请求依次经过 `sm_wrapper → sm2cluster_arb → l2_distribute → cluster_to_l2_arb → Scheduler`，共穿过 3 个互联模块才到达 L2。默认 `NUM_L2CACHE=1` 时 `l2_distribute` 实际只往一路分发，`cluster_to_l2_arb` 也只有一路输入，但代码结构已经为多 L2 预留。

#### 4.4.5 小练习与答案

**练习 1**：默认配置下 `l2_distribute` 把请求分发成几路？这些路最后都进了同一个 L2 吗？
**答**：分发成 `NUM_L2CACHE=1` 路。因为只有 1 个 L2，所有请求最终都进这同一个 `Scheduler`。`l2_distribute` 此时退化为直通，但保留了「按地址分发到多 L2」的可扩展结构。

**练习 2**：`mem_req_vec_out_*` 总线的总位宽是多少维？（用宏表示）
**答**：`NUM_CLUSTER × NUM_L2CACHE` 维，即「每个 cluster 到每个 L2」都有一条逻辑链路。这也是 `cluster_to_l2_arb` 在每个 L2 入口要仲裁 `NUM_CLUSTER` 路输入的原因。

---

### 4.5 NO_CACHE 宏：两种对外接口模式

#### 4.5.1 概念说明

`GPGPU_top` 用 `` `ifdef NO_CACHE `` 同时切换**端口**和**内部连线**两处，编译出两种「壳」：

- **默认（非 NO_CACHE）**：SM 内部有完整的 L1（icache/dcache）+ L2，对外是 TileLink 风格的 `out_a/out_d`。再套一层 `gpgpu_axi_top.sv` 就能接 AXI。这是产品形态。
- **NO_CACHE**：把每个 SM 的 `icache_mem_req/rsp`、`dcache_mem_req/rsp` **原始接口**直接引到顶层外，由外部（如 testbench）直接喂指令/数据。这适合早期不带缓存的流水线调试，省去了 L2 和互联的复杂度。

注意：`gpgpu_axi_top.sv`（带 AXI 的壳）**只支持非 NO_CACHE 模式**，因为它连的是 `out_a/out_d`。

#### 4.5.2 核心流程

```
`ifndef NO_CACHE   (默认)
   sm_wrapper ──mem_req──► 互联 ──► Scheduler(L2) ──out_a/out_d──► 顶层 out_a/out_d
                                                                (可接 gpgpu_axi_top → AXI)

`ifdef NO_CACHE    (调试壳)
   sm_wrapper ──icache_mem_req / dcache_mem_req──► 直接引到顶层端口 (icache_mem_req_o / dcache_mem_req_o)
   (没有互联，没有 L2，外部直接响应)
```

#### 4.5.3 源码精读

**(a) 端口二选一**

[GPGPU_top.v:47-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L47-L95)：`` `ifdef NO_CACHE `` 分支声明 icache/dcache 原始端口，`` `else `` 分支声明 `out_a/out_d`。两套端口互斥，编译时按宏二选一。

**(b) sm_wrapper 内部连线的二选一**

[GPGPU_top.v:349-395](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L349-L395)：在 sm_wrapper 例化里同样用宏——`NO_CACHE` 时连 `icache_mem_*`/`dcache_mem_*`，否则连 `mem_req_*`/`mem_rsp_*`/`cache_invalid_i`。

**(c) L2 与互联只在非 NO_CACHE 时例化**

[GPGPU_top.v:402](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L402) 的 `` `ifndef NO_CACHE `` 把整个「Scheduler + cluster_to_l2_arb + sm2cluster_arb + l2_distribute + out_a/out_d 连线」块包了起来。NO_CACHE 模式下这一大段**根本不编译**，顶层因此没有 L2。

**(d) NO_CACHE 模式的直连 assign**

[GPGPU_top.v:612-639](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L612-L639) 在 `` `ifdef NO_CACHE `` 里用一串 `assign` 把 SM[1] 的 `icache_mem_req`/`dcache_mem_req` 内部总线切片后连到顶层 `icache_mem_req_o` 等端口：

```verilog
assign icache_mem_req_valid_o = icache_mem_req_valid[1];
assign dcache_mem_req_valid_o = dcache_mem_req_valid[1];
...
```

> 注意：这里硬编码了索引 `[1]`（对应 `2*NUM_SM_IN_CLUSTER-1-:...` 切片里的第 2 个 SM），同样反映了「当前 NO_CACHE 路径只对接单个 SM」的简化假设。

**(e) gpgpu_axi_top.sv 只走非 NO_CACHE**

[gpgpu_axi_top.sv:278-325](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L278-L325) 在例化 `GPGPU_top` 时，`` `ifdef NO_CACHE `` 分支把所有 cache 端口留空 `()`，`` `else `` 分支才连 `top_out_a_*`/`top_out_d_*`。真正有意义的就是 `else` 分支：

```verilog
.out_a_valid_o(top_out_a_valid),
...
.out_d_valid_i(top_out_d_valid),
```

而 `top_out_a_*`/`top_out_d_*` 又被 [gpgpu_axi_top.sv:179-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L179-L195) 转成 `axi4_adapter_top` 的 `req_i/valid_o` 形式，最终由 [axi4_adapter_top 例化 L328-L416](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L328-L416) 转成标准 AXI4 通道。主机侧则由 [axi4lite_2_cta 例化 L197-L251](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L197-L251) 把 AXI4-Lite 写事务翻译成 `host_req_*`。

#### 4.5.4 代码实践

**实践目标**：确认两种模式下顶层「对外长什么样」，并理解 `gpgpu_axi_top` 为什么不能配 NO_CACHE。

**操作步骤**：

1. 在 [GPGPU_top.v:47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L47) 与 [GPGPU_top.v:77](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L77) 对比两套端口。
2. 在 [GPGPU_top.v:402](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L402) 注意 `` `ifndef NO_CACHE `` 包住的范围（一直延伸到 L585）。
3. 在 [define.v:20](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L20) 附近（GPGPU_top.v 第 20 行有被注释掉的 `` //`define NO_CACHE ``）确认宏默认未定义。
4. 在 [gpgpu_axi_top.sv:307-325](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L307-L325) 确认只有 `else` 分支（out_a/out_d）被真正连接。

**预期结果**：默认仿真走非 NO_CACHE + `gpgpu_axi_top`，顶层对外是 AXI（AXI4-Lite 做主机控制、AXI4 做 L2 访存）。若强行定义 `NO_CACHE`，`gpgpu_axi_top` 的 `out_a/out_d` 就没有驱动源，仿真无法工作。

#### 4.5.5 小练习与答案

**练习 1**：为什么说「`gpgpu_axi_top` 只支持非 NO_CACHE」？
**答**：因为 `gpgpu_axi_top` 在例化 `GPGPU_top` 时，只把 `out_a/out_d`（TileLink 通道）连出去再做 AXI 转换；而 `out_a/out_d` 只在非 NO_CACHE 模式下由 L2 驱动。NO_CACHE 模式下顶层没有 L2、也没有 `out_a/out_d`，所以这套 AXI 壳接不上。

**练习 2**：NO_CACHE 模式下，指令和数据由谁直接提供给 SM？
**答**：由顶层外部的 testbench 直接通过 `icache_mem_req/rsp`、`dcache_mem_req/rsp` 原始接口提供，绕过了 L2 和片上互联。这种模式省去了缓存复杂度，便于单独验证 SM 流水线。

---

## 5. 综合实践

把本讲学的「控制流 + 完成回报 + 数据流」串起来，完成下面这个贯穿任务。

**任务**：以 [GPGPU_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v) 为蓝本，画出一张完整的「主机发起一个 workgroup 到收到完成回报」的全流程图，并在图上标注：

1. **控制流入口**（红色）：`host_req_valid_i` 经 [cta_interface 例化 L277](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L277) → `cta_scheduler` → 广播 `cta2warp_*` → 某个 `sm_wrapper.cta_req_*`。
2. **完成回报链**（蓝色）：warp 完成 → `warp2cta_*` → `cta_interface` 内 `wf_done_interface` → `wg_done`（[L295](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L295)）→ `cache_invalid` + `is_flushing`（[L253-L270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L253-L270)）→ SM 刷 dcache → L2 `finish_issue_o` → `host_rsp_valid_o`。
3. **数据流旁路**（绿色）：SM 的 `mem_req` 经 `sm2cluster_arb → l2_distribute → cluster_to_l2_arb → Scheduler` → `out_a/out_d`。

**额外要求**：在图上用一句话标注 `wg_done` 与 `host_rsp_valid_o` 为什么不是同一个信号（提示：4.3 的缓存冲刷握手）。再把当前实现的两个简化假设（`cache_invalid` 只刷 SM0、NO_CACHE 硬编码 SM 索引）标成「待完善」。

> 这是一个**源码阅读型实践**，不需要跑仿真。完成后你就掌握了 GPGPU_top 的全景数据通路，为下一讲进入 CTA 调度器内部做好了准备。

## 6. 本讲小结

- `GPGPU_top` 是系统「主板」，本身不做运算，靠例化 `cta_interface`、`sm_wrapper ×N`、`Scheduler ×M` 并连线组成系统；默认 `NUM_SM=2, NUM_L2CACHE=1`。
- **控制流**：`host_req_*` 原样进 `cta_interface` → `cta_scheduler` 选中一个 CU → 把同一份派发参数**广播**给所有 CU，靠 `cta2warp_valid[i]` 的 one-hot **点选**目标 SM 的 `cta_req_*`。
- **完成回报**有缓存冲刷握手：warp 全部完成产生 `wg_done` → 置 `is_flushing` 并对 SM 发 `cache_invalid` → SM 把 dcache 脏块刷下去 → L2 处理完产生 `l2cache_finish_issue` → 才拉起 `host_rsp_valid_o = l2cache_finish_issue && is_flushing`。即**必须等 L2 刷完缓存才回报主机**。
- **数据流**：SM 的 `mem_req` 依次穿过 `sm2cluster_arb`（cluster 内仲裁）→ `l2_distribute`（按地址分发到各 L2）→ `cluster_to_l2_arb`（L2 入口仲裁）→ `Scheduler`(L2)，缺失再经 `out_a/out_d` 对外。
- `NO_CACHE` 宏用 `` `ifdef `` 同时切换端口与内部连线：默认带 L2 + TileLink 对外（可套 `gpgpu_axi_top` 转 AXI）；NO_CACHE 则把 icache/dcache 原始接口直接引出，便于早期调试，但不与 AXI 壳兼容。
- 当前实现有两个简化：`cache_invalid` 目前只触发 SM[0]（注释 `TODO: cache_invalid can't multi SM`）；NO_CACHE 直连代码硬编码了单个 SM 索引。

## 7. 下一步学习建议

- **下一讲 u2-l1（CTA 调度器与资源表）**：本讲把 `cta_interface` 当黑盒，只看了它的对外握手。下一步就打开它内部的 `cta_scheduler` 和 `resource_table`，看调度器**如何根据 VGPR/SGPR/LDS/slot 资源**决定一个 workgroup 能否派发、派给哪个 CU。
- **u2-l2（cu_handler 与 inflight_wg_buffer）**：进一步看派发字段（wf_tag、基址、start_pc）是怎么组装出来的，以及 `wf_done` 是怎么被回收并最终汇聚成本讲看到的 `wg_done`。
- **u7-l2 / u7-l3（L2 与互联）**：本讲只看了 `Scheduler` 在顶层的位置和 `finish_issue_o` 的用途；L2 内部的 directory/banked_store/MSHR，以及三个互联模块的仲裁与地址分发算法，留到进阶单元深入。
- **延伸阅读**：对照 [gpgpu_axi_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv) 的三个例化（`axi4lite_2_cta` / `GPGPU_top` / `axi4_adapter_top`），理解「AXI4-Lite 主机控制 + AXI4 访存」这一整套对外接口是如何在 GPGPU_top 之外再包一层的。
