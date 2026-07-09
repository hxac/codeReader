# u7-l1 trace-player 测试平台架构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `verif/synth_tb/tb_top.v` 里那个名为 `top` 的模块**例化了哪些东西**、各自扮演什么角色。
- 画出测试平台（testbench，下称 TB）的**整体框图**：激励源 → CSB 口 → DUT → AXI 存储口 → 行为存储模型。
- 指出 TB **自己产生**了哪几路时钟、复位是怎么来的、分别喂给了谁。
- 解释 `syn_axi_slave` 如何把 NVDLA 的 AXI 五通道事务**拆解**成一组内部 `saxi2mem_*` 命令，并靠一堆 FIFO 维持事务顺序。
- 理解 `slave_mem` 这块行为级存储**为什么能“可回压”**（throttle）。
- 认识 `zemi3_tb.sv` 这个 SystemVerilog 监控/检查器在仿真后端（ZeBu/Cadence）流程里的角色。

本讲是单元 7（验证与参考模型）的第一篇，承接 u1-l4（你已经会用 `make run TESTDIR=...` 跑一个 sanity trace）。本讲**不再讲怎么敲命令**，而是拆开 `simv` 跑起来时**围绕 DUT 搭起来的那一圈外围 RTL**。

## 2. 前置知识

- **DUT（Design Under Test）**：被验证的设计，本仓库里就是顶层 [`NV_nvdla`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1)。
- **trace-player**：u1-l4 提过，TB 把一个文本激励文件（`input.txn`）转成定宽十六进制 `input.txn.raw`，再由一个状态机一条条“回放”成 CSB 寄存器读写。本讲会看到这个回放器长什么样。
- **CSB（配置空间总线）**：u2-l1 详述。CPU 编程 NVDLA 各引擎寄存器的唯一入口，请求组 `csb2nvdla_*`（valid/ready/addr/wdat/write/nposted），响应组 `nvdla2csb_*`（valid/data/wr_complete）。本讲的 `syn_csb_master` 就是 TB 侧模拟的“CPU”。
- **AXI memif**：u4-l1/u4-l2 详述。NVDLA 对外两组 AXI 接口——`core2dbb`（接片外主存）与 `core2cvsram`（接片上 CVSRAM），每组分 AR/R（读）、AW/W/B（写）五个通道。本讲的 `syn_axi_slave` 是 TB 侧模拟的“AXI 从端存储器”。
- **行为模型 vs 综合模型**：u6-l3 讲过 RAM 的双面性。本讲的 `slave_mem` 是**纯行为级**存储（`reg ... memory[]`），只为仿真用，不综合。

如果你对上面任何一项完全陌生，建议先回看对应讲义。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 [`verif/synth_tb/`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/)：

| 文件 | 模块名 | 作用 |
|------|--------|------|
| [tb_top.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v) | `top`（及若干小模块） | TB 顶层：产生时钟/复位、例化 DUT、CSB master、存储 wrapper |
| [syn_tb_defines.vh](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh) | —（宏定义） | AXI 宽度、地址映射、激励命令格式等全局宏 |
| [csb_master_seq.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v) | `csb_master_seq` | 读 `input.txn.raw`、按命令回放 CSB 读写/等待/结束 |
| [csb_master.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v) | `syn_csb_master` | 把 sequencer 的命令转成真正符合 CSB 握手的请求 |
| [slave_mem_wrap.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/slave_mem_wrap.v) | `syn_slave_mem_wrap` | 把 2 个 AXI slave 和 2 块存储包在一起的 wrapper |
| [axi_slave.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v) | `syn_axi_slave` | AXI 从端协议机，把 AXI 事务拆成内部命令 |
| [memory.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/memory.v) | `slave_mem` | 行为级存储数组 + 带宽节流逻辑 |
| [zemi3_tb.sv](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/zemi3_tb.sv) | `zemi3_tb` | 仿真/仿真加速后端用的 SV/DPI 监控检查器 |

另外还有一组配套 FIFO（`id_fifo.v` / `raddr_fifo.v` / `waddr_fifo.v` / `wdata_fifo.v` / `wstrb_fifo.v` / `memresp_fifo.v`）和一个 `clk_divider.v`，本讲会点到为止，细节留作练习。

## 4. 核心概念与源码讲解

### 4.1 tb_top 顶层：DUT 例化、时钟与复位

#### 4.1.1 概念说明

一个 RTL 仿真 TB 的顶层模块通常负责三件事：**造时钟、发复位、把 DUT 和外围模型连起来**。NVDLA 的这个 TB 顶层就在 `tb_top.v` 里，模块名直接叫 `top`——这也是 VCS 编译时的顶层（`-top top`，见 [`verif/sim/Makefile:603`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L603)）。它例化了四类东西：

1. **DUT**：`NV_nvdla nvdla_top`。
2. **激励通路**：`csb_master_seq`（回放器）+ `syn_csb_master`（CSB 协议机）。
3. **存储模型**：`syn_slave_mem_wrap`（内含 2 个 AXI slave + 2 块存储）。
4. **辅助电路**：`soc2nvdla_time_gen`（给 DUT 喂时间戳）、`clk_divider`、`bandwidth_mon` / `bandwidth_throttle`（带宽/未完成事务限流）、`assert_module`。

#### 4.1.2 核心流程

时钟与复位的产生逻辑（非 `EMU_TB`/`ZEBU` 的默认 VCS 流程）：

```text
msc_clk_ip  ──(每 simulation_cycle/2=10 翻转)──▶  clk (= msc_clk_ip, 周期 20)
clk ──(每个 posedge 翻转)──▶ half_speed_clk (周期 40, 喂 DUT/CSB master)
mem_clk_fast ──(每 10 翻转)──▶  ──clk_divider(/2)──▶ mem_clk (喂 AXI slave/存储慢口)

reset: 上电 = 0（复位有效），1000 个 clk 边沿后置 1（释放复位）
```

要点：

- `reset` 是一个 **active-low 风格**的复位（0=复位中，1=释放）。它被**原样**接到 DUT 的 `dla_reset_rstn` 和 `direct_reset_`，也接给所有 TB 模型。
- DUT 的 `dla_core_clk` 与 `dla_csb_clk` **都被接到了 `half_speed_clk`**——也就是说仿真里 core 域和 csb 域跑同一个时钟，这是 TB 的简化（真实 SoC 里两者不同频，需要 u6-l1 讲的跨域同步器，但仿真里没必要）。
- `half_speed_clk` 由 `always @(posedge clk) half_speed_clk <= ~half_speed_clk;` 产生，所以它是 `clk` 的二分频。

#### 4.1.3 源码精读

顶层模块声明与时钟周期参数：

[verif/synth_tb/tb_top.v:34-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L34-L47) —— 模块 `top`，定义 `simulation_cycle=20`、`simulation_cycle_mem=20` 两个周期参数。

复位与主时钟产生：

[verif/synth_tb/tb_top.v:318-337](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L318-L337) —— `reset` 上电为 0、`repeat(1000) @(clk)` 后置 1；`msc_clk_ip`/`mem_clk_fast` 用 `always #(...) x = ~x` 翻转；`clk = msc_clk_ip`；`half_speed_clk` 在 `posedge clk` 翻转。

DUT 例化（注意时钟、复位、CSB、两组 AXI、中断的连接）：

[verif/synth_tb/tb_top.v:359-431](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L359-L431) —— `NV_nvdla nvdla_top`，其中 `.dla_core_clk(half_speed_clk)`、`.dla_csb_clk(half_speed_clk)`、`.dla_reset_rstn(reset)`；CSB 请求来自 `mcsb2scsb_*`；`nvdla_core2dbb_*` 接 `axi_slave0`、`nvdla_core2cvsram_*` 接 `axi_slave1`；`.dla_intr(dla_intr)` 把中断引回。

CSB 口字段切片（这正好印证 u2-l1 的 CSB 请求包定义）：

[verif/synth_tb/tb_top.v:368-376](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L368-L376) —— 从 63 位 `mcsb2scsb_pd` 里切出 `addr[15:0]`、`wdat[53:22]`、`write[54]`、`nposted[55]`、响应 `nvdla2csb_data`、`wr_complete`。

#### 4.1.4 代码实践

**实践目标**：确认 DUT 的两组 AXI 存储口分别落到了哪个 AXI slave 实例上，并搞清时钟来源。

**操作步骤**：

1. 打开 `verif/synth_tb/tb_top.v`，定位到 `NV_nvdla nvdla_top`（约 359 行）。
2. 找到 `.nvdla_core2dbb_aw_awvalid (...)` 这一组端口，记录括号里连接的 wire 名（应是 `axi_slave0_*`）。
3. 同样找到 `.nvdla_core2cvsram_aw_awvalid (...)`，确认连到 `axi_slave1_*`。
4. 往上翻到 `syn_slave_mem_wrap slave_mem_wrap`（约 218 行），看它的 `axi_slave0`/`axi_slave1` 对应 DBB 还是 CVSRAM。

**需要观察的现象**：`core2dbb` 的全部五通道（AR/R/AW/W/B）都连到 `axi_slave0_*`，`core2cvsram` 连到 `axi_slave1_*`，二者一一对应、无交叉。

**预期结果**：你能写出一张“DUT 端口 → wire → wrapper 端口 → 实例”的四列对应表。

**运行结果**：待本地验证（本实践是纯源码阅读，不需要跑仿真）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DUT 的 `dla_core_clk` 和 `dla_csb_clk` 都接 `half_speed_clk`，而不是各自接独立时钟？
**答**：仿真 TB 为了简化，让 core 域与 csb 域同步，避免引入跨时钟域同步器带来的复杂度与仿真时间；真实 SoC 里两者不同频，由 DUT 内部的 `sync3d`/异步 FIFO 处理（见 u6-l1）。

**练习 2**：`reset` 信号是 active-high 还是 active-low？为什么 DUT 能正常工作？
**答**：在 TB 内 `reset=0` 表示“复位中”、`reset=1` 表示“释放”，对 DUT 而言 `dla_reset_rstn(reset)` 是标准的 active-low 复位（rstn=0 复位、rstn=1 解除），所以上电后先复位 1000 拍再释放，符合 DUT 预期。

---

### 4.2 CSB 激励通路：sequencer 与 syn_csb_master

#### 4.2.1 概念说明

DUT 要工作，必须有人通过 CSB 口给它写寄存器。TB 用两级串联实现这个“假 CPU”：

- **`csb_master_seq`（回放器/sequencer）**：把 `input.txn.raw` 读进一个命令数组 `cmd_memory`，用一个状态机逐条解释命令（写寄存器、读寄存器、轮询、等待中断、加载/转储存储、结束）。它输出的是一个**“逻辑请求”**（`mseq2mcsb_pd` + `mseq_pending_req`），还不是真正符合 CSB 握手的信号。
- **`syn_csb_master`（CSB 协议机）**：把上面的逻辑请求翻译成真正符合 valid/ready 握手的 CSB 请求包 `mcsb2scsb_*`，并回收 DUT 的响应。

为什么要拆两层？因为 sequencer 只关心“命令语义”（我要写哪个寄存器、写什么值），而 CSB 握手有时序细节（posted/非 posted 写、读要等 valid），把这些协议细节单独放进 `syn_csb_master` 的 FSM 里，能让 sequencer 代码保持线性、易读。

#### 4.2.2 核心流程

`syn_csb_master` 的核心是一个 5 状态 FSM：

```text
M_CSB_IDLE ──有请求──▶ M_CSB_START_REQ ──ready──┬─posted写─▶ WAIT_FOR_WR_COMP
                                                  ├─非posted写─▶(立即完成)IDLE
                                                  └─读────────▶ WAIT_FOR_RD_VALID
M_CSB_HOLD_REQ：请求被反压时锁存住，等 ready
WAIT_FOR_WR_COMP：等 scsb2mcsb_wr_complete
WAIT_FOR_RD_VALID：等 scsb2mcsb_valid 并回收 rdata
```

CSB 请求包 `mcsb2scsb_pd`（63 位）的字段布局（与 u2-l1 一致）：

```text
[62:61] level   [60:57] wrbe   [56] srcpriv   [55] nposted
[54]   write    [53:22] wdat(32b)   [21:0] addr
```

#### 4.2.3 源码精读

sequencer 读入激励文件（这是 trace-player 的“装弹”动作）：

[verif/synth_tb/csb_master_seq.v:106-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L106-L113) —— `$readmemh("input.txn.raw", cmd_memory)` 把回放用的十六进制激励装进命令数组；同时读 `slave_mem.cfg` 作为存储节流配置。

sequencer 的命令状态集（每种 trace 命令对应一个状态）：

[verif/synth_tb/csb_master_seq.v:33-50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L33-L50) —— 定义 `MSEQ_REG_WR/REG_RD/MEM_LD/MEM_DMP/WAIT/DONE` 等，正是 u1-l4 提到的“七类命令”在源码里的体现。

CSB 协议机的 FSM 定义与字段切片：

[verif/synth_tb/csb_master.v:44-50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L44-L50) 与 [verif/synth_tb/csb_master.v:84-88](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L84-L88) —— 请求包字段拆解与 5 个 FSM 状态宏。

FSM 主逻辑（组合段，决定下一个状态与各输出）：

[verif/synth_tb/csb_master.v:94-181](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L94-L181) —— 在 `M_CSB_START_REQ` 里根据 `write`/`nposted` 决定走写完成等待、立即完成、还是读等待三条分支。

> 说明：本节是理解“DUT 如何被驱动”的关键，更多 trace 命令格式与 fifo 细节见下一篇 u7-l2。

#### 4.2.4 代码实践

**实践目标**：跟踪一条“非 posted 寄存器写”命令在两级模块里的传递路径。

**操作步骤**：

1. 在 `csb_master_seq.v` 里找到 `MSEQ_REG_WR` 状态，看它如何置 `mseq_pending_req=1` 并把命令摆到 `mseq2mcsb_pd`。
2. 切到 `csb_master.v` 的 `M_CSB_START_REQ` 分支（约 112 行），找到 `mcsb2scsb_pd_write & !mcsb2scsb_pd_nposted`（非 posted 写）的处理。
3. 确认非 posted 写会在 ready 时立即拉 `mcsb2mseq_rvalid`，而不是去 `WAIT_FOR_WR_COMP`。

**需要观察的现象**：posted 写（`nposted=1`）才需要等 DUT 回 `wr_complete`；非 posted 写被当作“投递即完成”。

**预期结果**：你能用一句话解释为什么 posted 写和 non-posted 写走不同状态。

**运行结果**：待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：`csb_master_seq` 与 `syn_csb_master` 谁更靠近 DUT？
**答**：`syn_csb_master` 更靠近 DUT——它的 `mcsb2scsb_*` 直接经 tb_top 的 wire 连到 DUT 的 `csb2nvdla_*`；`csb_master_seq` 在更上游，只产生逻辑请求。

**练习 2**：为什么 sequencer 不直接驱动 DUT 的 CSB 口？
**答**：CSB 是严格的 valid/ready 握手协议，且有 posted/非 posted、读响应等时序分支；把这些细节统一交给 `syn_csb_master` 的 FSM，sequencer 才能保持“一条命令一步”的线性结构，便于维护和扩展命令集。

---

### 4.3 存储模型 wrapper：syn_slave_mem_wrap 与两块存储

#### 4.3.1 概念说明

DUT 有两组 AXI 存储口（DBB 主存、CVSRAM），TB 也要准备两组“假的 AXI 从端存储器”来接住它们。`syn_slave_mem_wrap` 就是把 **2 个 `syn_axi_slave` + 2 块 `slave_mem`** 包成一个模块，对 `tb_top` 暴露整齐的两组 AXI 端口。这种“wrapper”写法让顶层连线清爽，也方便参数化每块存储的地址范围。

两块存储用参数区分地址空间：

- `dbb_mem`：`slave_mem #(\`DBB_ADDR_START, \`DBB_MEM_SIZE)` —— 基址 `0x8000_0000`。
- `cvsram_mem`：`slave_mem #(\`CVSRAM_ADDR_START, \`CVSRAM_MEM_SIZE)` —— 基址 `0x5000_0000`。

地址映射宏见 [verif/synth_tb/syn_tb_defines.vh:94-100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L94-L100)。

#### 4.3.2 核心流程

wrapper 内部的数据流（每个 AXI slave ↔ 一块存储）：

```text
DUT core2dbb ──▶ syn_axi_slave#(0) ──(saxi02mem_* 命令)──▶ slave_mem dbb_mem
                       ▲                                       │
                       └────(mem2saxi0_* 读返回/写响应)────────┘
DUT core2cvsram ─▶ syn_axi_slave#(1) ─(saxi12mem_*)─▶ slave_mem cvsram_mem
                       ▲                                       │
                       └────(mem2saxi1_*)──────────────────────┘
```

关键点：AXI slave 与存储之间用一套**简化内部接口** `saxi2mem_*`（cmd_wr/cmd_rd/addr/data/wstrb/len/size）通信，不再是完整 AXI 五通道。这样存储模型只需理解“收到一个带长度的读/写命令”，逻辑大幅简化。

#### 4.3.3 源码精读

wrapper 模块与两个 AXI slave 实例：

[verif/synth_tb/slave_mem_wrap.v:190-202](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/slave_mem_wrap.v#L190-L202) —— `syn_axi_slave #(0) axi_slave0`，参数 `AXI_SLAVE_ID=0`，把 `saxi02mem_*` 连向存储。

[verif/synth_tb/slave_mem_wrap.v:247-302](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/slave_mem_wrap.v#L247-L302) —— `syn_axi_slave #(1) axi_slave1`，对应 CVSRAM 侧 `saxi12mem_*`。

两块存储实例（用参数区分地址范围）：

[verif/synth_tb/slave_mem_wrap.v:304-346](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/slave_mem_wrap.v#L304-L346) —— `slave_mem #(\`DBB_ADDR_START, \`DBB_MEM_SIZE) dbb_mem` 与 `slave_mem #(\`CVSRAM_ADDR_START, \`CVSRAM_MEM_SIZE) cvsram_mem`；注意 `.clk(fast_clk)`、`.slow_clk(clk)`——存储数组跑快钟、命令接口跑慢钟。

#### 4.3.4 代码实践

**实践目标**：确认 DBB 与 CVSRAM 的地址空间不重叠，并理解地址如何选存储。

**操作步骤**：

1. 打开 `syn_tb_defines.vh`，记录 `DBB_ADDR_START`、`CVSRAM_ADDR_START`、`DLA_ADDR_MASK` 的值。
2. 在 `slave_mem_wrap.v` 里核对 `dbb_mem` / `cvsram_mem` 的实例化参数。
3. 思考：DUT 发出一个地址 `0x8000_1234` 的读请求，会落到哪块存储？`0x5000_1234` 呢？

**需要观察的现象**：`DLA_ADDR_MASK = 0xffff_ffff_f000_0000`，即最高 4 位决定地址归属；`0x8...` 走 DBB、`0x5...` 走 CVSRAM。

**预期结果**：你能画出 DBB 与 CVSRAM 两个不重叠的地址区间。

**运行结果**：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 wrapper 要把存储数组接 `fast_clk`、命令接口接 `slow_clk`？
**答**：存储数组（`memory[]`）要在一个 AXI beat 内搬运 512 位数据（见 4.5），用快钟才能在慢钟一个周期内完成多次数组访问；命令/握手接口跑慢钟与 AXI slave 对齐。`clk_divider` 的注释也写明“memory clk is faster than axi_slave clk”。

**练习 2**：如果想让 TB 模拟“CVSRAM 容量比 DBB 小得多”，改哪里？
**答**：改 `syn_tb_defines.vh` 里 `CVSRAM_MEM_SIZE` 的定义（默认与 `MEM_SIZE` 相同），它决定 `slave_mem` 里 `memory[]` 数组的大小。

---

### 4.4 AXI slave：syn_axi_slave 与配套 FIFO

#### 4.4.1 概念说明

`syn_axi_slave` 是 TB 侧的 **AXI 从端协议机**。它要解决一个核心矛盾：AXI 的五个通道（AR/R/AW/W/B）在时间上是**相互独立**的——写地址（AW）可能先来、写数据（W）可能后到、写响应（B）要等 W 拍完；读地址（AR）和读数据（R）也要按 id 配对。而存储模型只想要“一个完整的命令”。于是 `syn_axi_slave` 用**一堆 FIFO** 把各通道的事务**缓存、配对、重排**成对存储友好的 `saxi2mem_*` 命令，再把存储返回的数据**按 AXI id 还原**成 R/B 响应。

这正是它例化了 7 个 FIFO 的原因：写地址、写数据、写选通、读地址各一个，id 类两个，响应类两个。

#### 4.4.2 核心流程

写通路：

```text
AW 通道 ──▶ waddr_fifo ──┐
W  通道 ──▶ wdata_fifo ──┼─▶ 配对/拼装 ──▶ saxi2mem_cmd_wr + addr/data/wstrb/len ──▶ 存储写
W  选通 ──▶ wstrb_fifo ──┘                                          │
                                                                    ▼
                                            存储写完成 ──▶ memresp ──▶ B 通道(bvalid/bid)
```

读通路：

```text
AR 通道 ──▶ raddr_fifo ──▶ saxi2mem_cmd_rd + addr/len ──▶ 存储读
                                                          │
存储读返回(id+data) ──▶ memresp_fifo ──▶ 按 id 还原 ──▶ R 通道(rvalid/rid/rlast)
```

FIFO 在这里承担三件事：**解耦各通道时序**、**维持事务顺序**、**支持背压**（`*_wr_busy` 反压上游 AXI 通道）。

#### 4.4.3 源码精读

AXI 五通道端口与内部命令端口声明：

[verif/synth_tb/axi_slave.v:67-119](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L67-L119) —— 上半是标准 AXI 五通道（AR/R/AW/W/B）；下半 `saxi2mem_*` 是发给存储的简化命令。

7 个 FIFO 的例化（这是理解本模块的“骨架”）：

[verif/synth_tb/axi_slave.v:248-338](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L248-L338) —— 依次例化 `wdata_fifo`、`wstrb_fifo`、`waddr_fifo`、`raddr_fifo`、两个 `id_fifo`（写/读 id）、两个 `memresp_fifo`（读/写响应数据）。每个 FIFO 都带 `*_wr_busy` 反压。

AXI 宽度等关键宏（决定每个 beat 多宽）：

[verif/synth_tb/syn_tb_defines.vh:3-14](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L3-L14) —— `AXI_ADDR_WIDTH=64`、`DATABUS2MEM_WIDTH=512`（64 字节/beat）、`AXI_AID_WIDTH=8`、`AXI_LEN_WIDTH=4`（最多 16 拍）。

#### 4.4.4 代码实践

**实践目标**：弄清一个 AXI 写事务在 `syn_axi_slave` 内部经过哪几个 FIFO。

**操作步骤**：

1. 打开 `axi_slave.v`，定位 7 个 FIFO 的例化（248–338 行）。
2. 对每个 FIFO，记录它的“写入触发条件”和“读出后送给谁”。例如 `waddr_fifo` 写入由 `awvalid & awready` 触发，读出用于生成 `saxi2mem_addr_wr`。
3. 列表归纳：写通路用哪几个 FIFO、读通路用哪几个。

**需要观察的现象**：写通路用到 `waddr_fifo` + `wdata_fifo` + `wstrb_fifo`（+ 写 id/memresp），读通路用到 `raddr_fifo`（+ 读 id/memresp）。

**预期结果**：你能复述 4.4.2 那张数据流图，并指明每个箭头对应的 FIFO 名。

**运行结果**：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么读返回数据要按 AXI `id` 还原，而不是按到达顺序直接发 R 通道？
**答**：AXI 允许多个不同 id 的读事务乱序 outstanding，DUT 用 `rid` 区分哪个响应对应哪个请求；若 TB 不按 id 还原，DUT 的 id 匹配逻辑会拿到错配数据。`memresp_fifo` 与 `id_fifo` 配合就是为此。

**练习 2**：`*_wr_busy` 信号在协议里起什么作用？
**答**：它是 FIFO 满标志，用来反压上游 AXI 通道（把对应的 `awready`/`wready`/`arready` 拉低），防止事务丢失——这就是 TB 存储模型的“可回压”来源之一。

---

### 4.5 行为存储模型：slave_mem 与带宽节流

#### 4.5.1 概念说明

`slave_mem`（在 `memory.v` 里）是真正“放数据”的地方——一块 `reg ... memory[TOTAL_MEM_SIZE]` 行为数组。它和 u6-l3 的综合 RAM 模型不同：那是给 RTL 用的宏单元，这是 **TB 专用、永不综合**的纯行为存储。

它额外干了一件重要的事：**带宽节流（throttle）**。真实 SoC 的主存带宽有限，DUT 不能假设存储无限快。于是 `slave_mem` 内置一个百分比式调度器：读、写请求按可配置的 `perc`（百分比）配额轮流放行，超配额就回压。这让仿真更接近真实系统的带宽约束。

#### 4.5.2 核心流程

存储 + 节流的工作方式：

```text
每次 fast_clk 沿：
  1. 在 read 相位 / write 相位间交替（rdNotWrtPhase）
  2. 若该相位有请求且 blocksOutstanding < perc * MAX_PORTS：
        - 读：memory[addr..] 取 512 位 → mem2slave_rdresp_data
        - 写：按 wstrb 逐字节写 memory[addr..]
        - 放行(ready=1)，并把本次请求的加权长度累加进 blocksOutstanding
  3. 否则回压(ready=0)
  4. 每周期按 perc 衰减 blocksOutstanding（模拟带宽恢复）
```

读写配额来自 `slave_mem.cfg`（由仿真前 `slave_mem.cfg.pl` 脚本生成），用 `$readmemh` 装入 `config_mem`。

> 默认编译宏 `MEM_WIDTH_4B` 下，`memory[]` 每个元素是 32 位，一次 512 位传输要读 16 个连续元素（见源码 233–248 行的逐段拼接），这就是“快钟一次完成多次数组访问”的来由。

#### 4.5.3 源码精读

存储数组与配置：

[verif/synth_tb/memory.v:50-55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/memory.v#L50-L55) —— `config_mem`（节流配置）与行为存储数组 `reg /*sparse*/ [\`MEM_WIDTH-1:0] memory[TOTAL_MEM_SIZE-1:0]`。

读写命令拆解子模块：

[verif/synth_tb/memory.v:113-147](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/memory.v#L113-L147) —— 例化 `slave2mem_wr`（把写命令排队并给出写地址/数据/掩码）与 `slave2mem_rd`（把读命令排队并给出读地址）。

节流主循环（核心调度逻辑）：

[verif/synth_tb/memory.v:163-270](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/memory.v#L163-L270) —— `always @(posedge clk or negedge reset)` 内按 `rdNotWrtPhase` 轮流处理读/写；超配额（`blocksOutstandingScaled >= perc*MAX_PORTS`）时回压 `rdy=0`；读时把 16 个 32 位元素拼成 512 位 `curr_rd_data`；写时按 `curr_wr_mask` 逐字节写。

配置装入与复位：

[verif/synth_tb/memory.v:150-153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/memory.v#L150-L153) —— `$readmemh("slave_mem.cfg", config_mem)` 在仿真开始时装入节流参数。

#### 4.5.4 代码实践

**实践目标**：理解节流百分比如何影响 DUT 的存储访问节奏。

**操作步骤**：

1. 打开 `memory.v`，找到 `blocksOutstandingScaled`、`perc`、`MAX_PORTS` 的用法（约 195–208 行）。
2. 找到 `slave_mem.cfg` 的生成脚本：`verif/synth_tb/sim_scripts/slave_mem.cfg.pl`（在 `verif/sim/Makefile` 第 216 行被调用）。
3. 阅读该脚本，找出控制读写百分比的字段（对应 `syn_tb_defines.vh` 的 `WR_PERC_0`/`RD_PERC_0`/`PERC_ALL`）。

**需要观察的现象**：`perc` 越小，`blocksOutstandingScaled` 越容易达到阈值，存储越频繁回压 → DUT 看到的存储越“慢”。

**预期结果**：你能说出“把 `PERC_ALL` 调小”会让仿真里存储带宽变低，从而暴露 DUT 在低带宽下的行为。

**运行结果**：待本地验证（若实际跑仿真，可对比不同 `slave_mem.cfg` 下的吞吐，但本实践以阅读为主）。

#### 4.5.5 小练习与答案

**练习 1**：`slave_mem` 的存储是综合友好的吗？为什么放在 TB 里？
**答**：不综合友好——它是 `reg` 数组加 `for` 循环逐字节写、还用了 `real`/`$countones` 等行为级结构，仅供仿真。放在 TB 里是为了给 DUT 提供一个可配置、可回压、能加载/转储的“假主存/CVSRAM”。

**练习 2**：为什么存储访问要分 read 相位和 write 相位交替？
**答**：用一个相位位在读写间轮转，模拟单端口存储“一次只能服务一个方向”的约束，并让读写按 `perc` 配额公平分享带宽，更接近真实存储控制器行为。

---

### 4.6 zemi3 监控：zemi3_tb 与 DPI-C

#### 4.6.1 概念说明

`zemi3_tb.sv` 是一个 **SystemVerilog** 模块（注意后缀 `.sv`），名字里的 “zemi3” 指 Synopsys 的 Zemi3 仿真加速编译器。它的角色是 **TB 的监控/检查器与仿真后端桥接**：通过 DPI-C 调用一组 C 函数来完成“初始化、加载存储镜像、转储存储镜像、结束仿真”。

需要诚实说明一点：在**默认的 VCS `synth_tb` 流程**里，`top` 并没有例化 `zemi3_tb`，存储的加载/转储与 `$finish` 实际是由 `csb_master_seq` 的 `MSEQ_MEM_LD`/`MSEQ_MEM_DMP`/`MSEQ_DONE` 命令、以及标准 `$finish` 完成的。`zemi3_tb` 是为 **ZeBu/Cadence 仿真加速（`EMU_TB`）等后端**准备的等价物——当走 `ZEBU` 或 `CADENCE` 宏分支时（`syn_tb_defines.vh` 里 `CADENCE` 会 `define EMU_TB`），这些后端不支持某些 Verilog 系统任务，于是改用 DPI-C 由 C 侧完成。所以它是“同一套 TB 在不同仿真后端上的监控/检查器变体”。

#### 4.6.2 核心流程

`zemi3_tb` 的三件事：

```text
initial:         z_initialize()                      // C 侧初始化
initial:         1000 个 clk 后 resetn=1              // 复位释放（与 tb_top 的 reset 呼应）
always(posedge): 若 dollar_finish → z_finish()        // 收到结束信号调 C 侧收尾
always(posedge): case(cs)
                    MSEQ_MEM_LD : z_readmemh(...)      // 把 hex 文件加载进存储
                    MSEQ_MEM_DMP: z_writememh(...)      // 把存储转储成 hex 文件
```

四个 DPI-C 函数都用 `(* zemi3_stream = 0 *)` 属性显式关闭流式编译（因为它们没有返回值，Zemi3 默认会当成流式函数处理）。

#### 4.6.3 源码精读

DPI-C 导入的四个 C 函数：

[verif/synth_tb/zemi3_tb.sv:17-33](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/zemi3_tb.sv#L17-L33) —— `z_initialize`、`z_readmemh`、`z_writememh`、`z_finish`，均带 `zemi3_stream=0` 属性。

复位释放与结束处理：

[verif/synth_tb/zemi3_tb.sv:35-48](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/zemi3_tb.sv#L35-L48) —— `initial` 里先 `z_initialize()`，再过 1000 个 `clk` 把 `resetn` 拉高；`always` 监测 `dollar_finish` 调 `z_finish()`。

存储加载/转储命令译码：

[verif/synth_tb/zemi3_tb.sv:50-59](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/zemi3_tb.sv#L50-L59) —— `case(cs)` 在 `MSEQ_MEM_LD`/`MSEQ_MEM_DMP` 时调用 `z_readmemh`/`z_writememh`，参数从 `curr_cmd` 里切出文件名与起止地址。

> 注意源码里这两处访问的路径是 `top.slave_mem_wrap.syn_mem.memory`，这是一个**历史实例名**，与当前 `slave_mem_wrap` 里的 `dbb_mem`/`cvsram_mem` 实例名不一致——这也是它属于遗留/特定后端路径、默认流程未启用的旁证。

#### 4.6.4 代码实践

**实践目标**：对比“默认 VCS 流程”与“`zemi3_tb` 后端”在“加载/转储存储”这件事上的不同实现。

**操作步骤**：

1. 在 `csb_master_seq.v` 里找到 `MSEQ_MEM_LD` 与 `MSEQ_MEM_DMP` 状态，看默认流程如何处理（通常是用 `$readmemh`/`$writememh` 直接操作 `slave_mem_wrap` 的存储，或交给 C-model）。
2. 对比 `zemi3_tb.sv` 第 50–59 行，它把同样的语义转成了 DPI-C 调用。
3. 在 `syn_tb_defines.vh` 里找到 `ZEBU` 与 `CADENCE` 宏分支（约 59–71 行），理解 `EMU_TB` 何时被定义。

**需要观察的现象**：两个实现解决的是同一个问题（加载/转储存储镜像、结束仿真），但一个用 Verilog 系统任务，一个用 DPI-C，分别适配不同仿真后端的能力。

**预期结果**：你能说清“为什么默认流程不依赖 `zemi3_tb`，但它仍留在 synth_tb 源码集里”。

**运行结果**：待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：`zemi3_tb` 用 DPI-C 而不是直接调 `$readmemh`/`$writememh`，主要动机是什么？
**答**：仿真加速后端（ZeBu）和某些 CADENCE 流程对部分 Verilog 系统任务支持有限或语义不同；改用 DPI-C 把这些操作下沉到 C 侧，可在不同后端上获得一致、可控的行为，并便于与 C-model/参考结果对接。

**练习 2**：在默认 `synth_tb` 仿真里，谁实际承担了 `zemi3_tb` 的“结束仿真 + 存储转储”职责？
**答**：主要由 `csb_master_seq` 承担——遇到 `MSEQ_DONE` 类命令时驱动 `$finish`，遇到 `MSEQ_MEM_LD/DMP` 时处理存储镜像的加载/转储；`zemi3_tb` 是这些职责在加速后端上的等价监控实现。

---

## 5. 综合实践：画出 trace-player 测试平台框图

把本讲四个最小模块串起来，完成下面这个**贯穿性任务**——这也是本讲规格里指定的实践。

**实践目标**：在 `tb_top.v` 中梳理 DUT 与 CSB master、AXI slave 的连接，画出完整测试平台框图，并标注时钟与复位来源。

**操作步骤**：

1. **清点顶层例化**：在 [`tb_top.v`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v) 里列出 `top` 模块例化的全部实例（`csb_mseq`、`csb_master`、`slave_mem_wrap`、`nvdla_top`、`soc2nvdla_time_gen0`、`mem_clk_gen`、`bw_mon`、`a0` 及若干 `bandwidth_throttle`）。
2. **追 CSB 通路**：从 `csb_mseq.mseq2mcsb_pd` → `csb_master.mcsb2scsb_*` → tb_top wire `mcsb2scsb_*` → `nvdla_top.csb2nvdla_*`；反向追响应 `nvdla2csb_*` → `scsb2mcsb_*` → `csb_master` → `csb_mseq`。再追中断 `nvdla_top.dla_intr` → `dla_intr` → `csb_mseq.dut2mseq_intr0`。
3. **追 AXI 通路**：`nvdla_top.nvdla_core2dbb_*` → wire `axi_slave0_*` → `slave_mem_wrap` → `axi_slave0` → `dbb_mem`；`core2cvsram_*` → `axi_slave1` → `cvsram_mem`。
4. **标注时钟/复位**：在框图上标出 `half_speed_clk`（喂 `nvdla_top`、`csb_mseq`、`csb_master`）、`mem_clk`（喂 `slave_mem_wrap`/`axi_slave`）、`mem_clk_fast`（喂 `slave_mem` 数组）、`reset`（喂所有模块）。
5. **画框图**：用方框 + 箭头画出，至少包含：激励（`input.txn.raw`）→ `csb_master_seq` → `syn_csb_master` → **DUT NV_nvdla** →（DBB/CVSRAM 两组 AXI）→ `syn_axi_slave` → `slave_mem`。

**需要观察的现象**：

- CSB 是**单向注入激励 + 双向回收响应/中断**的窄通路（63 位请求 + 32 位响应 + 1 位中断）。
- 两组 AXI 是**宽通路**（512 位数据），各自独立连一块存储。
- 时钟有三路（DUT/CSB 的 `half_speed_clk`、AXI/存储慢口的 `mem_clk`、存储快口的 `mem_clk_fast`），复位只有一路 `reset`。

**预期产出（参考框图，文字版）**：

```text
                         ┌───────────────┐
   input.txn.raw ───────▶│ csb_master_seq │◀──── dla_intr (中断轮询)
                         └───────┬───────┘
                          mseq2mcsb_pd / mcsb2mseq_*
                         ┌───────▼───────┐
                         │ syn_csb_master │   clk=half_speed_clk
                         └───────┬───────┘
                          mcsb2scsb_* / scsb2mcsb_*
        ┌────────────────────────▼────────────────────────┐
        │                 NV_nvdla (DUT)                   │   dla_core_clk=dla_csb_clk=half_speed_clk
        │                                                  │   dla_reset_rstn=reset
        └──────┬───────────────────────────┬──────────────┘
       core2dbb│(AXI 512b)         core2cvsram│(AXI 512b)
        ┌──────▼──────┐                ┌──────▼──────┐
        │syn_axi_slave#0│              │syn_axi_slave#1│  clk=mem_clk
        └──────┬──────┘                └──────┬──────┘
          saxi02mem_*                    saxi12mem_*
        ┌──────▼──────┐                ┌──────▼──────┐
        │ slave_mem    │              │ slave_mem    │  clk=fast_clk(mem_clk_fast)
        │ dbb_mem      │              │ cvsram_mem   │  slow_clk=mem_clk
        │ (0x8000_0000)│              │ (0x5000_0000)│
        └─────────────┘               └──────────────┘
```

**运行结果**：待本地验证（本实践为源码阅读 + 画图，建议你把上面这张图与你自己从源码追出来的连线逐一核对）。

## 6. 本讲小结

- TB 顶层是 `tb_top.v` 里名为 `top` 的模块，它造时钟、发复位、并把 DUT、CSB 激励、AXI 存储模型三类东西连起来。
- 激励分两级：`csb_master_seq` 回放 `input.txn.raw`，`syn_csb_master` 用 5 状态 FSM 把逻辑请求翻译成符合 CSB 握手的 `mcsb2scsb_*`。
- 存储模型由 `syn_slave_mem_wrap` 包住 2 个 `syn_axi_slave` + 2 块 `slave_mem`，分别对应 DBB（`0x8000_0000`）与 CVSRAM（`0x5000_0000`）。
- `syn_axi_slave` 用 7 个 FIFO 把 AXI 五通道事务解耦、配对、按 id 还原，是 TB 存储模型“可回压”的关键。
- `slave_mem` 是纯行为级存储数组，内置百分比式带宽节流，让仿真更接近真实带宽约束。
- `zemi3_tb.sv` 是面向 ZeBu/Cadence 加速后端的 SV/DPI 监控变体；默认 VCS 流程的等价职责由 `csb_master_seq` 承担。

## 7. 下一步学习建议

- 想深入 trace 命令的**字段格式与回放细节**，请看下一篇 **u7-l2 CSB 激励序列与 trace 格式**，它会把 `csb_master_seq` 的命令字段（`MSEQ_OP_BITS`/`MSEQ_ADDR_BITS`/…）和 `input.txn.raw` 的逐行含义讲透。
- 想知道 TB 怎么判断“这个测试到底过没过”，可顺带阅读 `verif/sim/checktest_synthtb.pl`（u1-l4 提到的 `checktest` 系列）。
- 对存储节流、带宽监控感兴趣的话，可以继续读 `tb_top.v` 里的 `bandwidth_mon` 与 `bandwidth_throttle`（本讲点到为止）。
- 回归 RTL 本身：理解了 TB 如何驱动 DUT 后，建议带着“一条 CSB 写最终触发了哪条卷积通路”的问题，重看单元 3（卷积主流水线）。
