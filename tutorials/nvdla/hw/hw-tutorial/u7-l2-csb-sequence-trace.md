# CSB 激励序列与 trace 格式

## 1. 本讲目标

在 [u7-l1](u7-l1-traceplayer-testbench.md) 里我们搭好了 trace-player 测试平台的“骨架”：`tb_top` 造时钟、发复位，`csb_master_seq`（sequencer）回放 trace，`syn_csb_master` 把逻辑请求翻译成 CSB 握手，`syn_axi_slave` 充当可回压的存储模型。本讲要钻进这条激励链的“神经”——**一段文本 trace 是怎样一步步变成对 DUT 寄存器的真实 CSB 写的**。

读完本讲你应当能够：

1. 说清 `input.txn`（人读的文本）到 `input.txn.raw`（机器读的 384 位定宽十六进制）的转换过程，以及每个命令字的字段布局。
2. 跟着 `csb_master_seq` 的状态机，讲明白一条 `write_reg` / `read_reg` 是如何被译码、发出、并（必要时）轮询校验的。
3. 跟着 `syn_csb_master` 的 5 状态有限状态机，讲明白一个“逻辑请求”如何被翻译成符合 CSB valid/ready 握手的 63 位请求包，以及读、投递写、非投递写三种事务的等待差异。
4. 认识 `id_fifo` / `raddr_fifo` / `wdata_fifo` / `wstrb_fifo` 这组事务 FIFO 在 `axi_slave` 里如何维持 AXI 事务顺序与背压（注意：它们服务于存储侧，不是 CSB 侧）。
5. 打开一个真实 sanity trace，识别“先配置、后 kick-off 卷积引擎”的寄存器写序列。

> 命名提示：本仓库里有两个名字很像、但角色相反的模块，千万别混淆：
> - **`syn_csb_master`**（在 TB 里，[verif/synth_tb/csb_master.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L2-L22)）：测试平台一侧的 **CSB 主机**，扮演“写寄存器的 CPU”，本讲的主角。
> - **`NV_NVDLA_csb_master`**（在 DUT 里，u2-l2 讲过）：DUT 一侧的 **CSB 路由器**，把收到的 CSB 请求扇出到各引擎。
> 一句话：TB 的 master 发，DUT 的 master 收并分发。

---

## 2. 前置知识

- **CSB 协议回顾（来自 [u2-l1](u2-l1-csb-bus-apb2csb.md) / [u2-l2](u2-l2-csb-master-router.md)）**：CSB 是 NVDLA 内部的配置空间总线。请求侧有 `valid`/`ready` 握手，携带 16 位字地址、32 位写数据、`write`、`nposted` 等位；响应侧有读数据 `valid`/`data` 与写完成 `wr_complete`。本讲的 TB master 就是在忠实模拟一个 CPU 驱动按这套协议发请求。
- **trace / trace-player（来自 u7-l1）**：trace 是一段文本激励序列，描述“往哪个地址写什么、读什么、等多久”。trace-player 指 sequencer 回放这段序列驱动 DUT。
- **posted / non-posted 写**：posted 写（`nposted=0`）“投递即完成”，CPU 不必等回执；non-posted 写（`nposted=1`）必须等 `wr_complete` 才算落定。对配置寄存器这种“必须确认生效”的访问，软件通常用 non-posted 写。
- **轮询（poll）**：读一个状态寄存器，若不是期望值就隔一段时间再读，直到满足或超时。NVDLA trace 里 `read_reg` 天生带“期望值 + 比较模式 + 重试次数”三个字段，正是为轮询而生。
- **背压（backpressure）**：下游来不及处理时，用 `ready=0`（或 FIFO 的 `wr_busy=1`）顶住上游，让上游停一拍。本讲的 master FSM 与存储侧 FIFO 都靠它避免丢事务。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [verif/synth_tb/csb_master_seq.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L4-L16) | **trace 译码器**：`$readmemh` 读入 `input.txn.raw`，用状态机把每条命令字翻译成一次寄存器/存储访问，负责轮询、超时、校验。 |
| [verif/synth_tb/csb_master.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L2-L22) | **CSB 协议适配器**（模块 `syn_csb_master`）：把 sequencer 给出的“逻辑请求”按 valid/ready 握手打成 63 位 CSB 请求包，并回收响应。 |
| [verif/synth_tb/syn_tb_defines.vh](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L20-L49) | **格式圣经**：定义命令字宽度、各字段位段、操作码、FIFO 数据宽度。trace 格式与译码逻辑都以它为单一可信源。 |
| [verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L1-L53) | **格式转换器**：把人读的 `input.txn` 文本编译成 `$readmemh` 能吃的定宽十六进制 `input.txn.raw`。 |
| [verif/traces/traceplayer/sanity0/input.txn](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/input.txn#L1-L6) | 最小冒烟 trace：读默认值→写魔数→读回校验。 |
| [verif/traces/traceplayer/sanity3/input.txn](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L1-L40) | 真实卷积 trace：含完整 CDMA 配置与 OP_ENABLE kick-off 序列。 |
| [verif/synth_tb/axi_slave.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L246-L323) | 存储侧 AXI slave，例化了本讲的 4 个事务 FIFO。 |
| [verif/synth_tb/id_fifo.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/id_fifo.v#L16-L42)、[raddr_fifo.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/raddr_fifo.v#L17-L43)、[wdata_fifo.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/wdata_fifo.v#L18-L44)、[wstrb_fifo.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/wstrb_fifo.v#L17-L43) | 自动生成的标准 FIFO 原语，分别在 axi_slave 中承载 AXI 事务的 id / 读地址 / 写数据 / 写字节掩码。 |

---

## 4. 核心概念与源码讲解

### 4.1 trace 事务格式：input.txn 与命令字布局

#### 4.1.1 概念说明

一段 trace 就是“CPU 驱动的一份脚本”：每一行告诉加速器“做什么访问”。但 RTL 里的 sequencer 不会去解析英文字符串，它读的是 `$readmemh` 能装载的**定宽十六进制数组**。所以 trace 有两种形态：

- **`input.txn`**：人读的文本，例如 `write_reg 0xffff1405 0x11001100`。可读、可注释（`#` 之后是注释）。
- **`input.txn.raw`**：机器读的定宽十六进制，每行是一个 **384 位的命令字**（`MSEQ_CMD_SIZE`），由 `inp_txn_to_hexdump.pl` 编译生成，运行时被 `$readmemh` 装进 `cmd_memory` 数组。

这种“文本源 → 编译 → 定宽二进制”的设计，让 trace 既好写好评审，又能在仿真里零开销回放。

#### 4.1.2 核心流程

一条 trace 的生命周期：

```text
input.txn (文本)
   │  inp_txn_to_hexdump.pl  （make run 阶段调用）
   ▼
input.txn.raw (每行一个 384-bit 命令字)
   │  $readmemh("input.txn.raw", cmd_memory)  （csb_master_seq 的 initial 块）
   ▼
cmd_memory[0..N]  （reg [383:0] 数组）
   │  csb_master_seq 状态机逐条译码
   ▼
mseq2mcsb_pd[62:0] 逻辑请求 + mseq_pending_req
```

命令字 384 位的字段布局定义在 [syn_tb_defines.vh:L23-L40](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L23-L40)：

| 字段 | 位段 | 宽度 | 含义 |
| --- | --- | --- | --- |
| `MSEQ_OP_BITS` | 127:120 | 8 | 操作码（见下表） |
| `MSEQ_ADDR_BITS` | 119:88 | 32 | 寄存器/存储地址 |
| `MSEQ_DATA_BITS` | 87:56 | 32 | 写数据 / 读期望数据 |
| `MSEQ_MASK_BITS` | 55:24 | 32 | 读比较位掩码 |
| `MSEQ_COMPARE_BITS` | 23:16 | 8 | 比较模式（EQ/LE/GE） |
| `MSEQ_NPOLLS_BITS` | 15:0 | 16 | 读轮询最大次数 |
| `MSEQ_FILENAME_BITS` | 383:128 | 256 | load/dump_mem 的文件名 |

操作码（[syn_tb_defines.vh:L42-L49](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L42-L49)）：

| 操作码值 | 文本命令 | 含义 |
| --- | --- | --- |
| `0x00` | `write_reg` | 写一个 32 位寄存器 |
| `0x01` | `read_reg` | 读寄存器并按掩码/期望值校验（可轮询） |
| `0x02` / `0x03` | `write_mem` / `read_mem` | 经 AXI 读写存储 |
| `0x04` / `0x05` | `load_mem` / `dump_mem` | 后门装载/转储存储数组（喂测试数据、取结果） |
| `0x06` | `wait` | 等 DUT 中断或超时 |
| `0xff` | `done` | trace 结束 |

#### 4.1.3 源码精读

`inp_txn_to_hexdump.pl` 用一张哈希表把文本命令映射成两位操作码，逐行把 `input.txn` 编译成定宽十六进制（[inp_txn_to_hexdump.pl:L45-L53](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L45-L53)）：

```perl
my %command_hash = (
    'write_reg' => '00',
    'read_reg'  => '01',
    'write_mem' => '02',
    'read_mem'  => '03',
    'load_mem'  => '04',
    'dump_mem'  => '05',
    'wait'      => '06',
);
```

对一条 `write_reg`，它把操作码 + 地址 + 数据拼起来，再按 `MSEQ_MASK_BITS/COMPARE_BITS/NPOLLS_BITS` 的总宽度补零到 384 位（[inp_txn_to_hexdump.pl:L124-L143](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L124-L143)）。注释里点明了一个关键约定——**对 CSB，地址的高 16 位是协议杂项（misc），低 16 位才是真正的寄存器字地址**：

```perl
# For CSB, top 16 bits are misc, lower 16 are addr
my $address = $values[1];
...
$hex_string = $hex_string.$hex_addr_string.$hex_data_string.$padding_string;
```

`read_reg` 的编译更丰富：地址、期望数据、位掩码、比较模式（`==`/`<=`/`>=` → `00`/`01`/`02`）、可选轮询次数（[inp_txn_to_hexdump.pl:L144-L186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L144-L186)）。整个文件末尾追加一行 `FF00...` 作为 `done` 结束标记（[inp_txn_to_hexdump.pl:L291-L292](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L291-L292)）。

最小冒烟 trace [sanity0/input.txn](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/input.txn#L1-L6) 演示了三种命令的形态：

```text
# load_mem(addr, offset,  file)
# write_reg(reg_addr, reg_data, misc_bits)
read_reg 0xffff100b 0xffffffe0 0x00000000  # 读 BDMA 寄存器，掩码 0xffffffe0，期望默认值 0
write_reg 0xffff100b 0xf0a5a500             # 写魔数 0xf0a5a500
read_reg 0xffff100b 0xffffffe0 0xf0a5a500  # 读回校验（掩码后应等于魔数）
```

注意 `read_reg` 在文本里是 `<addr> <bitmask> <expected_data>`（比较模式缺省为 `==`）。这里第一次 `read_reg` 校验复位默认值，最后一次校验刚写的魔数是否真的落地——这正是配置类 trace 的典型“写后验”模式。

#### 4.1.4 代码实践

**目标**：亲手把一条 `write_reg` 编译成 384 位命令字，验证字段布局。

**步骤**：

1. 取 `write_reg 0xffff1405 0x11001100`（sanity3 里配置 CDMA 的 `D_MISC_CFG`）。
2. 按 [syn_tb_defines.vh:L23-L40](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L23-L40) 的字段从高位到低位拼接：`op(8)=00`、`addr(32)=ffff1405`、`data(32)=11001100`、`mask(32)=0`、`compare(8)=0`、`npolls(16)=0`、`filename(256)=0`。
3. 得到 96 位有效十六进制：`00_ffff1405_11001100_00000000_00_0000`，其余补零。

**预期**：拼接结果应与 perl 脚本对 `write_reg` 的输出一致——前 24 个十六进制字符正是 `00ffff14051100110000000000000000`（操作码 + 地址 + 数据 + 零填充），后面是全 0 的 mask/compare/npolls/filename。**若不一致，请回头核对字段位段是否抄错。**

> 说明：本实践是“纸笔编译”，不需要运行仿真；目的是让你记住命令字布局。运行时 `make run` 会自动调用 perl 脚本生成 `.raw`，无需手工编译。

#### 4.1.5 小练习与答案

**练习 1**：`read_reg 0xffff1401 0xffffffff 0x0003000f` 这一行编译后，`MSEQ_COMPARE_BITS` 和 `MSEQ_NPOLLS_BITS` 分别是什么？
**答**：文本只有 4 段，比较模式缺省 `==` → `compare=0x00`；未给轮询次数 → 脚本用默认 `0xc350`（50000，见脚本 `$read_reg_poll_retries`）填入 `npolls`。

**练习 2**：为什么 `MSEQ_CMD_SIZE` 要定到 384 位，而不是刚好装下 `write_reg` 的 96 位？
**答**：因为同一数组要兼容 `load_mem`/`dump_mem`，它们需要带文件名（`MSEQ_FILENAME_BITS` 256 位）。取所有命令里最宽的，统一成 384 位定宽，`$readmemh` 才能整齐装载、sequencer 才能用统一的 `curr_cmd` 切片读字段。

---

### 4.2 csb_master_seq：trace 译码状态机

#### 4.2.1 概念说明

`csb_master_seq` 是 trace 的“大脑”。它做三件事：① 在 `initial` 块里用 `$readmemh` 把 `input.txn.raw` 装进 `cmd_memory`；② 用一个状态机逐条取出命令字（`curr_cmd = cmd_memory[line]`）；③ 按 `op` 码把命令翻译成对 `syn_csb_master` 的逻辑请求（`mseq2mcsb_pd` + `mseq_pending_req`），并处理读校验、轮询、超时。

它对 `syn_csb_master` 暴露的接口非常克制（[csb_master_seq.v:L21-L25](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L21-L25)）：一个“有请求”标志 `mseq_pending_req`、一个 63 位请求载荷 `mseq2mcsb_pd`、一个“请求被消费”回执 `mcsb2mseq_consumed_req`、以及读返回数据 `mcsb2mseq_rdata`/`rvalid`。sequencer 只管“要不要发、发什么”，CSB 握手的时序细节全丢给 `syn_csb_master`。

#### 4.2.2 核心流程

装载（仿真 0 时刻一次性完成，[csb_master_seq.v:L106-L113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L106-L113)）：

```verilog
`ifdef ZEBU
   $readmemh("input.txn.zebu", cmd_memory);
`else
   $readmemh("input.txn.raw", cmd_memory);   // 默认 VCS 走这条
`endif
$readmemh("slave_mem.cfg", config_mem);      // 装载仿真配置（超时值、轮询间隔等）
```

主状态机围绕 `line`（当前命令下标）推进，关键状态（[csb_master_seq.v:L33-L50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L33-L50)）：

```text
MSEQ_BOOT_1/2 ──► MSEQ_IDLE ──┬─ REG_WR ──► REG_WR_WAIT_RESP ──► IDLE
                              ├─ REG_RD ──► REG_RD_WAIT_RESP ──┬─ 匹配 ──► IDLE
                              │                                 └─ 不匹配 ──► REG_RD_POLL_WAIT ──► REG_RD（重试）
                              │                                                   └─ 超过 npolls ──► REG_RD_MISMATCH
                              └─ WAIT（等中断 dut2mseq_intr0 或超时）
MSEQ_DONE（$finish）
```

- `MSEQ_IDLE`：取下一条命令，按 `op` 分派（[csb_master_seq.v:L141-L153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L141-L153)）。
- `MSEQ_REG_WR` / `MSEQ_REG_RD`：组装 `mseq2mcsb_pd` 并拉高 `mseq_pending_req`。
- `MSEQ_REG_RD_WAIT_RESP`：等 `mcsb2mseq_rvalid`，按 EQ/LE/GE 比较 `(rdata & mask)` 与期望值（[csb_master_seq.v:L179-L212](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L179-L212)）；不匹配则进轮询。
- `MSEQ_WAIT`：等 DUT 中断 `dut2mseq_intr0` 拉高，或 `wait_timeout` 到（[csb_master_seq.v:L154-L164](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L154-L164)）——这是“跑完一层等中断”的机制。

每完成一条命令，`line` 自增（[csb_master_seq.v:L503-L507](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L503-L507)）取下一条，直到 `done`。

#### 4.2.3 源码精读

命令字到逻辑请求的组装是本模块的“心脏”。读和写共用同一套切片，只差 `write` 位（[csb_master_seq.v:L365-L382](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L365-L382)）：

```verilog
MSEQ_REG_WR: begin
   mseq_pending_req <= 1;
   mseq2mcsb_pd[62:55] <= curr_cmd[`MSEQ_PD_BITS];      // 地址高位 → CSB 协议杂项(level/wrbe/srcpriv/nposted)
   mseq2mcsb_pd[54]    <= 1;                             // write=1
   mseq2mcsb_pd[21:0]  <= {6'b0, curr_cmd[`MSEQ_ADDR_PD_BITS]}; // 低16位地址 → CSB 字地址
   mseq2mcsb_pd[53:22] <= curr_cmd[`MSEQ_DATA_PD_BITS]; // 32位数据 → wdat
end
```

可见 sequencer 把 384 位命令字的字段重新打包进 63 位 `mseq2mcsb_pd`：

- 命令字地址的高位段（`MSEQ_PD_BITS` = 119:105）→ CSB 包的协议杂项段 `pd[62:55]`（对应 level/wrbe/srcpriv/nposted）；
- 命令字地址的低位段（`MSEQ_ADDR_PD_BITS` = 103:88，即地址低 16 位）→ CSB 包的字地址 `pd[21:0]`；
- 命令字数据段（`MSEQ_DATA_PD_BITS` = 87:56）→ CSB 包的 `wdat pd[53:22]`；
- `pd[54]` 由读/写状态决定。

这条切片链印证了 4.1 节 perl 注释的约定：**32 位 trace 地址的高 16 位是 CSB 协议杂项、低 16 位是寄存器字地址**。例如 `0xffff1405`：低 16 位 `0x1405` 是 CDMA `D_MISC_CFG` 的字地址，高位 `0xffff` 走协议杂项段。

读校验的比较逻辑（[csb_master_seq.v:L184-L209](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L184-L209)）用 `EQ/GE/LE` 三种模式判 `rdata_no_x`（已把 `x` 清成 0 的读数据，见 [csb_master_seq.v:L100-L104](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L100-L104)）。注意源码里比较用的是整 `rdata_no_x` 而非 `mask` 后的值（`//TODO: mask`），与 perl 文本侧“声明了掩码但脚本未真正逐位掩”是对得上的——掩码字段在当前实现里主要起占位作用。

#### 4.2.4 代码实践

**目标**：跟着状态机走一遍 sanity0 的第一条 `read_reg`。

**步骤**：

1. 在 [csb_master_seq.v:L141-L153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L141-L153) 确认 `MSEQ_IDLE` 看到 `op=0x01`（read_reg）会跳到 `MSEQ_REG_RD`。
2. 在 [csb_master_seq.v:L365-L371](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L365-L371) 看 `MSEQ_REG_RD` 如何把 `curr_cmd` 的地址/数据段打进 `mseq2mcsb_pd`，并把 `pd[54]` 置 0（读）。
3. 进入 `MSEQ_REG_RD_WAIT_RESP`，等 `mcsb2mseq_rvalid`；到来后按 `EQ` 比较 `0x00000000`（sanity0 第一条期望值）。

**需要观察的现象**：仿真日志里会出现 sequencer 的 `$display`，例如 `MSEQ: read_cmd address 0x... with data 0x...`（[csb_master_seq.v:L289-L293](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L289-L293)）与匹配成功信息 `MSEQ: Read (command N) matched ...`（[csb_master_seq.v:L335-L338](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L335-L338)）。

**预期**：sanity0 是冒烟测试，三条命令都应“matched”，最终走到 `MSEQ_DONE` 触发 `$finish`。**若日志出现 `MSEQ: ERROR: ... did not return the expected value`，说明读回值与期望不符——多半是 DUT 还没复位好或地址写错。** 待本地验证具体日志时间戳。

#### 4.2.5 小练习与答案

**练习 1**：`MSEQ_WAIT` 状态等的是什么信号？为什么需要它？
**答**：等 `dut2mseq_intr0`（DUT 上报的中断）。卷积一层跑完后 GLB 会聚合各引擎 `done` 拉中断（见 [u2-l4](u2-l4-glb-config-interrupts.md)），trace 用 `wait` 命令挂起 sequencer 直到中断到来，避免盲猜执行时间；同时有 `wait_timeout` 兜底防止死等。

**练习 2**：如果一条 `read_reg` 永远读不到期望值，会发生什么？
**答**：状态机在 `REG_RD → REG_RD_WAIT_RESP → REG_RD_POLL_WAIT → REG_RD` 间循环重试，每次 `count` 加 1；当 `count > curr_cmd_polls`（命令字里的 `npolls`）时进 `MSEQ_REG_RD_MISMATCH`。若 `continue_on_fail=1` 则跳过这条继续下一条，否则进 `MSEQ_DONE` 结束仿真（[csb_master_seq.v:L219-L225](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L219-L225)）。

---

### 4.3 syn_csb_master：从逻辑请求到 CSB 握手

#### 4.3.1 概念说明

sequencer 给的是“抽象请求”（要不要发、发什么），但 CSB 总线要求严格的 valid/ready 握手与响应回收。`syn_csb_master`（模块名见 [csb_master.v:L2-L22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L2-L22)）就是这个协议适配层：它把 sequencer 的请求按节拍送上 CSB，并根据事务类型（读 / 投递写 / 非投递写）决定要不要等响应。

#### 4.3.2 核心流程

63 位 CSB 请求包 `mcsb2scsb_pd[62:0]` 的字段布局（[csb_master.v:L30-L50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L30-L50)）：

| 字段 | 位 | 说明 |
| --- | --- | --- |
| level | 62:61 | 协议等级 |
| wrbe | 60:57 | 写字节使能 |
| srcpriv | 56 | 源私有位 |
| nposted | 55 | 1=非投递写（需等 wr_complete） |
| write | 54 | 1=写，0=读 |
| wdat | 53:22 | 32 位写数据 |
| addr | 21:0 | 字地址（低 16 位有效，高 6 位补 0） |

5 状态 FSM（[csb_master.v:L83-L88](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L83-L88)）：

```text
                 mseq_pending_req
IDLE ─────────────────────────────► START_REQ
                                        │
              prdy=0 (被反压)            │ prdy=1, 消费请求
           ┌────────────────────────────┤
           ▼                            ▼
        HOLD_REQ (保持请求)         按 write/nposted 分三路：
                                     ├ write &  nposted → WAIT_FOR_WR_COMP (等 wr_complete)
                                     ├ write & !nposted → 立即 rvalid，回 IDLE/START_REQ（投递写）
                                     └ !write (读)      → WAIT_FOR_RD_VALID (等读数据)
```

三类事务的等待语义不同，正是对真实 CPU 驱动行为的建模：

- **读**：必须等 `scsb2mcsb_valid` 拿到数据，把它通过 `mcsb2mseq_rdata` 回送给 sequencer（[csb_master.v:L167-L178](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L167-L178)）。
- **非投递写**（`nposted=1`）：必须等 `scsb2mcsb_wr_complete` 才算完成（[csb_master.v:L156-L166](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L156-L166)）。
- **投递写**（`nposted=0`）：握手成功即立刻回 `rvalid`，不等回执（[csb_master.v:L121-L130](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L121-L130)）。

#### 4.3.3 源码精读

`START_REQ` 状态是握手核心。被 `prdy` 顶住时进 `HOLD_REQ` 保住请求不丢，否则消费请求并按事务类型分流（[csb_master.v:L112-L134](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L112-L134)）：

```verilog
`M_CSB_START_REQ: begin
   latch_req = 1;
   if (mcsb2scsb_prdy) begin
      mcsb2scsb_pvld = 1'b1;
      mcsb2scsb_pd   = mseq2mcsb_pd;      // 把 sequencer 的请求送上 CSB
      mcsb2mseq_consumed_req = 1;          // 告诉 sequencer：这条我收下了
      if (mcsb2scsb_pd_write & mcsb2scsb_pd_nposted)
         m_csb_st_next = `M_CSB_WAIT_FOR_WR_COMP;  // 非投递写：等写完成
      else if (mcsb2scsb_pd_write & !mcsb2scsb_pd_nposted) begin
         mcsb2mseq_rvalid = 1;              // 投递写：立即完成
         ...
      end else if (!mcsb2scsb_pd_write)
         m_csb_st_next = `M_CSB_WAIT_FOR_RD_VALID; // 读：等数据
   end else
      m_csb_st_next = `M_CSB_HOLD_REQ;     // 被反压：保持
end
```

`latch_req` 配合 `HOLD_REQ`：当某拍 `prdy=0`，本拍先把请求锁进 `latched_mseq_pd`（[csb_master.v:L184-L194](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L184-L194)），下一拍起 `HOLD_REQ` 持续把 `latched_mseq_pd` 摆在总线上直到握手成功（[csb_master.v:L135-L155](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L135-L155)）。这保证了一次请求要么被完整接收、要么一直坚持，不会半途丢失——这就是 master 侧的背压正确性。

> 小细节：源码 [csb_master.v:L30-L50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L30-L50) 注释把 `wdat` 写成 `53:52`，但紧随其后的 `assign ... = mcsb2scsb_pd[53:22]` 表明真实宽度是 32 位 `[53:22]`——以 assign 为准。

#### 4.3.4 代码实践

**目标**：在波形/源码上确认一次“读事务要等两拍”。

**步骤**：

1. 在 [csb_master.v:L128-L130](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L128-L130) 看到 `!write` 时进 `WAIT_FOR_RD_VALID`。
2. 在 [csb_master.v:L167-L178](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L167-L178) 看该状态要等 `scsb2mcsb_valid`，到来后才 `mcsb2mseq_rdata = scsb2mcsb_pd` 并回 `rvalid`。

**需要观察的现象**：用 `DUMP=1 DUMPER=VERDI` 跑 sanity0（见 [u1-l4](u1-l4-first-simulation.md)），在 `debussy.fsdb` 里看 `m_csb_st_curr`：读事务会从 `START_REQ` 跳到 `WAIT_FOR_RD_VALID`，停若干拍（取决于 DUT csb_master 路由器回数据的延迟）再回 `IDLE`。

**预期**：读地址寄存器这种简单读，DUT 侧几拍内回 `scsb2mcsb_valid`；若长期停在 `WAIT_FOR_RD_VALID`，说明 DUT 没响应——多半是地址落到了未实现区间。**待本地验证具体延迟拍数。**

#### 4.3.5 小练习与答案

**练习 1**：为什么非投递写要比投递写多一个 `WAIT_FOR_WR_COMP` 状态？
**答**：非投递写要求 DUT 明确回 `wr_complete` 表示“写真的落到寄存器了”，所以 master 必须等这个回执才能告诉 sequencer “完成”；投递写则“送出手就算完”，不等回执，适合对顺序不敏感、可容忍丢失风险的批量写。配置寄存器这类“必须确认生效”的访问应走非投递写。

**练习 2**：`HOLD_REQ` 状态解决了什么问题？
**答**：当 `mcsb2scsb_prdy=0`（DUT 没准备好接收）时，请求不能撤。`HOLD_REQ` 把上一拍锁存的请求持续摆在总线上直到 `prdy=1`，保证一次请求完整送达——这是 master 在面对下游背压时保持事务完整性的标准做法。

---

### 4.4 事务 FIFO 与背压：axi_slave 的存储侧通路

#### 4.4.1 概念说明

trace 里除了 `write_reg`/`read_reg`（走 CSB），还有 `load_mem`/`dump_mem`（后门直接读写存储数组）和卷积运行时 DUT 自己发起的 AXI 读写（DUT 经 MCIF/CVIF 访问 DBB/CVSRAM）。后者由 `syn_axi_slave` 接收并喂给 `slave_mem` 行为存储。AXI 是五通道协议（AW/W/B/AR/R），事务之间可能乱序、可能并发，slave 必须把“哪个地址的哪笔数据、对应哪个 id 的响应”理清楚，并能在自己忙不过来时背压住 DUT。

承担这个“理清顺序 + 背压”职责的，正是 `axi_slave.v` 里例化的一组 FIFO。本讲的四个 FIFO 原语——`id_fifo`、`raddr_fifo`、`wdata_fifo`、`wstrb_fifo`——就在这里各司其职。

> 重要边界：这四个 FIFO **在存储侧 AXI slave 里**，服务于 DUT 的 AXI 存储访问，**不在 CSB 主路径上**。CSB 路径（4.2/4.3 节）是配置寄存器的“窄”通路；这里是数据搬运的“宽”通路。两者同属一个 TB，但职责不同。

#### 4.4.2 核心流程

`axi_slave` 把 AXI 各通道事务分别缓存，再按 id 配对还原响应（实例化见 [axi_slave.v:L246-L323](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L246-L323)）：

| FIFO 实例 | 原语 | 深度 | 数据宽度 | 承载内容 |
| --- | --- | --- | --- | --- |
| `axi_slave_wdata_fifo` | `wdata_fifo` | 64 | 8+512=520 | AXI 写数据（W 通道 id+512 位数据） |
| `axi_slave_wstrb_fifo` | `wstrb_fifo` | 64 | 64 | 写字节掩码（wstrb） |
| `axi_slave_raddr_fifo` | `raddr_fifo` | 384 | 8+64+4+3=79 | 读地址事务（AR 通道 id+addr+len+size） |
| `wrid2mem_fifo` / `rdid2mem_fifo` | `id_fifo`（×2） | 448 | 8+64+4+1=77 | 发往存储模型的写/读命令（cmd 位+id+addr+len） |

数据宽度定义见 [syn_tb_defines.vh:L13-L18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L13-L18)：

```verilog
`define ADDR_FIFO_DATA_LEN  `AXI_AID_WIDTH + `AXI_ADDR_WIDTH + `AXI_LEN_WIDTH + `AXI_SIZE_WIDTH  // 读地址
`define WDATA_FIFO_DATA_LEN `AXI_AID_WIDTH + `DATABUS2MEM_WIDTH                              // 写数据
`define ID_FIFO_DATA_LEN    `AXI_AID_WIDTH + `AXI_ADDR_WIDTH + `AXI_LEN_WIDTH + 1             // +1 位读/写命令
```

每个 FIFO 的工作模型是经典的“写侧计数、读侧计数、满则 `wr_busy` 背压上游、空则无数据”：

```text
DUT AXI 主端口 ──wr_req──► [FIFO 写侧] ──(满? wr_busy=1 背压)──► [RAM] ──rd_data──► [FIFO 读侧] ──► slave_mem / 响应通道
```

#### 4.4.3 源码精读

以 `id_fifo` 为例（[id_fifo.v:L16-L42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/id_fifo.v#L16-L42)），它对外只暴露写侧（`wr_req`/`wr_data`/`wr_busy`/`wr_empty`）和读侧（`rd_req`/`rd_data`/`rd_busy`）——典型的标准 FIFO 接口。背压的核心是 `wr_busy` 的产生（[id_fifo.v:L73-L99](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/id_fifo.v#L73-L99)）：

```verilog
wire wr_count_next_no_wr_popping_is_448 = ( wr_count_next_no_wr_popping == 9'd448 );
wire wr_busy_next = wr_count_next_is_448 ||              // 接近满（深度 448）
                    (wr_limit_reg != 9'd0 &&             // 或超过可编程限额
                     wr_count_next >= wr_limit_reg) ...;
```

当写侧计数将达 448（深度上限）或超过仿真 plusarg 设的限额，下一拍 `wr_busy` 拉高，回顶写请求方（即 AXI slave，进而背压 DUT）。存储体是一块 448×52 的 flop-RAM（[id_fifo.v:L149-L156](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/id_fifo.v#L149-L156)），单写口单读口、按 `wr_adr`/`rd_adr` 环形寻址。其余三个 FIFO 结构同构，只是深度/宽度不同（`raddr_fifo` 384×54、`wdata_fifo` 64×519、`wstrb_fifo` 64×64）。

在 `axi_slave` 内，这些 FIFO 这样串起来维持事务秩序（以写通路为例）：DUT 的 AW/W 通道事务先进 `wdata_fifo`/`wstrb_fifo`/`waddr_fifo`，slave 把“地址+id+len+写命令位”拼成 77 位压入 `wrid2mem_fifo`（`id_fifo` 实例，[axi_slave.v:L302-L312](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L302-L312)），存储模型按出队的命令把数据写进 `slave_mem` 并按 id 回 B 通道响应。读通路同理用 `raddr_fifo` + `rdid2mem_fifo`（[axi_slave.v:L288-L298](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L288-L298)、[axi_slave.v:L313-L323](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L313-L323)）。`id` 字段全程跟随，正是 AXI 多 outstanding 事务“按 id 还原响应顺序”的依据。

#### 4.4.4 代码实践

**目标**：体会 FIFO 深度差异背后的设计意图。

**步骤**：

1. 对比四个 FIFO 的深度：`id_fifo`=448、`raddr_fifo`=384、`wdata_fifo`/`wstrb_fifo`=64（见 [axi_slave.v:L246-L323](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L246-L323) 的注释 `// Write fifo depths of 64`、`// Read fifo depths of 384`、`// ID fifo depth of 384+64=448`）。
2. 思考：为什么 id fifo（448）= 读地址（384）+ 写相关（64）？

**需要观察的现象**：`id_fifo` 深度恰好等于 `raddr_fifo` 与 `wdata_fifo` 深度之和，因为所有发往存储模型的读命令和写命令最终都要排进 id 命令队列，其容量需能容纳“最多在读侧排队的 + 最多在写侧排队的”。

**预期**：你能用自己的话解释“id 命令队列容量 = 读侧容量 + 写侧容量”这一关系。**若算不出 448=384+64，请回看 [axi_slave.v:L300](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L300) 的注释 `// ID fifo depth of 384+64=448`。**

#### 4.4.5 小练习与答案

**练习 1**：`wr_limit_reg` 这个“可编程限额”是干什么用的？
**答**：仿真时可通过 plusarg（如 `id_fifo_wr_limit`）把 FIFO 的有效深度人为调小，提前触发背压，从而在仿真里制造“下游变慢”的极端场景，检验 DUT 在存储带宽紧张时是否仍正确。综合时它恒为 0（用满物理深度）。

**练习 2**：为什么 `wstrb_fifo` 的数据宽度是 64 位？
**答**：写数据总线 `DATABUS2MEM_WIDTH=512` 位 = 64 字节，每个字节需要一个写使能位，故写掩码（wstrb）正好 64 位（[syn_tb_defines.vh:L11](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L11) 与 [wstrb_fifo.v:L40](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/wstrb_fifo.v#L40)）。

---

## 5. 综合实践：跟踪一条寄存器写到引擎寄存器的全链路

把本讲四节串起来，做一次端到端的“一行 trace 走到底”追踪。取 sanity3 里配置 CDMA 的一行：

```text
write_reg 0xffff1405 0x11001100   # NVDLA_CDMA.D_MISC_CFG：全输入/全权重、DIRECT、INT16
```

**任务**：画出这行从文本到 CDMA 寄存器落值的完整数据通路，并标注每一跳对应的源码。

**参考追踪链**（请逐跳在源码里确认）：

1. **文本 → 命令字**：`inp_txn_to_hexdump.pl` 把它编成 `00 ffff1405 11001100 <零填充>`（[inp_txn_to_hexdump.pl:L124-L143](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L124-L143)），写入 `input.txn.raw`。
2. **命令字 → 数组**：`csb_master_seq` 的 `$readmemh` 装进 `cmd_memory[line]`（[csb_master_seq.v:L106-L113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L106-L113)）。
3. **数组 → 逻辑请求**：状态机 `IDLE→REG_WR` 把字段切片进 `mseq2mcsb_pd`（`pd[54]=1` 写、`pd[21:0]=0x1405`、`pd[53:22]=0x11001100`），拉高 `mseq_pending_req`（[csb_master_seq.v:L376-L382](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L376-L382)）。
4. **逻辑请求 → CSB 包**：`syn_csb_master` 的 `START_REQ` 等 `prdy`，握手后把 `mseq2mcsb_pd` 送上 `mcsb2scsb_*`（[csb_master.v:L112-L134](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L112-L134)）。
5. **CSB 包 → DUT**：经 `tb_top` 连线进 DUT 顶层 CSB 端口（u7-l1），再由 DUT 内 `NV_NVDLA_csb_master` 路由器按地址 `0x1405` 译码分发到 CDMA 寄存器文件（u2-l2）。
6. **落值**：CDMA 的 `_dual_reg`/`_single_reg` 把 `0x11001100` 写进 `D_MISC_CFG` 对应触发器（u2-l3）。

**进阶观察**：sanity3 在写完所有 CDMA 配置寄存器后，会在 [sanity3/input.txn:L211-L224](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity3/input.txn#L211-L224) 按 **SDP→CACC→CMAC_A→CMAC_B→CSC→CDMA** 的逆流水线顺序写各引擎 `D_OP_ENABLE=1`，把 CDMA（数据源）**留到最后**点火——这样当第一批数据从 CDMA 流出时，下游每一级都已装好配置、处于待命，整条卷积流水线才能一次性点亮、无空泡。这正是 producer/consumer 影偶配置（u2-l3）与 done 中断（u2-l4）协同的用武之地。

> 完成后建议：用 `DUMP=1 DUMPER=VERDI` 跑一遍 sanity3（`cd verif/sim && make run TESTDIR=../traces/traceplayer/sanity3`），在波形里沿上述 6 跳抓 `mseq2mcsb_pd` → `mcsb2scsb_pd` → DUT 内部寄存器的值变化，亲眼确认 `0x11001100` 一路无损落到 CDMA。具体波形抓取步骤待本地验证。

---

## 6. 本讲小结

- trace 有两副面孔：人读的 `input.txn` 文本与机器读的 `input.txn.raw`（384 位定宽命令字），由 `inp_txn_to_hexdump.pl` 编译、`$readmemh` 装载。
- 命令字字段布局（op/addr/data/mask/compare/npolls/filename）是 sequencer 与 perl 脚本共同的“格式圣经”，定义在 `syn_tb_defines.vh`；32 位地址的高 16 位是 CSB 协议杂项、低 16 位是寄存器字地址。
- `csb_master_seq` 是 trace 译码大脑：状态机逐条取命令、切片成 `mseq2mcsb_pd`，并对 `read_reg` 做 EQ/LE/GE 比较与轮询、对 `wait` 等中断。
- `syn_csb_master` 是 CSB 协议适配层：5 状态 FSM 把逻辑请求按 valid/ready 握手打成 63 位包，读等数据、非投递写等 `wr_complete`、投递写立即返回；`HOLD_REQ` 保证被反压时请求不丢。
- `id_fifo`/`raddr_fifo`/`wdata_fifo`/`wstrb_fifo` 四个事务 FIFO 位于 **axi_slave 存储侧**（非 CSB 侧），靠“写侧计数满则 `wr_busy`”背压上游、靠 id 字段维持 AXI 多 outstanding 事务的响应顺序；深度设计 448=384+64。
- 真实 trace 的卷积编程序列：先把 CDMA/CSC/CMAC/CACC/SDP 各寄存器写满配置（每写后常跟一条 read_reg 读回校验），再按逆流水线顺序写 `OP_ENABLE` 点火，CDMA 最后启动。

---

## 7. 下一步学习建议

- **横向接 C-model**：本讲只看了 trace 怎么“喂”RTL。下一讲 [u7-l3 C-model 参考模型](u7-l3-cmodel-reference.md) 会讲 `cmod/` 如何作为黄金参考产生期望输出，与本讲的 `dump_mem` 取回的 RTL 输出做比对——这是判断仿真 PASSED 的最终依据。
- **深入寄存器语义**：本讲把寄存器当“地址+数据”黑盒。回到 [u2-l3](u2-l3-register-files-shadow-config.md) 与 [u8-l2](u8-l2-rdl-ordt-reggen.md)，看 `D_OP_ENABLE`、`S_POINTER` 这些字段是如何由 SystemRDL 自动生成、如何驱动 producer/consumer 影偶切换的。
- **亲手写一条 trace**：在 sanity0 基础上加一行 `write_reg` 改某个 GLB 中断屏蔽位（见 [u2-l4](u2-l4-glb-config-interrupts.md) 的 `INTR_MASK`），重跑仿真，用本讲的链路追踪法确认你的写真的生效——这是把“读源码”变成“会调参”的关键一步。
- **存储侧纵深**：若对 AXI 事务 FIFO 意犹未尽，可继续读 `axi_slave.v` 的命令/响应配对逻辑与 `slave_mem` 的带宽节流模型（u7-l1 提过），理解 TB 如何逼真地模拟有限带宽的存储器。
