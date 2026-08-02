# 仿真测试框架与 testbench

## 1. 本讲目标

本讲是单元 8（工程实践）的第一篇，带你走进 Ventus GPGPU 的**仿真验证层**。前面所有讲义都在读 RTL 本身（DUT，Design Under Test），本讲要回答的是：**在没有真实芯片、没有主机 CPU 的情况下，怎么证明这份 RTL 跑得对？**

读完本讲你应当能够：

- 说清 `test_gpu_axi_top` 这个仿真顶层例化了哪几个模块、它们如何连成一条「主机 → GPU → 外部存储」的闭环。
- 解释 `host_inter` 如何扮演「虚拟主机 CPU」：读 metadata、按 AXI4-Lite 协议把一个 workgroup 的派发参数写进 DUT、再轮询完成信号。
- 解释 `axi_ram` 作为外部 DRAM 模型如何响应 AXI4 读写，以及 kernel 代码与数据是**怎样、在哪里**被预先灌进去的。
- 看懂 `file_list.f` 与各用例自带的 `tc.v` 如何让「一套公共平台」服务「多个测试用例」。

本讲与 [u1-l4（仿真环境搭建与用例运行）](u1-l4-simulation-and-testcases.md) 是一对：u1-l4 讲「**怎么跑**」（Makefile、run.f、CASE 宏、看 PASSED/FAILED），本讲讲「**跑进去的到底是什么**」（testbench 内部如何驱动 DUT）。同时也承接 [u7-l4（AXI4 适配器与 host 接口）](u7-l4-axi-adapter-and-host.md)——那里讲的 `axi4lite_2_cta` 寄存器映射，在本讲会看到主机侧如何一笔笔写它。

> 说明：本讲以 `tc_gaussian`（高斯消元）用例为蓝本精读，所有行号均对照该目录下的 `tc.v`。其他用例（`tc_vecadd`、`tc_matadd` 等）的 testbench 结构完全一致，只是 `.metadata`/`.data` 内容与 `print_result` 的比对逻辑不同。

## 2. 前置知识

### 2.1 什么叫 testbench

RTL 描述的是「芯片内部该怎么工作」，但芯片自己不会动——它需要外部给时钟、给复位、给激励（输入数据/控制）、再观察输出。**testbench** 就是一段**只用于仿真、不会被综合成电路**的 Verilog 代码，负责扮演这些外部角色。它通常包含：

- 时钟/复位发生器（产生周期的方波、上电复位脉冲）；
- 激励发生器（模仿主机 CPU 往芯片写寄存器、写内存）；
- 响应模型（模仿外部 DRAM 应答读写）；
- 自检逻辑（把硬件输出和「黄金参考」对比，打印 PASSED/FAILED）。

### 2.2 AXI4 / AXI4-Lite 两套接口（承接 u7-l4）

Ventus 对外暴露两类接口，本讲会同时碰到：

- **AXI4-Lite（控制通路，从端 s_axilite）**：主机 CPU 用它配置寄存器、触发一个 workgroup 派发。5 个通道：AW（写地址）、W（写数据）、B（写响应）、AR（读地址）、R（读数据），每通道独立 `valid/ready` 握手。
- **AXI4（数据通路，主端 m_axi）**：GPU 用它读写外部 DRAM，搬运指令与数据。在 AXI4-Lite 基础上多了 burst（一次事务传多拍）、`wlast`（最后一拍）、`id`（事务标识）等。

### 2.3 `$readmemh` 与 `force/release`

两个仿真专用系统任务，是本讲预加载机制的核心：

- `$readmemh("文件名", 数组)`：把一个**十六进制文本文件**按行读进一个 reg 数组，每行一个字。`.metadata` / `.data` 文件就是这种格式。
- `force 信号 = 值; ... release 信号;`：在仿真里**强行改写**某条线网/寄存器的值，跨越模块层次，`release` 后恢复。testbench 用它从外部「假装自己是主机」去驱动 RAM 的从端口。

> 提示：这两个构造只在仿真器（VCS/Verdi）里有意义，综合工具会忽略或报错。这也是 testbench 文件与 RTL 分目录的原因。

## 3. 本讲源码地图

本讲全部位于 `testcase/test_gpgpu_axi_top/`，是「带 AXI 接口」的仿真平台（另一个不带 AXI 的 `test_gpgpu_top/` 思路类似）。

| 文件 | 作用 | 归属 |
|------|------|------|
| `common/test_gpu_axi_top.sv` | 仿真顶层，例化 DUT + 时钟复位 + 主机模型 + RAM 模型，声明所有 AXI 连线 | 公共 |
| `common/host_inter.sv` | 虚拟主机：解析 metadata、AXI4-Lite 写派发参数、轮询完成 | 公共 |
| `common/axi_ram.sv` | 外部 DRAM 模型（AXI4 从端 RAM），并提供内存窥探任务 | 公共 |
| `common/gen_clk.v` / `gen_rst.v` | 10ns 时钟、2 拍复位发生器 | 公共 |
| `common/file_list.f` | 列出 6 个 testbench 文件，其中 `./tc.v` 由各用例提供 | 公共 |
| `common/run.f` | VCS 编译配方，引用 `file_list.f` 与 RTL `model_list`（u1-l4 已讲） | 公共 |
| `tc_gaussian/tc.v` | 用例私有：编排整个仿真流程、预加载内存、比对结果、打印 PASSED/FAILED | 用例私有 |
| `tc_gaussian/Makefile` | `make run-vcs-*` 等目标，本质是带 `+define+CASE_*` 的 vcs 命令（u1-l4 已讲） | 用例私有 |

一句话区分：`common/` 是**平台**（换用例不变），`tc_gaussian/`（或 `tc_vecadd/` 等）是**内容**（换用例就换这里的 `tc.v` 与 `softdata/`）。

## 4. 核心概念与源码讲解

### 4.1 test_gpu_axi_top：仿真顶层与例化

#### 4.1.1 概念说明

`test_gpu_axi_top` 是整个仿真的**最外层模块**，它没有端口（testbench 顶层与外界唯一的交互是 `$display` 打印和写波形文件）。它的职责只有两件：

1. **声明**连接 DUT 两侧接口的所有 wire/reg；
2. **例化** 6 个模块，把线接好，构成闭环。

它本身不做任何运算——这与 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md) 讲的 `GPGPU_top`「只连线」的定位一致，是仿真版的「主板」。

#### 4.1.2 核心流程

仿真启动后的连接拓扑与数据流如下：

```text
                 ┌─────────────── test_gpu_axi_top (无端口顶层) ───────────────┐
                 │                                                              │
   gen_clk ──clk─┼─► gpgpu_axi_top (u_dut, DUT)                 gen_rst ──rst_n┘
                 │     │        │
   host_inter ◄──┼─────┘        └──► axi_ram (u_ram, 外部 DRAM)
   (AXI4-Lite)        (m_axi4)
    虚拟主机             数据通路
                 │
                 └─ tc (u_tc): 流程编排 / 预加载 / 比对 (在 tc.v)
```

- **控制通路**：`host_inter` 经 AXI4-Lite（`s_axilite_*`）连到 DUT 的主机从端口，写寄存器触发 CTA 派发（承接 u7-l4 的 `axi4lite_2_cta`）。
- **数据通路**：DUT 的 AXI4 主端口（`m_axi_*`）连到 `axi_ram`，GPU 通过它取指、读写数据（承接 u7-l4 的 `axi4_adapter`，即实例 `l2_2_mem`）。
- `tc` 模块（在 `tc.v`，见 4.4）通过**层次化引用**（如 `u_host_inter.drv_gpu(...)`）调用上面各模块的 task，编排「灌数据 → 派发 → 等完成 → 比对」全流程。

#### 4.1.3 源码精读

顶层声明了大量 AXI 信号，按「AXI4-Lite 从端（接 host_inter）」与「AXI4 主端（接 RAM）」两组组织。例如 AXI4-Lite 写地址通道：

[test_gpu_axi_top.sv:9-12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L9-L12) — 声明 `s_axilite_awready_o`（DUT 输出）、`s_axilite_awvalid_i`/`awaddr_i`/`awprot_i`（host_inter 驱动），位宽取自 `define.v` 的 `AXILITE_*_WIDTH` 宏。

DUT 例化是把所有 `s_axilite_*` 与 `m_axi_*` 信号一一接到 `gpgpu_axi_top u_dut` 的端口：

[test_gpu_axi_top.sv:83-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L83-L160) — 例化 DUT `gpgpu_axi_top`，即 [u7-l4](u7-l4-axi-adapter-and-host.md) 讲的「把 `GPGPU_top` 包成 AXI IP 的壳」。`s_axilite_*` 接它的主机从端，`m_axi_*` 接它的对外主端。

随后例化时钟、复位、主机、RAM 四个配角：

[test_gpu_axi_top.sv:162-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L162-L169) — `gen_clk` 产生 10ns 周期时钟、`gen_rst` 产生 2 拍复位。

[test_gpu_axi_top.sv:171-197](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L171-L197) — 例化 `host_inter`，把 AXI4-Lite 信号悉数对接（控制通路）。

[test_gpu_axi_top.sv:199-242](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L199-L242) — 例化 `axi_ram`，参数 `DATA_WIDTH=64/ADDR_WIDTH=32/ID_WIDTH=4`，把 DUT 的 `m_axi_*` 主端口连到 RAM 的从端口（数据通路）。注意 RAM 的 `rst` 接的是 `~rst_n`（高有效复位，与 DUT 的低有效相反）。

最后是 `tc` 例化、波形转储与结果打印任务：

[test_gpu_axi_top.sv:244](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L244) — 无端口例化 `tc u_tc();`，它完全靠层次化路径驱动其他实例。

[test_gpu_axi_top.sv:246-249](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L246-L249) — `$fsdbDumpfile("test.fsdb")` 指定波形文件，`$fsdbDumpvars(0, test_gpu_axi_top, "+mda","+all")` 转储全部层次（`+mda` 支持多维数组/内存阵列，对 `axi_ram` 的 `mem` 数组很关键）。这就是 u1-l4 提到的 `test.fsdb` 的来源。

[test_gpu_axi_top.sv:251-273](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L251-L273) — `PASSED` / `FAILED` 是两个 `task`，用一串 `$display` 打印 ASCII 艺术字。它们**本身不做判定**，只是「报幕员」；判定逻辑在 `tc.v` 的 `print_result`（见 4.4）。

#### 4.1.4 代码实践

**实践目标**：在不开仿真器的前提下，凭源码画出仿真顶层的「谁连谁」。

**操作步骤**：

1. 打开 `test_gpu_axi_top.sv`，把 6 个例化（`u_dut`、`u_gen_clk`、`u_gen_rst`、`u_host_inter`、`u_ram`、`u_tc`）各自列出来。
2. 对每个例化，记下它对接的是 DUT 的哪一组信号（`s_axilite_*` 还是 `m_axi_*`）。
3. 标注 `u_tc` 是「无端口模块」，它只靠层次化路径访问其他实例。

**需要观察的现象**：你会发现 `s_axilite_*` 这组 wire 同时出现在 `u_dut` 和 `u_host_inter` 两处，`m_axi_*` 同时出现在 `u_dut` 和 `u_ram` 两处——这就是「连线」的本质：同一根 wire 接两个端口。

**预期结果**：得到一张与本节「核心流程」框图一致的连接图。

> 待本地验证：若环境有 Verdi，可在波形里确认 `clk` 每 5ns 翻转一次、`rst_n` 在第 3 个上升沿才变 1。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `u_ram` 的复位端口接 `~rst_n`，而其他模块接 `rst_n`？

> **答案**：DUT 与 `host_inter` 用**低有效**复位（`rst_n`，0 表示复位）；而 `axi_ram` 这个 IP 的 `rst` 是**高有效**（1 表示复位，见 [axi_ram.sv:265](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L265) 的 `if (rst)`）。为了和同一个 `gen_rst` 产生的复位同步，顶层取反接入。

**练习 2**：`PASSED`/`FAILED` 这两个 task 定义在 `test_gpu_axi_top` 模块里，但 `tc.v`（在另一个模块 `tc` 里）能调用它们，这是怎么做到的？

> **答案**：Verilog 允许跨模块的**层次化任务调用**。`tc` 模块通过层次名引用到了 `test_gpu_axi_top` 实例下的 task——这是仿真专用的「跨层调用」，综合不支持，因此只能出现在 testbench。

---

### 4.2 host_inter：虚拟主机驱动 CTA 派发

#### 4.2.1 概念说明

`host_inter` 是「假装自己是主机 CPU」的激励发生器。真实系统里，主机 CPU 跑驱动程序、通过 MMIO 写 GPU 寄存器来下发任务；仿真里没有 CPU，于是 `host_inter` 用 Verilog 的 `task` + AXI4-Lite 握手时序来扮演这个角色。

它对外只暴露一组 AXI4-Lite **主端口**（输出 valid/addr/data，输入 ready/resp），连到 DUT 的从端口；内部维护一个 `metadata` 数组，承载从 `.metadata` 文件读来的「kernel 描述符」。

> ⚠️ 一个常见误解：**`host_inter` 并不把 kernel 代码或数据写进 DRAM**。它只读 `.metadata` 描述符、配置寄存器、启动派发。真正把 kernel/数据预加载进 `axi_ram` 的是 `tc.v` 的 `init_mem`（见 4.4）。两者都「读文件」，但读不同文件、写到不同地方：`host_inter` 读 `.metadata` 写「寄存器」；`init_mem` 读 `.data` 写「内存」。

#### 4.2.2 核心流程

`host_inter` 自己不主动跑主流程——它的 task 是被 `tc.v` 调用的。完整的一次 kernel 派发分两段：

```text
 tc.v:drv_gpu(meta,data)             tc.v:exe_finish(meta,data)
        │                                     │
        ▼                                     ▼
 ┌──────────────────┐                  ┌──────────────────┐
 │ $readmemh(meta)  │ 读描述符          │ 轮询 reg[17]      │
 │ → 解析 wf/wg 尺寸 │                  │ @0x44 直到≠0      │
 │ → axilite_write  │ 写 reg[1..15]    │ = host_rsp_valid  │
 │ → 写 reg[0]=1    │ 触发 host_req   │ → 统计 cycles     │
 └──────────────────┘                  └──────────────────┘
        │                                     │
        ▼                                     ▼
   等 cta2host_rcvd_ack_o            返回，tc 继续下一个 kernel
```

**寄存器映射**（与 [u7-l4](u7-l4-axi-adapter-and-host.md) 的 `axi4lite_2_cta` 的 18 个 `data_buf` 一一对应）：

| 地址 | reg[] | host_req 字段 | drv_gpu 写入的值 |
|------|-------|--------------|------------------|
| 0x00 | reg[0] | host_req_valid | 写 `1` 才触发派发（最后一笔） |
| 0x04 | reg[1] | wg_id | 0 |
| 0x08 | reg[2] | num_wf | wg_size |
| 0x0c | reg[3] | wf_size | wf_size |
| 0x10 | reg[4] | start_pc | `0x8000_0000`（硬编码） |
| 0x14 | reg[5] | vgpr_size_total | wg_size×vgprUsage |
| 0x18 | reg[6] | sgpr_size_total | wg_size×sgprUsage |
| 0x1c | reg[7] | lds_size_total | 128 |
| 0x20 | reg[8] | vgpr_size_per_wf | vgprUsage |
| 0x24 | reg[9] | sgpr_size_per_wf | sgprUsage |
| 0x28 | reg[10] | gds_baseaddr | 0 |
| 0x2c | reg[11] | pds_baseaddr | pdsBaseAddr+… |
| 0x30 | reg[12] | csr_knl | metaDataBaseAddr |
| 0x34/38/3c | reg[13~15] | kernel_size_3d | 0 |
| 0x44 | reg[17] | （读）完成状态 | exe_finish 轮询 |

> 关键点：**写 reg[0]=1 是「扣扳机」**——前面 reg[1..15] 只是装填参数，只有最后一笔写 0x00 才把 `host_req_valid` 拉起来，CTA 调度器（[u2-l1](u2-l1-cta-scheduler-and-resource-table.md)）才开始消费这个 workgroup。

#### 4.2.3 源码精读

模块端口与内部寄存器：

[host_inter.sv:4-30](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L4-L30) — AXI4-Lite 主端口声明。注意命名后缀 `_o` 表示主机侧驱动（ready 是输入、valid 是输出）。

[host_inter.sv:33-69](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L33-L69) — 用一组 `_r` 寄存器实现输出，`initial` 里把它们清零，并把 `bready`/`rready` 常置 1（主机端「永远准备好接收响应」，简化握手）。

加载与解析 metadata 的核心——`drv_gpu` task：

[host_inter.sv:104](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L104) — `$readmemh(fn_metadata, metadata)` 把 `.metadata` 十六进制文件读进数组，**这是 host_inter 加载程序描述符的入口**。

[host_inter.sv:113-127](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L113-L127) — 把 metadata 数组里**每两个 32 位字拼成 1 个 64 位字段**（小端：低字在前 `{metadata[2k+1], metadata[2k]}`），解析出 `kernel_id`、`kernal_size0/1/2`、`wf_size`、`wg_size`、`metaDataBaseAddr`、`ldsSize`、`pdsSize`、`sgprUsage`、`vgprUsage`、`pdsBaseAddr`、`num_buffer`。这些就是 [u2-l1](u2-l1-cta-scheduler-and-resource-table.md) 资源表判定 WG 能否派发所需的全部输入。

[host_inter.sv:129-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L129-L160) — 逐笔 `axilite_write(地址, 值)` 把上表 reg[1..15] 写进去，最后一笔 `axilite_write(32'h0000_0000, 32'd1)` 写 reg[0]=1 触发派发。这就是**「发起首个（及每个）workgroup」的代码**。

[host_inter.sv:160-161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L160-L161) — 写完 reg[0] 后 `@(negedge ...cta2host_rcvd_ack_o)`：等到 CTA 调度器**收到并应答**这个 host_req（ack 下沿），说明派发参数已被取走，主机可以记录「配置完成」时刻 `cycle_count[0]`。

AXI4-Lite 写握手实现 `axilite_write`：

[host_inter.sv:172-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L172-L195) — 用 `fork...join` **并行**驱动 AW（写地址）与 W（写数据）两个通道，各自 `wait(ready)` 后在时钟沿撤掉 valid，`join` 后隐式等 B 通道响应（因 `bready` 常 1）。这还原了 AXI4-Lite「AW/W 可同时、B 在后」的标准时序。

完成轮询 `exe_finish`：

[host_inter.sv:214-246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L214-L246) — 循环 `axilite_read(0x44, r_data)` 读 reg[17]，直到非零。非零意味着 DUT 已经把 `host_rsp_valid` 拉起——即 workgroup 全部 warp 完成、缓存冲刷完毕、L2 发出了 `finish_issue`（见 [u7-l2](u7-l2-l2cache-scheduler.md) 的 `finish_issue` 与 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md) 的 `host_rsp_valid_o`）。随后 [host_inter.sv:238](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L238) 用 `(cycle_count[1]-cycle_count[0])/10` 算出 kernel 耗时（除 10 是因为时钟周期 10ns，结果换算成「周期数」），即 u1-l4 提到的 `kernel_cycles`。

> 旁路 task `get_result_addr`（[host_inter.sv:248-262](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L248-L262)）解析出每个输出 buffer 的基地址 `parsed_base_r` 与大小 `parsed_size_r`，供后续 `print_result` 去 RAM 里取硬件结果用。

#### 4.2.4 代码实践

**实践目标**：定位「加载描述符」「触发派发」「等待完成」三段代码。

**操作步骤**：

1. 在 `host_inter.sv` 中找到 `$readmemh` 调用（第 104 行），确认它读的是 metadata 而非 data。
2. 找到写 `0x00=1` 的那一行（第 160 行），确认它是 `drv_gpu` 里**最后一笔**写操作（前面都是装填参数）。
3. 找到 `exe_finish` 里读 `0x44` 的循环（第 222-230 行），看清循环退出条件是 `r_data != 0`。
4. 对照本节寄存器映射表，把 `drv_gpu` 写的 16 笔地址逐一对应到 `host_req_*` 字段。

**需要观察的现象**：注意 `drv_gpu` 里 `metaDataBaseAddr`（CSR/knl 基址，样例 `0x9000_8000`）被写进 reg[12]，`start_pc` 固定写 `0x8000_0000`（reg[4]）——这两个地址后续就是 SM 去 `axi_ram` 取指、取 CSR 初始值的落点。

**预期结果**：能口述「主机写 16 个寄存器描述这次任务、最后一笔拉 valid、然后死等 0x44 变 1」。

> 待本地验证：跑 `make run-vcs-4w4t`，可在 `simv.log` 里依次找到 `Begin test`（[host_inter.sv:106](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L106)）、`Config finish!`（第 165 行）、`exe finish!`（第 234 行）三句打印，对照时间戳验证顺序。

#### 4.2.5 小练习与答案

**练习 1**：`drv_gpu` 为什么把 `wg_size` 同时写进 reg[2]（num_wf）和参与 reg[5]/reg[6]（vgpr/sgpr 总量）的计算？

> **答案**：`wg_size` 是这个 workgroup 包含的 **warp 数**（num_wf）。每个 warp 都要独立占用若干 VGPR/SGPR，所以寄存器堆**总需求** = `wg_size × per_wf 用量`（[host_inter.sv:138-140](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L138-L140)），CTA 调度器据此查资源表判断能否放得下（承接 [u2-l1](u2-l1-cta-scheduler-and-resource-table.md) 的 CAM 比较与 [u2-l2](u2-l2-cu-handler-and-inflight-wg.md) 的逐 warp 递增基址）。

**练习 2**：`exe_finish` 里为何要 `wait(!s_axilite_rvalid_o)` 再读？

> **答案**：避免在上一笔读响应还没结束时发起新的 AR。AXI4-Lite 同一时刻只允许一个未完成读事务，先等 R 通道空闲（`rvalid` 为 0）再拉 `arvalid`，保证握手干净。

---

### 4.3 axi_ram：外部存储模型与预加载

#### 4.3.1 概念说明

`axi_ram` 是一个**行为级 AXI4 从端 RAM**（源自开源 verilog-axi，C\*Core 改造），用来扮演 GPU 外部的 DRAM。它不是真的存储器 IP（不综合），而是一段用 `reg` 数组实现的「记忆体 + AXI 协议状态机」，仿真器里跑得很快。

它在仿真里身兼两职，这是理解它的关键：

- **运行期**：作为 DUT 的「远方存储」——DUT 取指、访存的 AXI4 读写都打到它这里，它按状态机正常响应。
- **预加载期**：它的从端口信号被 `tc.v` 用 `force` 直接驱动，**绕过状态机**把 `.data` 内容批量灌进 `mem`（见 4.4）。两者之所以能并存，是因为 `force` 只在仿真开始、DUT 还没发起访存时短暂使用，之后 `release` 即恢复正常。

它还提供两个 task 给结果比对用：`display_mem`（打印某地址内容）、`store_mem`（把一段地址的内容拷到 `mem_tmp_1/2` 数组供逐字比较）。

#### 4.3.2 核心流程

存储体与地址译码：

- 存储体 `mem`：`reg [DATA_WIDTH-1:0] mem [0 : 2**VALID_ADDR_WIDTH - 1]`。
- `VALID_ADDR_WIDTH = ADDR_WIDTH - $clog2(STRB_WIDTH) = 32 - $clog2(8) = 32 - 3 = 29`。
- 即 `mem` 有 \(2^{29}\) 个表项，每项 64 位（8 字节），按**字地址**（word-addressed）寻址。

容量与地址空间（默认参数）：

\[
\text{容量} = 2^{29} \times 8\,\text{字节} = 2^{29} \times 2^{3}\,\text{字节} = 2^{32}\,\text{字节} = 4\,\text{GB}
\]

读写各一套独立状态机（AXI4 的读、写通道本就相互独立）：

- **写状态机**：`IDLE → BURST → RESP`。IDLE 收 AW、锁存 id/addr/len/size/burst；BURST 逐拍收 W 并按 `wstrb` 写入 `mem`，`wlast` 时结束；RESP 发 B（写响应）。
- **读状态机**：`IDLE → BURST`。IDLE 收 AR、锁存；BURST 逐拍从 `mem` 读出、发 R，计到 len 发 `rlast`。

#### 4.3.3 源码精读

存储体与地址换算：

[axi_ram.sv:136](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L136) — `mem` 数组定义。注释掉的 `(* RAM_STYLE="BLOCK" *)` 提示在 FPGA 上可综合成块 RAM，但仿真里只是 reg 数组。

[axi_ram.sv:139-142](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L139-L142) — 把字节地址右移 `(ADDR_WIDTH-VALID_ADDR_WIDTH)=3` 位得字索引，等效于 `addr >> 3`（每项 8 字节）。

写状态机（IDLE/BURST/RESP 的组合部分）：

[axi_ram.sv:188-242](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L188-L242) — 算 `write_state_next`。第 211-214 行在收到 W 拍时按 burst 递增地址（仅 INCR，`awburst==2'b01`；FIXED `2'b00` 不递增）；第 219-221 行计满后发 B 响应。

按字节使能写入：

[axi_ram.sv:259-263](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L259-L263) — 对 `STRB_WIDTH=8` 个字节位逐位判断，`wstrb[i]` 为 1 才把 `wdata` 对应字节写进 `mem`。这正是 [u7-l1](u7-l1-tilelink-protocol.md) 讲的 TileLink `mask`（字节使能）一路映射到 AXI `wstrb` 后的落点。

读状态机：

[axi_ram.sv:291-328](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L291-L328) — `IDLE → BURST`。第 309-317 行在能发 R 时从 `mem[read_addr_valid]` 读出，设 `rlast = (count==0)`，递增地址、递减计数；读出数据锁进 `rdata` 在 [axi_ram.sv:345-347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L345-L347)。

窥探 task：

[axi_ram.sv:365-412](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L365-L412) — `display_mem(addr)` 打印某地址的高低 32 位；`store_mem(base1, base2, size1, size2, en1, en2)` 把两段结果区拷进 `mem_tmp_1`/`mem_tmp_2`（每段按 32 位拆开），供 `tc.v` 比对。

#### 4.3.4 代码实践（核心：理解 kernel 如何被预加载）

**实践目标**：搞清「kernel 代码与输入数据是怎么进到 RAM 里的」。

**关键认知**：RAM 的 `mem` 数组**没有**用 `$readmemh` 直接初始化。预加载是 `tc.v` 的 `init_mem` task 用 `force` **驱动 RAM 自己的 AXI4 从端口**完成的——相当于「testbench 假装自己是 DUT，按 AXI4 写时序往 RAM 里写」。

**操作步骤**（读 `tc.v` 的 `init_mem`）：

1. 看 [tc.v:173-174](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L173-L174)：`$readmemh(fn_data, data)` 把 `.data`（kernel 二进制 + 输入数据）读进 `data[]`，`$readmemh(fn_metadata, metadata)` 读描述符。
2. 看 [tc.v:175-185](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L175-L185)：从 metadata 解析每个 buffer 的基地址 `buf_ba_w[]` 与大小 `buf_size[]`（样例基地址含 `0x8000_0000` 指令区与若干 `0x9000_xxxx` 数据区）。
3. 看 [tc.v:203-257](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L203-L257)：对每个 buffer，用 `force u_ram.s_axi_aw*` / `s_axi_w*` 按 INCR burst（`awlen=0xf` 即 16 拍、`awsize=2` 即 4 字节）把 `data[]` 逐段写进 RAM 的对应地址。[tc.v:237-238](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L237-L238) 把每个 32 位数据拼到 64 位 `wdata` 的高/低半，配 `wstrb=0xf`/`0xf0` 写对应半字。

**需要观察的现象**：`init_mem` 写完才 `release`，紧接着 `tc.v` 才调 `drv_gpu` 派发。也就是说**先有内存里的指令/数据，再启动核**——和真实「加载可执行文件到内存再跳转执行」一致。`start_pc=0x8000_0000` 正是 `init_mem` 写进去的指令区首地址。

**预期结果**：能解释「`.data` 文件经 `$readmemh`→`data[]`→`force` 走 AXI4 写通路→落到 `axi_ram.mem`」这条预加载链。

> 待本地验证：若你有 VCS，可在 `init_mem` 末尾插一句 `u_ram.display_mem(32'h8000_0000);`，应能看到 kernel 首条指令的编码（与 `.data` 文件对应位置一致）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `init_mem` 用 `force` 驱动 RAM 端口，而不是直接 `$readmemh` 到 `u_ram.mem`？

> **答案**：用 `force` 走 AXI4 写通路，能**同时验证 RAM 的写状态机本身是对的**（AW/W/B 握手、wstrb、burst 递增都参与了）；直接写 `mem` 数组则绕过了这些逻辑。此外 buffer 基地址由 metadata 决定、按段分布，逐段 AXI 写更贴近真实「DMA 搬运」。代价是 `init_mem` 代码较长。

**练习 2**：默认配置下 `axi_ram` 能寻址 4GB，但仿真真会分配 4GB 内存吗？

> **答案**：仿真器按需分配 `reg` 数组，`mem` 在被写入前不占实内存；且 `init_mem` 只写了少量 buffer 段。但 `$fsdbDumpvars` 用了 `+mda`（[test_gpu_axi_top.sv:248](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L248)）会尝试转储多维数组，所以波形里 `mem` 全量转储可能很大——这也是 `test.fsdb` 体积可观的原因之一。

---

### 4.4 file_list.f 与 tc.v：用例组织与流程编排

#### 4.4.1 概念说明

仿真平台分成「公共平台」与「用例内容」两层，靠两个文件粘合：

- `common/file_list.f` 列出 6 个 testbench 文件，其中 **`./tc.v` 是相对路径**，指向**当前用例目录**下的 `tc.v`。
- 各用例目录（`tc_gaussian/`、`tc_vecadd/`…）提供自己的 `tc.v` + `Makefile` + `softdata/`，复用同一套 `common/`。

`tc.v` 是真正的「总指挥」：本身无端口（`module tc;`），完全靠**层次化引用**调用 `host_inter` 与 `axi_ram` 的 task。它开头先用一堆 `` `define `` 给长路径起别名，例如 `` `define drv_gpu u_host_inter.drv_gpu ``，之后写 `` `drv_gpu(...) `` 即等价于层次化调用。

`tc.v` 定义了四个关键 task，对应实践任务要找的几个点：

- `init_test_file`：按 `CASE_xWyT` 宏选 `.metadata`/`.data` 文件名。
- `init_mem`：**预加载内存**（实践任务的「AXI RAM 如何被预加载」）。
- `test_main`：串流程，其中调用 `drv_gpu`（**发起首个 workgroup**）。
- `print_result`：读回结果与 golden 比较，调 `PASSED`/`FAILED`（**检测退出条件**）。

#### 4.4.2 核心流程

`tc` 模块的主循环（`test_main`）对每个 kernel 文件执行：

```text
for i in 0..FILE_NUM-1:
    force u_dut.l2_2_mem.m_axi_bvalid_i = 0   # 暂时压住 DUT 的写响应，别干扰预加载
    init_mem(meta[i], data[i])                 # 把 data 灌进 axi_ram
    release u_dut.l2_2_mem.m_axi_bvalid_i
    drv_gpu(meta[i], data[i])                  # host_inter 写寄存器、触发派发
    if i==0: get_result_addr(...)              # 解析输出 buffer 地址（仅首份）
    exe_finish(meta[i], data[i])               # 等 host_rsp_valid，统计 cycles
    if 末份: print_result()                    # 取硬件结果、比对、PASSED/FAILED
```

判定逻辑（`print_result`）：

1. 用 `store_mem` 把硬件写回的结果区拷到 `mem_tmp_1`/`mem_tmp_2`；
2. 与代码里**硬编码的黄金参考**（`matrix_a_*_soft`/`array_b_*_soft`）逐字比较；
3. 全部相等（`&pass` 归约）才调 `PASSED`，否则 `FAILED`。

> 注意：`PASSED`/`FAILED` 这两个 task 定义在 `test_gpu_axi_top` 模块里（4.1.3），`tc.v` 通过层次名调用——所以「报幕」在顶层、「判定」在 `tc.v`。

#### 4.4.3 源码精读

[file_list.f:1-6](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/file_list.f#L1-L6) — 6 行，前 5 个是 `../common/` 下公共文件，第 6 行 `./tc.v` 是用例私有。这是「一平台多用例」的关键：换用例只换目录，`run.f`/`file_list.f` 都不用动（u1-l4 讲过的 `-f ../common/file_list.f` 把它拉进来）。

`tc.v` 顶部的层次化别名（让代码可读）：

[tc.v:1-13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L1-L13) — 用 `` `define `` 把 `drv_gpu`、`exe_finish`、`display_mem`、`store_mem`、`kernel_cycles` 等定义成 `u_host_inter.xxx`/`u_ram.xxx` 的层次路径别名，后续就能像本地 task 一样调用。

CASE 宏决定文件数：

[tc.v:31-35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L31-L35) — `CASE_4W8T` 时 `FILE_NUM=8`，否则 6。这与 u1-l4 讲的 `+define+CASE_xWyT` 一一对应——`init_test_file`（[tc.v:60-131](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L60-L131)）据此选 `softdata/` 下不同子目录的 `.metadata`/`.data`。

主流程：

[tc.v:50-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L50-L58) — `initial`：等 100 拍（让复位/时钟稳定）→ `init_test_file` → `test_main` → 再 100 拍 → `$finish`。这是整个仿真唯一的顶层 `initial` 入口。

[tc.v:133-153](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L133-L153) — `test_main` 主循环。[tc.v:137-139](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L137-L139) 的 `force u_dut.l2_2_mem.m_axi_bvalid_i = 0` 很巧妙：`l2_2_mem` 是 DUT 内的 AXI4 适配器（[u7-l4](u7-l4-axi-adapter-and-host.md)），预加载期间压住它的写响应输入，避免 DUT 在主机还在灌数据时抢先发事务干扰 RAM。[tc.v:138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L138) 调 `init_mem` 预加载、[tc.v:140](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L140) 调 `drv_gpu` 派发、[tc.v:144](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L144) 调 `exe_finish` 等完成、[tc.v:147](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L147) 在末份调 `print_result`。

判定与退出：

[tc.v:262-393](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L262-L393) — `print_result`：[tc.v:277-280](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L277-L280) 硬编码黄金参考（高斯消元的浮点结果矩阵/数组）；[tc.v:314](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L314) 调 `store_mem` 取硬件结果；[tc.v:316-346](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L316-L346) 按 `CASE` 选 4×4 或 5×5 参考逐字比对（注意 `mem_tmp_1[j]==matrix_a_*_soft[N-1-j]` 的反向索引，源自 `store_mem` 高低字拆包顺序）；[tc.v:348-383](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L348-L383) 全相等（`(&matrix_*_pass) && (&array_*_pass)`）才 `PASSED`，否则 `FAILED`。这就是 u1-l4 所说 `simv.log` 里 `PASSED`/`FAILED` 的**真正来源**。

> 说明：本用例（高斯消元）的黄金参考是**直接硬编码**在 `tc.v` 里的浮点十六进制常量。其他用例（如 vecadd）可能改用 `.data` 里的参考段比对，但「取硬件结果→比→报 PASSED/FAILED」的模式一致。

#### 4.4.4 代码实践（本讲主实践）

> 实践任务原文：在 testbench 中找到 host_inter 加载程序、发起首个 workgroup、以及检测 PASSED/FAILED 退出条件的代码，说明 AXI RAM 如何被预加载 kernel 与数据。

**实践目标**：把「加载 → 启动 → 等完 → 判定」四段代码在源码里逐一指认出来，并纠正「host_inter 加载程序」的措辞。

**操作步骤**：

1. **加载程序/数据（预加载）**：打开 [tc.v:155-259](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L155-L259)，确认它是 `init_mem`、用 `$readmemh` + `force` 把 `.data` 写进 `u_ram`。这才是真正的「加载」，**它在 `tc.v` 而非 `host_inter`**。`host_inter` 加载的是 `.metadata` 描述符（写进寄存器），不是程序本体。
2. **发起首个 workgroup**：`init_mem` 之后，`test_main` 调用 `` `drv_gpu ``（[tc.v:140](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L140)），进入 [host_inter.sv:84-170](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L84-L170)，确认 [host_inter.sv:160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L160) 的 `axilite_write(0x00,1)` 是「扣扳机」。
3. **检测完成与 PASSED/FAILED**：等完成在 [host_inter.sv:214-246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L214-L246)（`exe_finish` 轮询 `0x44`）；退出/判定在 [tc.v:50-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L50-L58) 的 `$finish` 与 [tc.v:262-393](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L262-L393) 的 `print_result`（比较 + 调 `PASSED`/`FAILED`）。
4. **AXI RAM 预加载原理**：沿 `.data` → `$readmemh`（[tc.v:173](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L173)）→ `data[]` → `force u_ram.s_axi_aw*/s_axi_w*`（[tc.v:203-257](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L203-L257)）→ `axi_ram.mem` 这条链走一遍。

**需要观察的现象**：预加载用 `force`/`release`（绕过协议直接捅 `axi_ram` 从端口），而正常派发用 `axilite_write`（严格走 AXI4-Lite 握手）——两种完全不同的「写」机制。`init_mem` 写完 `release` 后，DUT 才开始正常读写 RAM。

**预期结果**：能画出 `init_mem($readmemh→force) → drv_gpu(配寄存器→reg[0]=1) → DUT 运行(读写 axi_ram) → exe_finish(轮询0x44) → print_result(store_mem→比较→PASSED/FAILED) → $finish` 的完整时序图，并指出「加载」与「启动」是两个不同模块干的。

> 待本地验证：把 `print_result` 里某个黄金参考常量（如 [tc.v:277](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L277) 的某个浮点值）故意改错一位，重跑 `make run-vcs-4w4t`，应看到 `simv.log` 从 PASSED 变 FAILED——以此验证判定通路。

#### 4.4.5 小练习与答案

**练习 1**：`file_list.f` 第 6 行写 `./tc.v` 而不是 `../common/tc.v`，这个相对路径是相对于谁？

> **答案**：相对于 **VCS 的工作目录**（即执行 `make` 时所在的用例目录，如 `tc_gaussian/`）。所以每个用例目录放自己的 `tc.v`，`common/` 不含 `tc.v`。这也是 `Makefile` 里 `-f ../common/run.f`、`run.f` 内 `-f ../common/file_list.f`、而 `file_list.f` 里 `./tc.v` 落到用例目录的原因——三层相对路径各指各的基准。

**练习 2**：为什么要 `force u_dut.l2_2_mem.m_axi_bvalid_i = 0` 再 `init_mem`？

> **答案**：`init_mem` 用 `force` 直接驱动 `axi_ram` 的从端口写数据，此时 DUT 的 AXI 适配器 `l2_2_mem` 不应同时往 RAM 发写响应/事务，否则两边争抢同一组 `s_axi_*` 信号会产生 X 或冲突。压住 `m_axi_bvalid_i` 让适配器在这段时间「沉默」，预加载完 `release` 再恢复正常数据通路。

**练习 3**：换一个用例（如 `tc_vecadd`）时，平台文件（`common/` 下的 5 个）需要改吗？

> **答案**：不需要。`file_list.f` 里前 5 个文件路径写死、共享；只有 `./tc.v` 随用例目录变化。换用例只需进入对应目录、改 `define.v` 的 `NUM_THREAD`（使 kernel 的 VLEN 匹配，详见 u1-l4）、跑对应 `make` 目标即可。

---

## 5. 综合实践

**任务**：以 `tc_gaussian` 的 `4w4t` 配置为例，写一份「仿真生命周期报告」，覆盖以下 7 个时刻，每个时刻给出**对应的源码位置（带行号的永久链接）**和**一句话说明**：

1. 上电复位释放（`gen_rst`，[gen_rst.v:10-15](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/gen_rst.v#L10-L15)）。
2. 内存预加载完成（`init_mem` 末尾的 `release`，[tc.v:255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L255)）。
3. workgroup 参数配置完毕（`drv_gpu` 写完 reg[15]，[host_inter.sv:158](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L158)）。
4. 首个 workgroup 被触发（`drv_gpu` 写 reg[0]=1，[host_inter.sv:160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L160)）。
5. DUT 确认收到派发（`cta2host_rcvd_ack_o` 下降沿，[host_inter.sv:161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L161)）。
6. 执行完成被检测到（`exe_finish` 读到 `0x44 != 0`，[host_inter.sv:226-227](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L226-L227)）。
7. 结果判定（`print_result` 调 `PASSED` 或 `FAILED`，`CASE_4W4T` 分支 [tc.v:369](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L369) / [tc.v:372](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L372)）。

**进阶**：在报告里画一张时序轴（ns 为单位），标出 `cycle_count[0]`（时刻 4/5 之间）和 `cycle_count[1]`（时刻 6）的位置，并用 `kernel_cycles = (cycle_count[1]-cycle_count[0])/10` 解释这个除以 10 的来源（提示：`gen_clk` 的 `PERIOD=10.0`，见 [gen_clk.v:8](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/gen_clk.v#L8)）。

**验证方式**：若本地有 VCS，跑 `make run-vcs-4w4t`，把 `simv.log` 里 `$display` 打印的时间戳填进你的时序轴，核对你的源码定位是否正确。无法运行则标注「待本地验证」。

## 6. 本讲小结

- 仿真顶层 `test_gpu_axi_top` 只做例化与连线：DUT `gpgpu_axi_top`、时钟/复位、虚拟主机 `host_inter`、外部 RAM `axi_ram`、总指挥 `tc`，构成「主机→GPU→DRAM」闭环；自身额外提供波形 dump 与 `PASSED`/`FAILED` 打印 task（不做判定）。
- `host_inter` 是虚拟主机 CPU，靠 `task` + AXI4-Lite 握手工作：`drv_gpu` 用 `$readmemh` 读 `.metadata`、写 16 个寄存器、最后写 reg[0]=1 触发派发；`exe_finish` 轮询 reg[17]@0x44 等完成并统计 `kernel_cycles`。它**不负责**加载 kernel/数据。
- `axi_ram` 是行为级 AXI4 从端 RAM（4GB 字地址空间、独立的读/写状态机、支持 `wstrb` 字节使能），并提供 `display_mem`/`store_mem` 供 testbench 窥探内存。
- **kernel 代码与数据不是 `$readmemh` 直接到 `mem` 的**，而是 `tc.v::init_mem` 用 `force` 驱动 RAM 的 AXI4 从端口、按 buffer 基地址逐段 INCR burst 写进去的——这同时验证了 RAM 的写通路。
- `file_list.f` 第 6 行的 `./tc.v` 让「公共平台」服务「多用例」：换用例只换用例目录的 `tc.v`+`softdata/`，`common/` 不动。
- `PASSED`/`FAILED` 的真正判定在 `tc.v::print_result`：取硬件写回 `axi_ram` 的结果，与硬编码黄金参考逐字比对，全等才 PASSED。

## 7. 下一步学习建议

- **横向对比其他用例**：去看 `tc_vecadd/`、`tc_matadd/`、`tc_nn/`、`tc_bfs/` 各自的 `tc.v`，对比它们 `print_result` 的比对逻辑（哪些硬编码 golden、哪些读 `.data` 参考段），这是写自定义测试用例的模板。
- **下一个公共库**：本讲多次用到 `force/release`、`$readmemh`、`fork/join` 等**仿真专用**手法，下一讲 [u8-l2 公共单元库 common_cell](u8-l2-common-cell-library.md) 转向**可综合**的复用单元（FIFO、仲裁器、popcount 等），注意区分「仿真专用」与「可综合复用」。
- **FPGA 实践衔接**：本讲的 `axi_ram` 只是仿真模型，真实 FPGA/流片用 SRAM IP 替换——这正是 [u8-l3 FPGA 验证与综合流程](u8-l3-fpga-and-synthesis.md) 要讲的 `T28_MEM` 宏与 DC 综合，那里还会看到 `FPGA_test/driver/naive_driver.c` 用 **C 代码**（而非 Verilog task）扮演主机，可对照本讲的 `host_inter`。
- **指令扩展验证**：若要做 [u8-l4 指令集扩展](u8-l4-isa-extension.md) 后的验证，本讲的 `init_mem`/`drv_gpu`/`exe_finish`/`print_result` 四件套就是模板——照着复制一份 `tc.v`，改 `.metadata`/`.data` 与黄金参考即可。
