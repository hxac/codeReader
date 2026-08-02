# AXI4 适配器与 host 接口

## 1. 本讲目标

本讲是单元 7（片上互联与 L2）的最后一讲，把目光从片内移到**片外**：Ventus GPGPU 如何与"外部世界"打交道。GPGPU 内部用 TileLink（A/D 通道）说话，但外部存储与主机却用 AXI4 / AXI4-Lite。学完本讲，读者应能：

- 理解 `gpgpu_axi_top` 如何把"主机 AXI4-Lite 接口 + 内部 GPGPU_top + 对外 AXI4 接口"三者总装成一块完整的 SoC 子系统。
- 读懂 `axi4_adapter` 的状态机，说清楚一条 L2 的 TileLink GET/PUT 请求如何被翻译成 AXI 的 AR/R 或 AW/W/B 事务，响应又如何被还原回 TileLink D 通道。
- 掌握 `axi4lite_2_cta` 如何用 AXI4-Lite 的 18 个寄存器组（`data_buf`）承载一次 workgroup 派发的全部参数，并触发 CTA 调度。
- 理解 `spill_register` / `axi_cut` 这类"缓冲寄存器"为何能切断组合长路径、改善时序，以及它们在 `axi4_adapter_top` 中的具体用法。

## 2. 前置知识

在进入正文前，先用最朴素的方式澄清几个概念。

- **协议（protocol）vs 接口（interface）**：TileLink 和 AXI4 都是片上总线协议，规定了"谁发请求、谁回响应、用什么字段描述事务"。Ventus 内部（L1↔L2↔互联）用 TileLink，对外（连 DRAM/主机）用 AXI。两者不能直接对接，需要一个"翻译器"，这就是 adapter。
- **AXI4 的五个通道**：读地址（AR）、读数据（R）、写地址（AW）、写数据（W）、写响应（B）。每个通道都是独立的 `valid/ready` 握手。一次写事务要走 AW→W→B，一次读事务要走 AR→R。
- **AXI4-Lite**：AXI4 的"轻量版"，每次只能传一个数据（不支持 burst 突发），常用于配置寄存器。本讲中主机用它写入派发参数。
- **TileLink 的 A/D 通道**：Ventus 只用 TileLink 的子集——A 通道发请求（带地址、操作码 opcode、source、mask、data），D 通道回响应。这一点在 [u7-l1](u7-l1-tilelink-protocol.md) 已详细讲过，本讲直接复用。
- **source 字段是"回信地址"**：A 通道请求里带的 `source`，会被响应原样带回，供发起方匹配"这是哪笔请求的响应"。本讲会看到：跨入 AXI 域时，这个 source 被"借用"成 AXI 的事务 ID（`id`），从而让响应能找到回家的路。
- **组合长路径（combinational long path）**：如果一拍时钟内信号要从模块 A 的输入直通到模块 B 的输出（中间不经过任何寄存器），这条路径就很"长"，时序难闭合。`spill_register` 就是在路径中间插一拍寄存器，把长路径切断。

如果你对 L2 Cache（Scheduler）和 GPGPU_top 的整体数据流还不熟，建议先读 [u7-l2](u7-l2-l2cache-scheduler.md) 和 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md)。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/gpgpu_top/gpgpu_axi_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv) | 顶层"壳"：例化 `axi4lite_2_cta`、`GPGPU_top`、`axi4_adapter_top`，把三者的 host 接口与 TileLink A/D 接口对接，并在适配器两侧做 TileLink↔简易总线的胶水逻辑。 |
| [src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv) | 适配器顶层：定义 AXI 各通道的 `struct packed` 类型，例化 `axi4_adapter`（核心转换）与 `axi_cut`（时序切断），把 struct 接口拆/合到扁平 AXI 端口。 |
| [src/gpgpu_top/axi4_adapter/axi4_adapter.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv) | 核心转换器：一个状态机把"单笔 read/write 请求"翻译成 AXI 的多通道握手序列，并回收 R/B 响应。源自开源 AXI 适配器设计。 |
| [src/gpgpu_top/axi4lite_2_cta.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v) | AXI4-Lite 从机：用一段 `data_buf` 寄存器组保存主机写入的派发参数，凑齐后产生 `host_req_valid` 驱动 CTA 调度，并回收 `host_rsp` 完成信号。 |
| [src/gpgpu_top/axi4_adapter/spill_register.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/spill_register.sv) 与 [spill_register_flushable.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/spill_register_flushable.sv) | 时序优化单元：带握手、可切断组合路径的"缓冲寄存器"，并提供 `Bypass` 旁路与 `flush` 冲刷能力。 |
| [src/gpgpu_top/axi4_adapter/axi4_cut.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_cut.sv) | AXI 切断器：对 AXI 五个通道各套一个 `spill_register`，整体打断输入到输出的组合路径。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 配置总开关：定义 `AXI_*`、`AXILITE_*` 位宽宏，以及 TileLink 操作码 `TLAOP_*`。 |
| [testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv) 与 [host_inter.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv) | 仿真平台：例化 `gpgpu_axi_top` 作 DUT，用 `host_inter` 模拟主机经 AXI4-Lite 派发，用 `axi_ram` 模拟外部存储。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看顶层总装（4.1），再钻进协议转换器（4.2）与主机接口（4.3），最后讲时序优化单元（4.4）。

### 4.1 gpgpu_axi_top：三部件总装与系统数据流

#### 4.1.1 概念说明

`GPGPU_top`（见 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md)）本身只暴露 TileLink 风格的 `out_a/out_d` 对外存储接口和一组 `host_req/host_rsp` 派发接口。要让它变成一块能挂在 AXI 总线上的"成品 IP"，还需要两件外套：

1. **对外存储外套**：把 `out_a/out_d`（TileLink）翻译成标准 AXI4 主机端口（`m_axi_*`），这样 GPGPU 就能读写外部 DRAM。
2. **主机控制外套**：把 `host_req/host_rsp`（自定义派发接口）翻译成标准 AXI4-Lite 从机端口（`s_axilite_*`），这样主机 CPU 只需像写一段寄存器一样就能下发一个 workgroup。

`gpgpu_axi_top` 就是同时穿上这两件外套的顶层模块。它的端口分两组：`s_axilite_*`（面向主机，从机）和 `m_axi_*`（面向外部存储，主机）。

#### 4.1.2 核心流程

`gpgpu_axi_top` 内部例化三个子模块，形成两条独立的通路：

```
                  ┌──────────────── gpgpu_axi_top ────────────────┐
   主机 CPU        │                                                │  外部 DRAM
   AXI4-Lite  <───►│  axi4lite_2_cta  ──host_req/rsp──►  GPGPU_top │              │
   (s_axilite_*)   │                          ▲              │     │              │
                   │                          │           out_a/d  │  axi4_adapter_top
                   │                          └──────────────┘─────┼──► (m_axi_*) ──► AXI RAM
                   └────────────────────────────────────────────────┘
```

- **控制通路（左侧）**：主机经 AXI4-Lite 写入派发参数 → `axi4lite_2_cta` 把参数拼成 `host_req_*` → 送入 `GPGPU_top` 的 CTA 调度器；workgroup 完成后 `host_rsp_*` 回送，主机可经 AXI4-Lite 读回完成状态。
- **数据通路（右侧）**：`GPGPU_top` 内部 L2 的缺失请求经 `out_a` → `gpgpu_axi_top` 的胶水逻辑 → `axi4_adapter_top` 翻译成 AXI4 → 读写外部 DRAM；AXI 响应再经反向路径还原成 `out_d` 回送 L2。

注意：`GPGPU_top` 内部把 `out_a_*`/`out_d_*` 直接别名到 L2 的 `l2cache_out_a_*`/`l2cache_out_d_*`（见 [GPGPU_top.v:564-579](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L564-L579)），所以这里的 `out_a/out_d` 就是 L2 Scheduler 对外的 TileLink 接口。这与 [u7-l2](u7-l2-l2cache-scheduler.md) 讲的 L2 四通道相衔接：L2 在片内收 L1 请求，片外用 `out_a` 取数。

#### 4.1.3 源码精读

`gpgpu_axi_top` 的端口声明分两大块：AXI4-Lite 从机端口（[gpgpu_axi_top.sv:21-43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L21-L43)）与 AXI4 主机端口（[gpgpu_axi_top.sv:45-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_top.sv#L45-L93)）。模块体内最关键的是 L2 TileLink 接口与适配器"简易总线"接口之间的胶水逻辑。

L2 对外的 TileLink A/D 通道（`top_out_a_*` / `top_out_d_*`）声明在 [gpgpu_axi_top.sv:134-150](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L134-L150)：

```verilog
wire [`NUM_L2CACHE*`OP_BITS-1:0]    top_out_a_opcode;
wire [`NUM_L2CACHE*`SOURCE_BITS-1:0] top_out_a_source;
wire [`NUM_L2CACHE*`ADDRESS_BITS-1:0] top_out_a_address;
wire [`NUM_L2CACHE*`MASK_BITS-1:0]    top_out_a_mask;
wire [`NUM_L2CACHE*`DATA_BITS-1:0]    top_out_a_data;
...
wire [`NUM_L2CACHE*`DATA_BITS-1:0]    top_out_d_data;
```

适配器 `axi4_adapter_top` 不懂 TileLink，它懂的是一套"单笔 read/write"的简易总线（`req_i/type_i/we_i/addr_i/wdata_i/be_i/size_i/id_i` 与 `valid_o/rdata_o/id_o`）。于是 `gpgpu_axi_top` 必须做两件事：**把 A 通道翻译成简易请求**，**把简易响应还原成 D 通道**。

**请求方向（A → 简易请求）** 见 [gpgpu_axi_top.sv:179-188](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L179-L188)：

```verilog
assign req_i   = top_out_a_valid;
assign top_out_a_ready = !busy_o;          // 适配器忙时不接新请求（单笔串行）
assign type_i  = 1'd0;                      // 0=单笔请求，1=cacheline 突发；这里恒为单笔
assign we_i    = (top_out_a_opcode != 'h4); // opcode==4(GET)为读，其余为写
assign addr_i  = top_out_a_address;
assign size_i  = 3'd3;                      // 8 字节
assign id_i    = top_out_a_source;          // source 字段"借用"为 AXI 事务 ID
```

这里的关键判定是 `we_i`：TileLink 操作码 `TLAOP_GET=4` 表示读，其余（`PUTFULL=0`、`PUTPART=1`、`FLUSH=5`）按写处理。操作码定义在 [define.v:271-277](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L271-L277)。`top_out_a_ready = !busy_o` 表明：只要适配器还处在某笔事务中（`busy_o=1`），就反压 L2 不再接收新请求——这是典型的**单笔在途（single in-flight）**简化设计。

`id_i = top_out_a_source` 是 source 字段跨协议的"接力棒"：TileLink 的回信地址被当作 AXI 的事务 ID 带出去，等响应回来再凭 ID 找回原主（详见 4.2）。

**响应方向（AXI 响应 → D 通道）** 用一段时序逻辑把 AXI 的 R/B 响应重新装配成 D 通道字段，见 [gpgpu_axi_top.sv:152-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L152-L166)：

```verilog
always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin ... end
  else begin
    // R 通道有效=读响应(opcode=1)，B 通道有效=写响应(opcode=0)
    mem_rsp_opcode <= (m_axi_rvalid_i) ? 'h1 : ((m_axi_bvalid_i) ? 'h0 : mem_rsp_opcode);
    mem_rsp_source <= (m_axi_rvalid_i) ? m_axi_rid_i : ((m_axi_bvalid_i) ? m_axi_bid_i : mem_rsp_source);
  end
end
```

然后这些寄存值接到 D 通道（[gpgpu_axi_top.sv:190-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L190-L195)）：`top_out_d_valid = valid_o`（来自适配器）、`top_out_d_opcode = mem_rsp_opcode`、`top_out_d_source = mem_rsp_source`、`top_out_d_data = rdata_o`。对照 TileLink 语义：D 通道 opcode=1 表示 `AccessAckData`（读数据），opcode=0 表示 `AccessAck`（写应答）——与 R/B 的对应关系天然吻合。

三个子模块的例化分别在：`axi4lite_2_cta`（[gpgpu_axi_top.sv:197-251](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L197-L251)）、`GPGPU_top`（[253-326](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L253-L326)，注意其 `ifdef NO_CACHE` 分支）、`axi4_adapter_top`（[328-416](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L328-L416)）。值得注意的是 `axi4_adapter_top` 的 `DATA_WIDTH` 被实参化为 `64`、`CACHELINE_BYTE_OFFSET` 为 `3`。

#### 4.1.4 代码实践

**实践目标**：在仿真平台里确认 `gpgpu_axi_top` 的三部件连接关系，并把 L2 的一次外部访问与 AXI 上的事务对应起来。

**操作步骤**：

1. 打开 [test_gpu_axi_top.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv)，确认 DUT 是 `gpgpu_axi_top u_dut`（[第 83 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L83)），其 `m_axi_*` 端口连到 `axi_ram #(.DATA_WIDTH(64)) u_ram`（[第 199-242 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L199-L242)）。
2. 在某测试用例（如 `tc_vecadd`）下运行 `make run-vcs-4w4t`（见 [u1-l4](u1-l4-simulation-and-testcases.md)），用 `make verdi` 打开 `test.fsdb` 波形。
3. 在波形中同时选中：`u_dut.top_out_a_valid`、`u_dut.top_out_a_opcode`、`u_dut.m_axi_arvalid_o`（或 `m_axi_awvalid_o`）、`u_dut.m_axi_rvalid_i`（或 `m_axi_bvalid_i`）、`u_dut.top_out_d_valid`。
4. 找一次 `top_out_a_valid` 拉高的时刻：观察 `top_out_a_opcode` 是否为 `4`（读）或 `0`（写），并核对紧随其后的 `m_axi_arvalid`/`m_axi_awvalid` 是否与之对应。

**需要观察的现象**：

- `top_out_a_valid` 拉高时 `top_out_a_ready`（即 `!busy_o`）是否同时为高；适配器开始处理后 `busy_o` 变高，期间不会再接收下一笔 `top_out_a_valid`。
- 读请求（opcode=4）应触发 `m_axi_arvalid_o`→`m_axi_rvalid_i` 链路；写请求（opcode=0）应触发 `m_axi_awvalid_o`→`m_axi_wvalid_o`→`m_axi_bvalid_i` 链路。
- 响应回送时 `top_out_d_valid` 与 `top_out_d_opcode`（读=1、写=0）是否正确。

**预期结果**：能在波形上清晰地看到"一笔 `top_out_a` 对应一拍 AXI 事务、再对应一笔 `top_out_d`"的串行对应关系。若仿真环境暂不可用，本步骤标注为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`gpgpu_axi_top` 里 `top_out_a_ready = !busy_o`，这意味着 L2 对外存储的并发度受限。请说明这种简化对 L2 性能的影响。

> **答案**：`busy_o` 在适配器状态机非 `IDLE` 时为高（见 4.2.3），意味着任意时刻 L2 到外部 DRAM 最多只有一笔事务在途。L2 即便有多个 MSHR 缺失，也只能排队逐笔发往 DRAM，外部访存吞吐受限。这是开源版本为简化设计所做的取舍，工程化时通常会用支持多 in-flight 的 AXI 适配器替换。

**练习 2**：为什么 `id_i = top_out_a_source` 这一步是必要的？如果直接把 `id_i` 固定为常量 0，会出现什么问题？

> **答案**：AXI 用 `id` 区分并发响应的归属。把 TileLink 的 `source` 借作 `id` 带出，是为了让 DRAM 的响应（`rid`/`bid`）能原样带回这个标识，进而还原成 D 通道的 `source`，使 L2 能把响应匹配到正确的在途请求。若 `id` 恒为 0，则所有响应的标识都相同，在多笔在途时无法区分归属（本设计靠单笔串行规避了这个问题，但失去了并发能力）。

---

### 4.2 axi4_adapter_top / axi4_adapter：TileLink→AXI4 协议转换

#### 4.2.1 概念说明

`axi4_adapter` 是协议翻译的核心。它的左边是一条**单笔 read/write 的简易总线**（一次只描述一个事务：地址、读/写、数据、字节使能、ID），右边是**完整的 AXI4 五通道接口**。它的职责是：把一笔简单的读请求展开成 AXI 的 `AR→R` 握手序列，把一笔写请求展开成 `AW→W→B` 握手序列。

为什么需要"展开"？因为 AXI 把一次事务拆成了多个独立通道（地址和数据分开发），而简易总线把它们打包在一起。适配器内部用一个**状态机**协调这些通道的时序：什么时候拉 `ar_valid`、什么时候收 `r_valid`、什么时候才算这笔事务完成。

`axi4_adapter_top` 则是它的外壳：定义 AXI 各通道的 `struct packed` 类型（把扁平信号打包成结构体，方便整体传递），并在适配器输出端套一个 `axi_cut`（见 4.4）来切断组合路径。

#### 4.2.2 核心流程

适配器把"一笔事务"的生命周期建模为一个状态机，核心状态如下（定义在 [axi4_adapter.sv:111-122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L111-L122)）：

```
读请求路径：                          写请求路径：
  IDLE                                  IDLE
   │ (req_i && !we_i)                    │ (req_i && we_i)
   ▼                                     ▼
  发 ar_valid ──ar_ready──►            发 aw_valid + w_valid
  WAIT_R_VALID                          (同时发地址与首拍数据)
   │ 收到 r_valid (last)                 │
   ▼                                     ├─ aw/w 都就绪 ─► WAIT_B_VALID
  COMPLETE_READ                         ├─ 只 w 就绪 ──► WAIT_LAST_W_READY
   │                                     ├─ 只 aw 就绪 ─► WAIT_AW_READY
   ▼                                     │
  IDLE                                  WAIT_B_VALID (收 b_valid)
                                         │
                                         ▼
                                        IDLE
```

由于 `gpgpu_axi_top` 把 `type_i` 恒置为 `SINGLE_REQ`（单笔）、`size_i=3`（8 字节）且 `DATA_WIDTH=AXI_DATA_WIDTH=64`，所以 `BURST_SIZE = 64/64-1 = 0`，即每次都是**单拍、单 beat** 的 AXI 事务（`ar.len=0`、`w.last=1`）。这把状态机简化到最简形态：读走 `IDLE→WAIT_R_VALID→COMPLETE_READ`，写走 `IDLE→WAIT_B_VALID`（aw 与 w 同拍就绪时）。

`busy_o = (state_q != IDLE)`（[axi4_adapter.sv:160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L160)），正是 4.1 中 `top_out_a_ready = !busy_o` 的来源。

#### 4.2.3 源码精读

**字段映射（组合默认值）**：状态机在 `always_comb` 开头给 AXI 各通道字段赋默认值（[axi4_adapter.sv:162-205](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L162-L205)），把简易总线字段搬到 AXI 通道：

```verilog
axi_req_o.aw.addr  = addr_i;
axi_req_o.aw.size  = size_i;            // 1/2/4/8 字节
axi_req_o.aw.burst = BURST_INCR;        // 增量突发
axi_req_o.aw.cache = CACHE_MODIFIABLE;
axi_req_o.aw.id    = id_i;              // ← source 借用的 ID
axi_req_o.aw.atop  = atop_from_amo(amo_i);
axi_req_o.ar.addr  = addr_i;
axi_req_o.ar.size  = {1'b0, size_i};
axi_req_o.ar.burst = (CRITICAL_WORD_FIRST ? BURST_WRAP : BURST_INCR);
axi_req_o.ar.id    = id_i;
axi_req_o.w.data   = wdata_i[0];
axi_req_o.w.strb   = be_i[0];           // ← TileLink mask 映射为 AXI 字节使能
```

可以看到字段一一对应：`addr_i→aw/ar.addr`、`id_i→aw/ar.id`、`be_i→w.strb`（TileLink 的 `mask` 字节使能 → AXI 的 `wstrb`）、`wdata_i→w.data`。

**读请求发起（IDLE 态）**：[axi4_adapter.sv:289-311](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L289-L311)，当 `req_i && !we_i` 且无在途写时，拉 `ar_valid`，`ar_ready` 后进入 `WAIT_R_VALID`：

```verilog
end else begin // read
  if (!any_outstanding_aw) begin
    axi_req_o.ar_valid = 1'b1;
    gnt_o = axi_resp_i.ar_ready;
    if (type_i != SINGLE_REQ) begin
      axi_req_o.ar.len = BURST_SIZE[7:0];   // 单笔时 len=0
      cnt_d = BURST_SIZE[ADDR_INDEX-1:0];
    end
    if (axi_resp_i.ar_ready) begin
      state_d = (type_i == SINGLE_REQ) ? WAIT_R_VALID : WAIT_R_VALID_MULTIPLE;
    end
  end
end
```

**读数据回收（WAIT_R_VALID）**：[axi4_adapter.sv:463-500](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L463-L500)，收到 `r_valid` 时把数据存入 `cache_line_d`，`r.last` 时进入 `COMPLETE_READ`，最终 `valid_o=1` 把整笔读结果交给上层。

**写请求发起与 B 通道回收**：写路径在 [axi4_adapter.sv:234-287](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L234-L287)（IDLE 中同时拉 `aw_valid` 与 `w_valid`），收 `b_valid` 在 `WAIT_B_VALID`（[403-449](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L403-L449)），`b_valid && !any_outstanding_aw` 时拉 `b_ready` 并置 `valid_o=1`，回到 `IDLE`。

> 关于 source↔ID 的位宽：`AXI_ID_WIDTH=4`（[define.v:230](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L230)），而 `SOURCE_BITS` 更宽（含 SM/cluster/set/entry 等编码，见 [u7-l1](u7-l1-tilelink-protocol.md)）。`gpgpu_axi_top.sv` 中 `id_i = top_out_a_source` 会在赋值时按低位截取，响应侧 `mem_rsp_source`（`SIZE_BITS` 位）由 `rid/bid` 截取后再零扩展回 `top_out_d_source`。这是开源版本为"单笔串行 + 简易路由"所做的简化；在多 in-flight 场景下能否完整保留 source 路由信息，**待本地验证**。本讲重点在于"source 经 AXI ID 往返"这一机制本身。

**顶层封装 axi4_adapter_top**：先用 `typedef struct packed` 定义 aw/w/b/ar/r 五种通道类型与 `axi_req_t`/`axi_rsp_t`（[axi4_adapter_top.sv:111-180](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L111-L180)），然后例化 `axi4_adapter`（[316-361](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L316-L361)）输出 struct 形式的 `adapter_axi_req_o`，再经 `axi_cut`（[363-379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L363-L379)）切断后，由一连串 `assign` 拆成扁平的 `m_axi_*` 端口（[190-264](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L190-L264)）。

#### 4.2.4 代码实践

**实践目标**：把 TileLink 操作码到 AXI 事务类型的映射整理清楚，并在源码中逐条对应。

**操作步骤**：

1. 列出 TileLink 操作码（[define.v:271-277](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L271-L277)）：`GET=4`、`PUTFULL=0`、`PUTPART=1`、`FLUSH=5`。
2. 对照 [gpgpu_axi_top.sv:184](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L184) 的 `we_i = (top_out_a_opcode != 'h4)`，确定每个操作码对应的读/写。
3. 在 [axi4_adapter.sv:228-313](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L228-L313) 的 `IDLE` 态里，分别找到 `we_i` 为真（走 `aw_valid`/`w_valid`）与为假（走 `ar_valid`）的两段代码。
4. 填写下面这张映射表（示例代码，非项目原有）：

   | TileLink opcode | 数值 | we_i | AXI 通道序列 |
   |---|---|---|---|
   | `TLAOP_GET` | 4 | 0（读） | AR → R |
   | `TLAOP_PUTFULL` | 0 | 1（写） | AW → W → B |
   | `TLAOP_PUTPART` | 1 | 1（写） | AW → W → B |
   | `TLAOP_FLUSH` | 5 | 1（写） | AW → W → B |

**需要观察的现象**：是否所有"写类"操作码（≠4）都汇入同一段 `if (we_i)` 分支；读类是否独占 `else` 分支。

**预期结果**：得到一张清晰的"操作码 → we_i → AXI 通道"映射表，理解 `!= 'h4` 这个判定如何把 TileLink 的多操作码归并为 AXI 的读/写两类。

#### 4.2.5 小练习与答案

**练习 1**：适配器状态机里有一个 `WAIT_R_VALID_MULTIPLE` 状态（[axi4_adapter.sv:463](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L463)）。在 `gpgpu_axi_top` 的实际配置下，这个状态会被进入吗？为什么？

> **答案**：不会。`gpgpu_axi_top` 把 `type_i` 恒置为 `SINGLE_REQ`（[gpgpu_axi_top.sv:181](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L181)），读请求只会进入 `WAIT_R_VALID` 而非 `WAIT_R_VALID_MULTIPLE`。后者是为 cacheline 突发读（`type_i=CACHE_LINE_REQ`）准备的，本配置下用不到。

**练习 2**：`be_i`（来自 TileLink 的 `mask`）和 AXI 的 `wstrb` 是什么关系？为什么 `axi_req_o.w.strb = be_i[0]` 只取下标 0？

> **答案**：两者都是"字节使能"，指示本拍数据中哪些字节有效。`be_i` 是按 `(DATA_WIDTH/AXI_DATA_WIDTH)` 分拍的数组，由于本配置 `DATA_WIDTH=AXI_DATA_WIDTH=64`，分拍数为 1，只有 `be_i[0]` 一拍，故直接取下标 0。若两者不等（如 cacheline 模式），则需要按 beat 序号选 `be_i[k]`。

---

### 4.3 axi4lite_2_cta：AXI4-Lite 主机接口驱动 CTA 派发

#### 4.3.1 概念说明

主机 CPU 如何"命令"GPGPU 开始计算？答案不是发一条指令，而是**写一组寄存器**。一个 workgroup 的派发需要十几个参数：wg_id、warp 数、起始 PC、各类寄存器/共享内存用量、基地址等（见 [u2-l1](u2-l1-cta-scheduler-and-resource-table.md)）。`axi4lite_2_cta` 把这些参数组织成 18 个 32 位寄存器（`data_buf`），主机用 AXI4-Lite 逐个写入，写完"启动位"后，模块自动把全部参数拼成 `host_req_*` 接口送给 CTA 调度器。

它本质上是一个**AXI4-Lite 从机 + 寄存器文件 + 输出握手状态机**。同时它还反向回收 `host_rsp_*`（workgroup 完成信号），主机可经 AXI4-Lite 读回完成状态。

#### 4.3.2 核心流程

模块内有两套状态机：

1. **AXI4-Lite 从机状态机**（`state`，[axi4lite_2_cta.v:139-218](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L139-L218)）：标准的 `IDLE→WRITEADDR→WRITEDATA→WRITERESP`（写）与 `IDLE→READADDR→READDATA`（读）。写事务把数据存入 `data_buf[addr]`，读事务从 `data_buf[addr]` 取出。
2. **派发输出状态机**（`out_state`，[axi4lite_2_cta.v:103-124](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L103-L124)）：两态 `OUT_IDLE/OUT_OUTPUT`。当 `data_buf[0]`（启动寄存器）被写为 1 时进入 `OUT_OUTPUT`，拉起 `host_req_valid_o`，与 `host_req_ready_i` 握手成功后回 `OUT_IDLE` 并清 `data_buf[0]`。

```
主机写 reg[1..15] 各项派发参数 ──► data_buf[1..15]
主机写 reg[0]=1（启动位）    ──► data_buf[0]=1
                                     │
                       out_state: OUT_IDLE ──► OUT_OUTPUT
                                     │ host_req_valid_o=1
                                     │ 与 host_req_ready_i 握手
                                     ▼
                                  OUT_IDLE（清 data_buf[0]）
```

`host_req_*` 各字段直接从 `data_buf` 的对应槽位切片输出（[axi4lite_2_cta.v:254-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L254-L270)），例如 `host_req_wg_id_o = data_buf[1]`、`host_req_start_pc_o = data_buf[4]` 等。寄存器号与主机写入地址的对应关系是：`addr = 寄存器号`，每个寄存器占 4 字节，所以 `addr = s_axilite_awaddr_i[31:2]`（右移 2 位去掉字节偏移，[第 180 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L180)）。

#### 4.3.3 源码精读

`NUM_REG = 18`（[axi4lite_2_cta.v:76](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L76)），`data_buf` 是 `18*32` 位的寄存器组（[第 88 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L88)）。各槽位含义：

- `data_buf[0]`：启动位（host_req_valid），写 1 触发派发。
- `data_buf[1..15]`：派发参数（wg_id、num_wf、wf_size、start_pc、vgpr/sgpr/lds 用量、pds/csr 基地址、kernel_size_3d 等）。
- `data_buf[16]`：完成的 wg_id（由 `host_rsp` 写入）。
- `data_buf[17]`：完成标志位（host_rsp 到来时置 1，主机读后清 0）。

**派发输出逻辑**（[axi4lite_2_cta.v:254](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L254)）：

```verilog
assign host_req_valid_o = data_buf[0] && (out_state == OUT_OUTPUT);
assign host_req_wg_id_o  = data_buf[(1+1)*AXILITE_DATA_WIDTH-1 -: AXILITE_DATA_WIDTH];
assign host_req_start_pc_o = data_buf[(4+1)*AXILITE_DATA_WIDTH-1 -: AXILITE_DATA_WIDTH];
...
```

`host_req_valid_o` 同时依赖启动位与输出状态机，保证只在 `OUT_OUTPUT` 态拉高。

**完成回收逻辑**（[axi4lite_2_cta.v:232、272-292](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L272-L292)）：当 `host_rsp_valid_i` 到来且完成位未占（`data_buf[17]==0`）时，`host_rsp_ready_o` 拉高接收，并把完成 wg_id 存入 `data_buf[16]`、`data_buf[17]` 置 1；主机读 `data_buf[17]`（地址 0x44）后该位清 0。

```verilog
assign host_rsp_ready_o = host_rsp_valid_i && (data_buf[17*AXILITE_DATA_WIDTH]==1'd0);
...
else if(host_rsp_valid_i && (!data_buf[17*AXILITE_DATA_WIDTH])) begin
  data_buf[(17+1)*AXILITE_DATA_WIDTH-1 -: AXILITE_DATA_WIDTH] <= 'b1;            // 完成标志
  data_buf[(16+1)*AXILITE_DATA_WIDTH-1 -: AXILITE_DATA_WIDTH] <= host_rsp_..._wg_id_i;
end
```

**主机驱动的真实写照**：仿真平台的 `host_inter.sv` 完整演示了主机如何用 AXI4-Lite 配置一次派发。`drv_gpu` 任务（[host_inter.sv:130-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L130-L160)）依次写 16 个寄存器，最后写 `reg[0]=1`：

```verilog
axilite_write(32'h0000_0004,{`WG_ID_WIDTH{1'd0}}); //reg[1] host_req_wg_id
axilite_write(32'h0000_0008,wg_size[31:0]);        //reg[2] host_req_num_wf
axilite_write(32'h0000_000c,wf_size[31:0]);        //reg[3] host_req_wf_size
axilite_write(32'h0000_0010,32'h8000_0000);        //reg[4] host_req_start_pc
...
axilite_write(32'h0000_0000,32'd1);                //reg[0] host_req_valid  ← 启动！
```

这里地址 `0x04` 即 `reg[1]`（`0x04>>2 = 1`），`0x00` 即 `reg[0]`。`exe_finish` 任务（[host_inter.sv:214-246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L214-L246)）则轮询读 `0x44`（`reg[17]`）直到其为 1，判定 workgroup 完成。

#### 4.3.4 代码实践

**实践目标**：对照仿真平台的真实写序列，画出"主机写寄存器 → 触发 CTA 派发"的完整时序。

**操作步骤**：

1. 读 [host_inter.sv:130-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L130-L160)，把每条 `axilite_write(地址, 值)` 的地址换算成寄存器号（地址>>2）。
2. 对照 [axi4lite_2_cta.v:255-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4lite_2_cta.v#L255-L270)，确认每个寄存器号对应的 `host_req_*` 字段。
3. 在波形（`test.fsdb`）中跟踪 `u_dut.axi2cta.data_buf[0]`（启动位）从 0 变 1 的时刻，观察紧随其后的 `host_req_valid_o` 与 `host_req_ready_i` 握手。
4. 跟踪 `host_rsp_valid_i` 到来时 `data_buf[17]` 被置 1，以及主机读 `0x44` 后该位清 0。

**需要观察的现象**：

- `data_buf[0]` 写 1 后，`out_state` 是否在下一拍进入 `OUT_OUTPUT`，`host_req_valid_o` 是否拉高。
- 握手成功后 `data_buf[0]` 是否清 0、`out_state` 是否回 `OUT_IDLE`。
- `host_rsp_valid_i` 与 `data_buf[17]` 置位的因果关系。

**预期结果**：得到一张从"主机写 reg[0]=1"到"`host_req_valid` 握手"再到"完成回读 reg[17]"的完整时序图。波形部分若环境不可用则标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi4lite_2_cta` 要用两个状态机（`state` 与 `out_state`）而不是一个？

> **答案**：两个状态机解耦了两件独立的事：`state` 负责 AXI4-Lite 从机的标准握手（何时收地址、收数据、回响应），`out_state` 负责把已写好的参数送出去（何时拉 `host_req_valid`、何时回收）。它们节奏不同——前者跟随主机写节拍，后者跟随 CTA 调度器的 ready 节拍。分离后逻辑清晰，且允许主机在派发进行中继续写下一组参数。

**练习 2**：主机如何知道一个 workgroup 已经跑完？请指出对应的寄存器与地址。

> **答案**：workgroup 完成时 CTA 调度器回送 `host_rsp_valid_i`，`axi4lite_2_cta` 把完成 wg_id 写入 `data_buf[16]` 并把 `data_buf[17]`（完成标志）置 1。主机用 AXI4-Lite 读地址 `0x44`（即 `reg[17]`），读到非 0 即表示完成；读后该位自动清 0。`host_inter.sv` 的 `exe_finish` 正是轮询 `0x44`（[第 224 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L224)）。

---

### 4.4 spill_register / axi_cut：接口时序优化单元

#### 4.4.1 概念说明

当模块 A 的输出直接组合驱动模块 B 的输入，而 B 的输出又组合反馈回 A，就可能形成一条贯穿多级的**组合长路径**，导致时序无法闭合（时钟频率上不去）。解决办法是在路径中间插一拍寄存器，把长路径"切断"。

`spill_register`（"溢出寄存器"）就是这样一个带握手的切断单元：它像一个深度为 1 的弹性缓冲，输入侧 `valid_i/ready_o`，输出侧 `valid_o/ready_i`，内部用寄存器暂存数据。它的关键能力是**完全打断输入到输出的组合路径**——输入的 `ready_o` 不再组合依赖于输出的 `ready_i`，反之亦然。

`axi_cut` 则是 AXI 版本的切断器：对 AXI 五个通道各套一个 `spill_register`，整体切断 AXI 接口的组合路径。在 `axi4_adapter_top` 中，它被放在 `axi4_adapter` 的输出端（[axi4_adapter_top.sv:363-379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L363-L379)），用于隔离适配器与外部 AXI 互联。

#### 4.4.2 核心流程

`spill_register` 是 `spill_register_flushable` 的薄封装（多绑一个 `flush_i=0`，[spill_register.sv:32-46](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/spill_register.sv#L32-L46)）。后者内部用 **A、B 两个寄存器**实现一个"可反压、可冲刷"的深度为 2 的缓冲（[spill_register_flushable.sv:38-97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/spill_register_flushable.sv#L38-L97)）：

- **Bypass=1** 时直接旁路（`valid_o=valid_i`、`data_o=data_i`），不做切断。
- **Bypass=0** 时启用 A/B 双寄存器：
  - A 寄存器接收输入数据（`a_fill = valid_i && ready_o && !flush`）。
  - 当下游不就绪（`!ready_i`）时，A 的数据搬到 B（`b_fill`），腾出 A 继续接收上游——这就是"spill（溢出）"的含义：下游堵塞时把数据溢出到 B，避免卡住上游。
  - `ready_o = !a_full || !b_full`：只要还有一个寄存器空着就能收。
  - `valid_o = a_full || b_full`：只要有一个寄存器有数据就向下游有效。

`axi_cut`（[axi4_cut.sv:44-113](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_cut.sv#L44-L113)）对 aw/w/ar 三个"请求类"通道与 b/r 两个"响应类"通道各例化一个 `spill_register`，方向分别是从 slave 到 master（请求）或从 master 到 slave（响应）：

```verilog
spill_register #(.T(aw_chan_t)) i_req_aw( ... .valid_i(slv_req_i.aw_valid), .ready_o(slv_resp_o.aw_ready),
                                          .valid_o(mst_req_o.aw_valid), .ready_i(mst_resp_i.aw_ready) ... );
spill_register #(.T(b_chan_t))  i_req_b ( ... .valid_i(mst_resp_i.b_valid), .ready_o(mst_req_o.b_ready),
                                          .valid_o(slv_resp_o.b_valid), .ready_i(slv_req_i.b_ready) ... );
```

注意 `axi4_adapter_top` 例化 `axi_cut` 时 `Bypass=1'b0`（[第 364 行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L364)），即真正启用切断。

#### 4.4.3 源码精读

`spill_register_flushable` 的双寄存器数据通路（[spill_register_flushable.sv:43-96](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/spill_register_flushable.sv#L43-L96)）：

```verilog
// A 寄存器：收输入
assign a_fill  = valid_i && ready_o && (!flush_i);
assign a_drain = (a_full_q && !b_full_q) || flush_i;
// B 寄存器：下游不就绪时接收 A 的溢出
assign b_fill  = a_drain && (!ready_i) && (!flush_i);
assign b_drain = (b_full_q && ready_i) || flush_i;

assign ready_o = !a_full_q || !b_full_q;   // 还能收
assign valid_o = a_full_q | b_full_q;      // 还有货
assign data_o  = b_full_q ? b_data_q : a_data_q;  // 优先出 B
```

这段逻辑的精妙之处：`ready_o` 只依赖内部的 `a_full_q/b_full_q`，不依赖下游 `ready_i`；`valid_o` 也只依赖内部状态。于是输入侧的握手（`valid_i/ready_o`）与输出侧的握手（`valid_o/ready_i`）之间**没有任何组合路径相连**，彻底切断了时序路径。

`axi4_cut` 是它的批量应用：5 个通道 × 1 个 spill_register = 完整切断一条 AXI 接口。它的 `Bypass` 参数允许在不需要时整体旁路。

#### 4.4.4 代码实践

**实践目标**：理解 `spill_register` 的"反压不传播"特性，并在 `axi4_adapter_top` 中定位它的使用位置。

**操作步骤**：

1. 在 [axi4_adapter_top.sv:182-183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L182-L183) 找到三个关键信号：`adapter_axi_req_o`（适配器原始输出）、`cut_slv_req_i`（cut 的 slave 侧输入）、`cut_mst_req_o`（cut 的 master 侧输出，接到 `m_axi_*`）。
2. 确认 `axi4_adapter` 的输出经 `cut_slv_req_i`→`axi_cut`→`cut_mst_req_o`→`m_axi_*` 的流向（[axi4_adapter_top.sv:316-379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L316-L379)）。
3. 思考实验（示例代码，非项目原有）：假设外部 AXI 互联的 `m_axi_awready_i` 组合延迟很大，若没有 `axi_cut`，这条延迟会经 `cut_mst_resp_i.aw_ready` 直通回适配器状态机的 `aw_ready`，形成长路径。有了 `axi_cut`，这条路径被 spill_register 的一拍寄存器切断。
4. 在 [axi4_cut.sv:45-57](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_cut.sv#L45-L57) 确认 AW 通道的 spill_register 接法：`ready_i` 来自 `mst_resp_i.aw_ready`（外部），`valid_o` 去往 `mst_req_o.aw_valid`（外部），而 `ready_o` 回给 `slv_resp_o.aw_ready`（适配器侧）。

**需要观察的现象**：

- `axi_cut` 的 `Bypass` 是否为 0（启用切断）。
- 切断后，适配器侧的 `aw_ready`（`slv_resp_o.aw_ready`）是否不再组合依赖外部 `m_axi_awready_i`。

**预期结果**：能说清楚"外部互联的反压 `m_axi_*ready` 不再直接穿透到适配器状态机，而是被 spill_register 吸收"这一时序优化效果。

#### 4.4.5 小练习与答案

**练习 1**：`spill_register` 的 `ready_o = !a_full_q || !b_full_q`，意味着它的最大缓冲深度是多少？为什么这样设计？

> **答案**：最大深度为 2（A、B 两个寄存器）。设计成双寄存器是为了在下游短暂反压时仍能接纳上游一拍数据（"溢出"到 B），从而把上游与下游的握手解耦得更彻底——既切断组合路径，又尽量不损失吞吐（不像单寄存器那样一反压就立刻堵住上游）。

**练习 2**：如果把 `axi4_adapter_top` 中 `axi_cut` 的 `Bypass` 改为 `1'b1`，会发生什么？

> **答案**：`Bypass=1` 时 spill_register 直接旁路（`valid_o=valid_i`、`ready_o=ready_i`、`data_o=data_i`），五个通道全部直通，`axi_cut` 等效于一根导线。此时适配器与外部 AXI 互联之间的组合路径不再被切断，时序变差，但逻辑功能不变、延迟少一拍。

---

## 5. 综合实践

**任务**：以一次 L2 数据缺失的"出片"之旅为主线，把本讲四个模块串成一条完整链路，并对照 [u7-l2](u7-l2-l2cache-scheduler.md) 的 L2 内部机制画出端到端时序图。

要求完成以下工作：

1. **起点**：L2 Scheduler 因某 MSHR 缺失，在 `out_a` 通道发出一笔 GET 请求。在 [gpgpu_axi_top.sv:179-188](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L179-L188) 找到这笔请求被翻译成简易总线字段（`req_i=1`、`we_i=0`、`id_i=source`、`addr_i=address`）。
2. **翻译**：在 [axi4_adapter.sv:289-311](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter.sv#L289-L311) 跟踪读路径：`IDLE→ar_valid→WAIT_R_VALID→COMPLETE_READ`。说明何时拉 `m_axi_arvalid_o`、何时收 `m_axi_rvalid_i`。
3. **切断**：在 [axi4_adapter_top.sv:363-379](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/axi4_adapter/axi4_adapter_top.sv#L363-L379) 指出 `axi_cut` 在 AR/R 通道上各加了一拍寄存器。
4. **外部**：在 [test_gpu_axi_top.sv:199-242](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L199-L242) 确认 `m_axi_*` 接到 `axi_ram`，由 RAM 返回读数据与 `rid`。
5. **回程**：在 [gpgpu_axi_top.sv:152-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_axi_top.sv#L152-L195) 跟踪响应回送：`m_axi_rvalid_i`→`mem_rsp_opcode=1`、`mem_rsp_source=rid`→`top_out_d_valid/opcode/source/data`，最终回到 L2 的 D 通道。

**交付物**：一张从 `l2cache_out_a_valid` 到 `l2cache_out_d_valid` 的端到端时序草图，标注每一级的模块名、关键信号名与代码行号，并写明 source 字段如何往返。若能在仿真波形中验证则更佳；否则标注**待本地验证**。

## 6. 本讲小结

- `gpgpu_axi_top` 是把 `GPGPU_top` 包装成 AXI 产品的顶层壳：用 `axi4_adapter_top` 把 L2 的 TileLink `out_a/out_d` 翻译成对外 AXI4，用 `axi4lite_2_cta` 把主机 AXI4-Lite 翻译成 `host_req/host_rsp`。
- L2 的 TileLink 操作码经 `we_i = (opcode != 4)` 归并：`GET(4)` 为读→AXI AR/R，`PUTFULL/PUTPART/FLUSH` 为写→AXI AW/W/B；响应侧 R 通道还原为 D 通道 opcode=1（读数据），B 通道还原为 opcode=0（写应答）。
- TileLink 的 `source` 字段跨协议时被"借用"为 AXI 事务 ID（`id_i`），响应再凭 `rid/bid` 还原回 D 通道 `source`，维持回信地址的连续性；当前开源版本为单笔串行简化设计。
- `axi4_adapter` 的状态机把单笔 read/write 展开成 AXI 多通道握手序列；`gpgpu_axi_top` 配置为单 beat 单笔模式（`type_i=0`、`size_i=3`、`DATA_WIDTH=AXI_DATA_WIDTH=64`）。
- `axi4lite_2_cta` 用 18 个 32 位寄存器 `data_buf` 承载一次 workgroup 派发的全部参数，主机写 `reg[0]=1`（地址 0x00）触发 `host_req_valid`，完成后回读 `reg[17]`（地址 0x44）。
- `spill_register`（A/B 双寄存器）与 `axi_cut`（五通道各一个 spill_register）用于切断组合长路径、改善时序，靠"反压不组合穿透"实现。

## 7. 下一步学习建议

本讲讲完单元 7（片上互联与 L2）。至此，读者已具备从 L1 缺失→L2→互联→外部 AXI 的完整存储通路视角，以及主机驱动 CTA 派发的控制通路视角。建议：

1. **进入单元 8 工程实践**：先读 [u8-l1（仿真测试框架）](u8-l1-testbench-framework.md)，把本讲提到的 `test_gpu_axi_top`/`host_inter`/`axi_ram` 在仿真平台里的协作彻底吃透，并亲手跑通一个用例。
2. **回顾 L2 内部**：若对 L2 收到 `out_d` 响应后如何回填 MSHR、如何经 sourceD 回送 L1 仍有疑问，回头重读 [u7-l2](u7-l2-l2cache-scheduler.md)，把"片外取数→L2 回填→L1 命中"的全链路闭环。
3. **FPGA 与综合**：阅读 [u8-l3（FPGA 验证与综合）](u8-l3-fpga-and-synthesis.md)，了解本讲的 AXI 接口在 FPGA 验证时如何对接真实主机驱动（`naive_driver.c`），以及 `T28_MEM` 宏对 SRAM 例化的影响。
4. **指令集扩展**：若对数据通路本身更感兴趣，可跳到 [u8-l4（指令集扩展）](u8-l4-isa-extension.md)，从执行侧贯通"译码→采集→执行→写回"。
