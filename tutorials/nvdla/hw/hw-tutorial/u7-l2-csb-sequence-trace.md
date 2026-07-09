# CSB 激励序列与 trace 格式

## 1. 本讲目标

本讲聚焦 trace-player 测试平台里「激励如何变成对 DUT 的编程动作」这一段。学完后读者应能：

- 看懂 `input.txn` 文本 trace 中 `write_reg`/`read_reg`/`wait`/`load_mem`/`dump_mem` 等命令的格式，并说清 `inp_txn_to_hexdump.pl` 如何把它转成定宽十六进制 `input.txn.raw`。
- 说清 `csb_master_seq` 如何用状态机把每条命令译码成一个 63 位逻辑请求，并处理「写等待完成 / 读比较 / 轮询 / 等中断 / 超时」。
- 说清 `syn_csb_master` 如何把逻辑请求翻译成符合 CSB valid/ready 握手的请求包，并把 DUT 响应回送给 sequencer。
- 说明 `id_fifo`/`raddr_fifo`/`wdata_fifo`/`wstrb_fifo` 等事务 FIFO 在 AXI slave 存储模型里如何配对五通道、维持事务顺序与背压。
- 打开一个 sanity trace，识别出「先配置寄存器 → kick-off 引擎 → 轮询 GLB done 中断」的完整编程序列。

## 2. 前置知识

本讲承接 u7-l1（测试平台整体结构）与 u2-l1/u2-l2/u2-l4（CSB 协议与 GLB 中断）。需要先建立的直觉：

- **trace**：一段文本激励序列，每行一条对 DUT 的命令（寄存器读写 / 等中断 / 装载内存）。它是软件驱动在真实 SoC 上做的事的「录像」。
- **CSB**：NVDLA 内部配置空间总线，CPU 经它编程各引擎寄存器；请求有 valid/ready 握手，响应分读数据与写完成两类。
- **影偶（shadow）配置**：每个引擎有两组操作参数寄存器（producer/consumer 轮换），`OP_ENABLE` 是「点火」开关。
- **done 中断**：引擎完成一层计算后向 GLB 上报状态位，软件读 `S_INTR_STATUS` 寄存器（W1C 清除）来确认。
- **DUT**：被测设计，即顶层 `NV_nvdla`。

一句话定位：`csb_master_seq` 是「会读 trace 的 CPU 模型」，`syn_csb_master` 是它手里的「CSB 总线驱动器」，二者合力把一行行文本变成对 DUT 寄存器的真实读写。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `verif/synth_tb/csb_master_seq.v` | sequencer：用 `$readmemh` 读 `input.txn.raw`，状态机逐条译码命令、发逻辑请求、处理响应与超时 |
| `verif/synth_tb/csb_master.v` | `syn_csb_master`：把逻辑请求翻译成 CSB 握手包，回送读数据 / 写完成 |
| `verif/synth_tb/syn_tb_defines.vh` | trace 命令位域定义（op/addr/data/mask/compare/npolls）与命令码、FIFO 数据宽度 |
| `verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl` | 文本 `input.txn` → 定宽十六进制 `input.txn.raw` 的转换器 |
| `verif/synth_tb/id_fifo.v` 等 | AXI slave 用的同步 FIFO 原语，配对 AXI 五通道事务 |
| `verif/synth_tb/axi_slave.v` | 例化各 FIFO，把 AXI 事务配对后送存储阵列 |
| `verif/traces/traceplayer/sanity0/input.txn` 等 | 真实 trace 样例 |

## 4. 核心概念与源码讲解

### 4.1 trace 事务格式：从 input.txn 到 input.txn.raw

#### 4.1.1 概念说明

直接写给 DUT 的激励是文本文件 `input.txn`，人能读、但硬件不能直接回放。于是仿真前先用 Perl 脚本 `inp_txn_to_hexdump.pl` 把它转成定宽十六进制文件 `input.txn.raw`，再由 sequencer 用 `$readmemh` 一次性装进一段 `reg` 数组 `cmd_memory`。

`input.txn` 一行一条命令，`#` 之后是注释。共 7 种命令，每种对应一个 3 位（脚本里写成 2 位十六进制）命令码：

| 命令 | 码 | 语义 |
|---|---|---|
| `write_reg` | 0x00 | 向寄存器写一个 32 位字 |
| `read_reg`  | 0x01 | 读寄存器并与期望值比较（可轮询） |
| `write_mem` | 0x02 | 向存储写一个字 |
| `read_mem`  | 0x03 | 从存储读一个字 |
| `load_mem`  | 0x04 | 把数据文件后门装载进存储模型 |
| `dump_mem`  | 0x05 | 把存储模型某段后门 dump 成文件 |
| `wait`      | 0x06 | 阻塞等待 DUT 中断线拉高 |

#### 4.1.2 核心流程

转换流程是一条单向流水线：

```
input.txn (文本)
   │  inp_txn_to_hexdump.pl：按命令类型拼装定宽十六进制行
   ▼
input.txn.raw (每行一个 384 位命令的十六进制)
   │  csb_master_seq: $readmemh("input.txn.raw", cmd_memory)
   ▼
cmd_memory[0..N] (384 位 reg 数组，逐行回放)
```

每条命令在 `cmd_memory` 里占一项，宽 `MSEQ_CMD_SIZE = 384` 位。对寄存器类命令只用到低 128 位，高 256 位（`MSEQ_FILENAME_BITS`，[383:128]）留给 `load_mem`/`dump_mem` 存文件名。128 位寄存器命令的位域布局：

```
[127:120] op(8)   [119:88] addr(32)   [87:56] data(32)
[55:24]  mask(32) [23:16] compare(8)  [15:0] npolls(16)
```

其中 `addr` 这个 32 位字段又有内部结构：**低 16 位是寄存器字地址**，**高 16 位打包 CSB 的杂项字段**（level/wrbe/srcpriv/nposted，详见 4.3）。这就是 `input.txn` 注释里写的 `write_reg(reg_addr, reg_data, misc_bits)`——`misc_bits` 实际上塞进了地址的高位。

一个关键细节：trace 里的地址是**字地址**（每字 4 字节），换算成字节地址要乘 4。例如 GLB 的 `S_INTR_STATUS` 在 trace 里是 `0x0003`，字节偏移即 \(0x3 \times 4 = 0xc\)；BDMA 基址在 trace 里是 `0x1000`，字节地址即 `0x4000`。

#### 4.1.3 源码精读

命令码哈希表定义了文本命令到十六进制码的映射：[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:45-53](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L45-L53)。

`write_reg` 的拼装：取地址与数据各 8 位十六进制，后面补 0 凑齐 mask/compare/npolls 位域（共 14 个 0），最终 32 个十六进制字符 = 128 位：[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:124-143](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L124-L143)。关键两行：

```perl
$hex_string = $hex_string.$hex_addr_string.$hex_data_string.$padding_string;
# 结果形如: 00 ffff100b f0a5a500 00000000000000
```

`read_reg` 的拼装多了 bitmask、比较模式（`==`→00、`<=`→01、`>=`→02）和轮询次数：[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:144-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L144-L186)。文件末尾追加一条 `FF000000...` 作为结束标志（op=0xff = DONE）：[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:291](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L291-L291)。

位域定义与命令码在头文件中：[verif/synth_tb/syn_tb_defines.vh:23-40](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L23-L40)（位域）、[verif/synth_tb/syn_tb_defines.vh:42-49](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L42-L49)（命令码）。注意 `MSEQ_CMD_SIZE 384`、`MSEQ_NUM_CMDS 2000000`——最多可回放 200 万条命令。

sequencer 在 `initial` 块里把 raw 文件装入 `cmd_memory`（ZeBu 后端读 `input.txn.zebu`）：[verif/synth_tb/csb_master_seq.v:106-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L106-L113)。

真实样例 `sanity0`（最小冒烟测试：读默认值 → 写魔数 → 读回校验）：[verif/traces/traceplayer/sanity0/input.txn:3-5](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/input.txn#L3-L5)。

#### 4.1.4 代码实践

**目标**：亲手把 `sanity0` 的三条命令翻译成 `input.txn.raw` 的十六进制行，验证对格式的理解。

**步骤**：
1. 打开 `verif/traces/traceplayer/sanity0/input.txn`，看到三条命令。
2. 对第 3 行 `read_reg 0xffff100b 0xffffffe0 0x00000000`：命令码 `01`，地址 `ffff100b`，期望数据 `00000000`，bitmask `ffffffe0`，比较模式缺省 `==`→`00`，轮询次数缺省。按 read_reg 拼装规则写出十六进制行。
3. 对第 4 行 `write_reg 0xffff100b 0xf0a5a500`：命令码 `00`，地址 `ffff100b`，数据 `f0a5a500`，后补 14 个 0。
4. 把你手算的结果与脚本实际产物对比：在 `verif/sim` 下 `make run TESTDIR=../traces/traceplayer/sanity0` 后，到结果目录查看生成的 `input.txn.raw`。

**需要观察的现象**：`input.txn.raw` 每行恰好 32 个十六进制字符（128 位），第 4 行（write_reg）应为 `00ffff100bf0a5a50000000000000000`，末尾还有一条 `FF000000...` 结束标志。

**预期结果**：手算行与文件行逐字符一致。若不一致，先检查是否把地址高低位顺序搞反（脚本是「命令码 + 地址 + 数据」从高位到低位书写）。

> 若本地未配置 VCS，无法生成 `input.txn.raw`，则按上述规则手算后标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`write_reg 0xffff100b 0xf0a5a500` 中，`0xffff100b` 的低 16 位和高 16 位分别承载什么？
**答案**：低 16 位 `0x100b` 是 BDMA `CFG_DST_SURF_0` 寄存器的字地址；高 16 位 `0xffff` 是打包进地址高位的 CSB 杂项字段（level/wrbe/srcpriv/nposted），`0xffff` 是「标准非投递全字访问」的常用前缀。

**练习 2**：为什么 `sanity0` 第 3 行读期望值是 `0x00000000`，而第 5 行变成 `0xf0a5a500`？
**答案**：第 3 行在写之前读，校验寄存器复位默认值为 0；第 4 行写入魔数 `0xf0a5a500`；第 5 行读回，校验写入是否生效。这是典型的「写后读回」自检模式。

### 4.2 csb_master_seq 译码：trace 命令 → 逻辑请求

#### 4.2.1 概念说明

`csb_master_seq` 是 trace-player 的「大脑」。它用一个计数器 `line` 在 `cmd_memory` 里逐条推进，每条命令经状态机译码后产出一个 63 位逻辑请求 `mseq2mcsb_pd`（外加一个 `mseq_pending_req` 脉冲）。它还消费 DUT 回来的响应（`mcsb2mseq_rdata`/`mcsb2mseq_rvalid`），负责写完成确认、读值比较、轮询重试、等中断、超时失败等所有「CPU 驱动侧」的调度逻辑。

它和 `syn_csb_master` 的分工很清晰：seq 只懂「命令语义和时序」，不懂 CSB 握手；握手细节全交给下一节的 `syn_csb_master`。

#### 4.2.2 核心流程

状态机主干（复位后从 `BOOT_1` 起步，等若干拍让 DUT 复位稳定）：

```
BOOT_1 → BOOT_2(等 boot_timer) → IDLE
 IDLE:  取 cmd_memory[line]，按 op 分派:
        DONE      → DONE($finish)
        REG_WRITE → REG_WR → REG_WR_WAIT_RESP → IDLE
        REG_READ  → REG_RD → REG_RD_WAIT_RESP
                              ├ 匹配 → IDLE
                              └ 不匹配 → REG_RD_POLL_WAIT → REG_RD(重试)
                                         超过 npolls → REG_RD_MISMATCH
        WAIT      → WAIT(等 dut2mseq_intr0==1) → IDLE  (超时 → WAIT_TIMEOUT)
```

写请求时（`REG_WR`）把 63 位 pd 打包成「写」：

```
mseq2mcsb_pd[62:55] <= curr_cmd[PD_BITS];        // 杂项字段，来自 addr 高位
mseq2mcsb_pd[54]    <= 1;                         // write=1
mseq2mcsb_pd[21:0]  <= {6'b0, curr_cmd[ADDR_PD_BITS]}; // 寄存器字地址
mseq2mcsb_pd[53:22] <= curr_cmd[DATA_PD_BITS];   // 写数据 wdat
```

读请求时（`REG_RD`）同样的打包，只是 `[54]<=0`。读响应回来后在 `REG_RD_WAIT_RESP` 按 `EQ/LE/GE` 比较 `rdata` 与 `curr_cmd_data`：相等即通过；不等进 `POLL_WAIT`，等 `read_reg_poll_interval` 拍后重读，直到超过 `npolls` 次判为 `MISMATCH`。注意源码里比较是**直接比较**，bitmask 字段虽被解析但并未实际施加（`//TODO: mask`）——所以 trace 里写的 bitmask 当前不影响判定，仅作占位。

#### 4.2.3 源码精读

状态参数定义：[verif/synth_tb/csb_master_seq.v:33-50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L33-L50)。

`IDLE` 态按 op 分派：[verif/synth_tb/csb_master_seq.v:141-153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L141-L153)。

读请求的 pd 打包：[verif/synth_tb/csb_master_seq.v:365-371](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L365-L371)；写请求的 pd 打包：[verif/synth_tb/csb_master_seq.v:376-382](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L376-L382)（注意 `[54]` 读为 0、写为 1）。

读响应比较逻辑（EQ/LE/GE 三种）：[verif/synth_tb/csb_master_seq.v:184-209](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L184-L209)。

`WAIT` 态等中断线 `dut2mseq_intr0`：[verif/synth_tb/csb_master_seq.v:154-164](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L154-L164)。该信号在 `tb_top` 里连到 DUT 的 `dla_intr`（[verif/synth_tb/tb_top.v:187](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L187-L187)），即 GLB 聚合后的中断。

`DONE` 态调 `$finish` 结束仿真：[verif/synth_tb/csb_master_seq.v:268-276](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L268-L276)。

`line`/`count`/`timer` 三个计数器的推进逻辑（决定何时进下一条命令、何时轮询重试、何时超时）：[verif/synth_tb/csb_master_seq.v:493-546](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L493-L546)。

#### 4.2.4 代码实践

**目标**：用 `sanity1` 的 BDMA 搬运样例，看清「配置 → 点火 → 轮询完成」这一段在 trace 与状态机里的对应关系。

**步骤**：
1. 打开 `verif/traces/traceplayer/sanity1/input.txn`。
2. 找到 BDMA 的点火两行：`write_reg 0xffff100c 0x1`（`CFG_OP_0=1`，即 group 0 搬运）与 `write_reg 0xffff100d 0x1`（`CFG_LAUNCH0_0=1`，启动）。地址低 16 位 `0x100c`/`0x100d` 是 BDMA 的 `OP`/`LAUNCH0` 寄存器。
3. 找到末尾 `read_reg 0xffff0003 0x00000040 0x00000040`：读 GLB `S_INTR_STATUS`（字地址 `0x0003` = 字节 `0xc`），bitmask 与期望值都是 `0x00000040`，即期望 bit6（`bdma_done_status0`）被置位。
4. 对照状态机：`LAUNCH0` 写完走 `REG_WR_WAIT_RESP`；之后 `read_reg` 走 `REG_RD`→`REG_RD_WAIT_RESP`，若 BDMA 尚未完成则 bit6=0、比较不等、进 `REG_RD_POLL_WAIT` 反复重读，直到 BDMA done 把 bit6 拉起才匹配通过。

**需要观察的现象**：仿真日志里会出现多行 `MSEQ: Retrying read at 0xffff0003 ...`，直到某拍 `MSEQ: Read ... matched`。

**预期结果**：日志最终出现 matched，证明 BDMA 确实完成并上报了 GLB 中断。若日志直接 timeout，说明 BDMA 配置或存储模型有问题。

> 若无法运行仿真，可改为「源码阅读型实践」：在 [csb_master_seq.v:327-330](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master_seq.v#L327-L330) 的 `REG_RD_POLL_WAIT` 分支确认「重试前会打印 Retrying」，据此推断日志形态。

#### 4.2.5 小练习与答案

**练习 1**：`read_reg` 命令里写了 bitmask `0x00000040`，但比较时这个 mask 起作用了吗？
**答案**：没有。源码 `REG_RD_WAIT_RESP` 里直接 `rdata_no_x == curr_cmd_data`，mask 处标注 `//TODO: mask` 未实现。当前 trace 之所以仍能正确，是因为软件作者在写期望值时已自行保证「未 mask 的位也相等」（例如 `sanity0` 的魔数低 5 位本就是 0）。

**练习 2**：`MSEQ_REG_RD_MISMATCH` 与 `MSEQ_REG_RD_TIMEOUT` 有何区别？两者最终走向哪？
**答案**：`MISMATCH` 是「重试 `npolls` 次仍读不到期望值」；`TIMEOUT` 是「发出读后等 `read_timeout` 拍仍无 `rvalid`」（DUT 没响应）。两者都看 `continue_on_fail`：为 1 则回 `IDLE` 继续下一条，为 0 则进 `DONE` 终止仿真。`continue_on_fail` 由 `slave_mem.cfg` 的 `MSEQ_CONT_ON_FAIL` 配置，`sanity0` 的 `plusargs.txt` 写了 `+continue_on_fail`。

### 4.3 syn_csb_master：逻辑请求 → CSB 握手包

#### 4.3.1 概念说明

`syn_csb_master`（文件 `csb_master.v`）夹在 sequencer 与 DUT 之间。它把 seq 给的「裸」逻辑请求（`mseq2mcsb_pd` + `mseq_pending_req`）翻译成真正符合 CSB 协议的请求：拉 `mcsb2scsb_pvld`、驱动 63 位 `mcsb2scsb_pd`、等 `mcsb2scsb_prdy`，并把 DUT 回来的读数据 / 写完成回送给 seq（`mcsb2mseq_rdata`/`mcsb2mseq_rvalid`）。它还区分三类事务：非投递写（等 `wr_complete`）、投递写（立即返回）、读（等 `valid`+数据）。

63 位请求包 `mcsb2scsb_pd` 的字段布局（由本模块解码）：

| 位 | 字段 | 含义 |
|---|---|---|
| [62:61] | level | 优先级/层级 |
| [60:57] | wrbe | 写字节使能（4 位） |
| [56] | srcpriv | 源私有位 |
| [55] | nposted | 1=非投递（要写完成），0=投递 |
| [54] | write | 1=写，0=读 |
| [53:22] | wdat | 32 位写数据 |
| [21:0] | addr | 22 位地址（低 16 位有效） |

这正好解释了 4.1 里「addr 高 16 位装杂项字段」：seq 把 trace 地址的高位原样塞进 `pd[62:55]`，于是 `0xffff` 前缀就映射成 level=3、wrbe=0xf、srcpriv=1、nposted=1 的标准非投递全字写。

#### 4.3.2 核心流程

FSM 五态：

```
IDLE ──pending_req──▶ START_REQ
START_REQ: prdy=1 ?
   ├ 是 + 写 + nposted  → WAIT_FOR_WR_COMP  (等 wr_complete)
   ├ 是 + 写 + 投递     → 立即回 rvalid，回 IDLE/START_REQ
   ├ 是 + 读            → WAIT_FOR_RD_VALID (等 valid+data)
   └ 否                 → HOLD_REQ (锁存请求，保持 pvld 直到 prdy)
HOLD_REQ: prdy=1 → 同上分支
WAIT_FOR_WR_COMP: wr_complete=1 → 回 rvalid → IDLE/START_REQ
WAIT_FOR_RD_VALID: valid=1 → 回 rdata+rvalid → IDLE/START_REQ
```

`HOLD_REQ` 的存在是为了应对 DUT 暂时不收（`prdy=0`）：把请求锁存到 `latched_mseq_pd`，持续拉 `pvld` 直到 DUT 接收。每完成一笔，若 seq 已备好下一笔（`mseq_pending_req` 仍为 1）则直奔 `START_REQ`，否则回 `IDLE`。

#### 4.3.3 源码精读

pd 字段解码：[verif/synth_tb/csb_master.v:44-50](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L44-L50)。

FSM 状态定义：[verif/synth_tb/csb_master.v:84-88](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L84-L88)。

`START_REQ`：若 `prdy` 则驱动 `pvld`+`pd` 并置 `consumed_req=1`，再按 `write`/`nposted` 三分支跳转；否则进 `HOLD_REQ`：[verif/synth_tb/csb_master.v:112-134](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L112-L134)。

`HOLD_REQ` 用锁存值持续驱动：[verif/synth_tb/csb_master.v:135-155](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L135-L155)。

非投递写等 `wr_complete`：[verif/synth_tb/csb_master.v:156-166](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L156-L166)。读等 `valid` 并取数据：[verif/synth_tb/csb_master.v:167-178](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L167-L178)。

锁存与状态翻转（`latch_req` 有效时把 `mseq2mcsb_pd` 存入 `latched_mseq_pd`）：[verif/synth_tb/csb_master.v:184-194](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L184-L194)。

`tb_top` 里的例化与连线：seq 与 master 共享 `mseq2mcsb_pd`/`mcsb2mseq_*`，master 对外的 `mcsb2scsb_*`/`scsb2mcsb_*` 接 DUT 的 CSB 口：[verif/synth_tb/tb_top.v:176-215](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v#L176-L215)。

#### 4.3.4 代码实践

**目标**：在 CSB 请求实际发出处加一行日志，把每笔请求的地址、数据、读写位打出来，便于把 trace 行与 DUT 实际收到的 CSB 事务一一对应。

**步骤**：
1. 在 [csb_master.v:112-134](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/csb_master.v#L112-L134) 的 `M_CSB_START_REQ` 分支、`if (mcsb2scsb_prdy)` 命中处，加一行：
   ```systemverilog
   $display("%0t CSB: %s addr=0x%06x data=0x%08x",
            $time, mcsb2scsb_pd_write ? "WR" : "RD",
            mcsb2scsb_pd_addr, mcsb2scsb_pd_wdat);
   ```
2. 重新跑 `sanity0`，观察日志。
3. 把每行 `CSB:` 日志与 `input.txn` 的命令逐条对齐。

**需要观察的现象**：`sanity0` 应产生 3 笔 CSB 事务（1 读 + 1 写 + 1 读），地址都是 `0x100b`，写数据为 `0xf0a5a500`。

**预期结果**：每条 trace 命令恰好对应一笔 CSB 事务，地址与数据与手算一致；读事务之后没有 `wr_complete`，写事务之后有。

> 这是「源码阅读 + 加日志」型实践，不依赖完整跑通也可在阅读层面验证：`START_REQ` 里 `consumed_req=1` 只在 `prdy` 命中时拉起，说明每笔请求恰好被消费一次。

#### 4.3.5 小练习与答案

**练习 1**：为什么要有 `HOLD_REQ` 态，而不能在 `START_REQ` 里原地等 `prdy`？
**答案**：`START_REQ` 里组合逻辑把 `mcsb2scsb_pd` 直接接 `mseq2mcsb_pd`；一旦 `prdy=0` 进不了下一态，若不锁存，下一拍 seq 可能撤掉 `pending_req`/改 `pd`，请求就丢了。`HOLD_REQ` 把请求锁存到 `latched_mseq_pd` 并持续拉 `pvld`，保证直到 DUT 接收前请求稳定不变。

**练习 2**：投递写（`nposted=0`）与非投递写（`nposted=1`）对 sequencer 的时序影响有何不同？
**答案**：非投递写要等 DUT 回 `wr_complete` 才回 `rvalid`，seq 在 `REG_WR_WAIT_RESP` 多停若干拍；投递写一被接收就立即回 `rvalid`，seq 立刻进下一条。`0xffff` 前缀设 `nposted=1`，故 trace 里的写都是非投递的，能确认写真正到达寄存器。

### 4.4 事务 FIFO：AXI slave 的事务配对与背压

#### 4.4.1 概念说明

trace 的 `write_reg` 只负责「编程 + 点火」。引擎一旦被 kick-off，就会自主经 MCIF/CVIF 向外发 AXI 事务访问存储。这些事务打到测试平台的 `syn_axi_slave` 存储模型上。AXI 有五个独立通道（写地址 AW、写数据 W、写响应 B、读地址 AR、读数据 R），它们到达顺序未必一致，必须有人负责「配对」：把某笔写的 AW 和它的 W 数据、B 响应串起来，按 id 还原响应顺序，并在存储忙时回压。

`syn_axi_slave` 用一组同步 FIFO 原语完成这件事——`id_fifo`、`raddr_fifo`、`waddr_fifo`、`wdata_fifo`、`wstrb_fifo`、`memresp_fifo`。它们是「事务 fifo」，维持事务顺序与背压。注意它们**不在 CSB 路径上**，而是存储模型内部机制；本讲涉及它们是因为它们让「kick-off 之后引擎产生的存储流量」变得可观察、可回压，是 trace 驱动流程的「下半场」。

每个 FIFO 都是标准的「写侧 `wr_req`/`wr_busy`/`wr_empty` + 读侧 `rd_req`/`rd_busy`/`rd_data`」同步 FIFO，差别只在数据宽度：

| FIFO | 数据宽度 | 缓存内容 |
|---|---|---|
| `waddr_fifo` | `ADDR_FIFO_DATA_LEN`=79 | 写地址：`{awid, awaddr, awlen, awsize}` |
| `raddr_fifo` | 79 | 读地址：`{arid, araddr, arlen, arsize}` |
| `wdata_fifo` | `WDATA_FIFO_DATA_LEN`=520 | 写数据：`{wid, wdata(512)}` |
| `wstrb_fifo` | 64 | 写字节使能 `wstrb`（512 位/8） |
| `id_fifo` | `ID_FIFO_DATA_LEN`=77 | 在途事务：`{cmd_rd/wr, len, id, addr}` |
| `memresp_fifo` | — | 存储返回的响应数据，待发往 R/B 通道 |

#### 4.4.2 核心流程

写通路与读通路各自独立，结构对称：

```
写: DUT.AW ──▶ waddr_fifo ─┐
   DUT.W  ──▶ wdata_fifo ──┼─▶ 配对成存储写命令 ──▶ id_fifo(wrid2mem)
          └──▶ wstrb_fifo ─┘                              │
                                            存储阵列执行 ▼
                                          memresp_fifo(wrrsp) ──▶ DUT.B

读: DUT.AR ──▶ raddr_fifo ──▶ 存储读命令 ──▶ id_fifo(rdid2mem) ──▶ 存储阵列
                                                              └─▶ memresp_fifo(rdrsp) ──▶ DUT.R
```

`id_fifo` 是「事务登记簿」：每发一笔存储命令就压入一条 `{cmd, len, id, addr}`，待存储执行完、响应回到 `memresp_fifo` 后，再按 id 把响应配对送回对应 AXI 通道。`wr_busy`/`rd_busy` 是 FIFO 满时的回压信号——这就是 trace 里引擎流量过大时会被「卡住」的根因，也是 slave 存储模型「可回压」的关键。

#### 4.4.3 源码精读

FIFO 数据宽度定义：[verif/synth_tb/syn_tb_defines.vh:13-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L13-L15)。

FIFO 模块端口（统一接口，宽度不同）：`id_fifo` [verif/synth_tb/id_fifo.v:16-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/id_fifo.v#L16-L42)、`raddr_fifo` [verif/synth_tb/raddr_fifo.v:17-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/raddr_fifo.v#L17-L43)、`wdata_fifo` [verif/synth_tb/wdata_fifo.v:18-44](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/wdata_fifo.v#L18-L44)、`wstrb_fifo` [verif/synth_tb/wstrb_fifo.v:17-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/wstrb_fifo.v#L17-L43)。

`axi_slave.v` 里六个 FIFO 的例化（`wdata`/`wstrb`/`waddr`/`raddr` 各一个，`id_fifo` 例化两次分别管读/写，`memresp_fifo` 例化两次分别管读/写响应）：[verif/synth_tb/axi_slave.v:248-340](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L248-L340)。

写地址通道握手时把 `{awid, awaddr, awlen, awsize}` 压入 `waddr_fifo`：[verif/synth_tb/axi_slave.v:483](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L483-L483)；读地址通道同理压入 `raddr_fifo`：[verif/synth_tb/axi_slave.v:517](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L517-L517)。

把已配对的写命令压入 `id_fifo`（带 `SAXI2MEM_CMD_WR` 标志）：[verif/synth_tb/axi_slave.v:650](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L650-L650)；读命令压入另一个 `id_fifo`：[verif/synth_tb/axi_slave.v:665](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L665-L665)。

从 `id_fifo` 读出在途命令、拆成 `{cmd, len, id, addr}` 送存储：写侧 [verif/synth_tb/axi_slave.v:690](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L690-L690)、读侧 [verif/synth_tb/axi_slave.v:763](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L763-L763)。

#### 4.4.4 代码实践

**目标**：把六个 FIFO 的「宽度 + 缓存内容 + 所属 AXI 通道」整理成一张表，建立「存储模型如何配对五通道」的直觉。

**步骤**：
1. 读 [axi_slave.v:248-340](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L248-L340) 的六处例化，记录每个 FIFO 实例名。
2. 读 [syn_tb_defines.vh:13-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/syn_tb_defines.vh#L13-L15) 算出 `ADDR_FIFO_DATA_LEN`、`WDATA_FIFO_DATA_LEN`、`ID_FIFO_DATA_LEN` 的具体位宽（分别 79、520、77）。
3. 追踪 `waddr_fifo_bus`（[axi_slave.v:483](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L483-L483)）和 `wrid_fifo_wr_bus`（[axi_slave.v:650](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/axi_slave.v#L650-L650)）的拼装，确认每段字段对应 AXI 哪个通道。

**需要观察的现象**：写通路有 4 个 FIFO（waddr/wdata/wstrb/id），读通路有 2 个（raddr/id），响应通路 2 个 memresp——共 8 个实例（id/memresp 各 2），与 u7-l1 说的「7 个 FIFO 解耦五通道」一致（7 类模块，部分例化两次）。

**预期结果**：能画出 4.4.2 那张配对流程图，并指出 `id_fifo` 是唯一携带「读/写命令标志 + 在途长度」的事务登记簿。

> 这是源码阅读型实践，无需运行仿真。

#### 4.4.5 小练习与答案

**练习 1**：为什么写通路需要 `wdata_fifo` 和 `wstrb_fifo` 两个独立 FIFO，而不合成一个？
**答案**：AXI 的 W 通道上 `wdata` 与 `wstrb` 是同拍并行的两根线，且 slave 内部对它们的使用节奏可能不同（数据要进存储阵列、strobe 要做字节掩码）。分成两个 FIFO 各自缓冲，解耦更干净，且 `wstrb` 宽度仅 64 位、`wdata` 宽 520 位（含 id），合并会浪费位宽并增加拼装复杂度。

**练习 2**：`id_fifo` 里的 `cmd` 位（`SAXI2MEM_CMD_WR/RD`）有什么用？
**答案**：它标记这条在途事务是读还是写，使存储响应侧知道该把返回数据送回 R 通道（读）还是 B 通道（写），并能按 id 配对响应与原始请求，维持 AXI 的事务顺序与完成语义。

## 5. 综合实践

把本讲知识串起来，走一遍 `verif/traces/traceplayer/conv_8x8_fc_int16/input.txn` 的完整生命周期，画出「trace 行 → CSB 事务 → 引擎行为 → 中断回报」的时间线。

1. **装数据**：开头 `load_mem 0x80000000 ... sample_surf.dat` —— 经 `inp_txn_to_hexdump.pl` 转成 `load_mem` 命令（op=0x04，文件名塞进命令高 256 位），seq 在 `MSEQ_MEM_LD` 态用 `$readmemh` 把输入特征图后门装进 DBB 存储模型（`0x8000_0000` 段）。
2. **配置各引擎寄存器**：一连串 `write_reg` 写 CDMA/CSC/CMAC/CACC/SDP 的数据格式、地址、尺寸、影偶参数（producer 组）。
3. **kick-off（下游优先）**：连续写各引擎 `D_OP_ENABLE=1`，顺序是 SDP→CACC→CMAC_A→CMAC_B→CSC→CDMA（见 [conv_8x8_fc_int16/input.txn:211-224](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/conv_8x8_fc_int16/input.txn#L211-L224)）。**最下游的 SDP 先点火、最上游的 CDMA 最后点火**——先把消费者备好再放生产者，避免数据到达时下游还没就绪而丢失。CDMA 一旦使能便开始从存储取数，整条流水线才真正流动。
4. **轮询 done 中断**：kick-off 后逐个 `read_reg 0xffff0003 <mask> <exp>` 轮询 GLB `S_INTR_STATUS`，等待每个引擎的 done 位置位，再用 `write_reg 0xffff0003 0xffffffff` 清除（W1C）。各引擎 done 位由 [NV_NVDLA_GLB_CSB_reg.v:185](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L185-L185) 的位拼装决定：`sdp_done_status0`=bit0、`cdma_dat_done_status0`=bit16、`cdma_wt_done_status0`=bit18、`cacc_done_status0`=bit20，与 trace 里的掩码 `0x1`/`0x10000`/`0x40000`/`0x100000` 完全吻合。
5. **等中断 + dump 结果**：中间穿插一条 `wait`（阻塞到 `dla_intr` 拉高），末尾 `dump_mem 0x80400000 0x20 output_feature_map.dat` 把输出特征图 dump 出来，供与 C-model 比对。

**交付物**：画一张时间线图，横轴是 `line`（trace 行号），标注「load_mem → 配置区 → kick-off 区 → 轮询区 → wait → dump」五段，并在 kick-off 区标出下游优先的使能顺序，在轮询区标出每个 done 位的掩码值。

> 若本地能跑仿真，可加 4.3.4 的日志补丁，把 CSB 事务流与上面时间线对齐，验证「每条 write_reg 恰好一笔 CSB 写、每条 read_reg 恰好一笔 CSB 读 + 若干重试」。

## 6. 本讲小结

- `input.txn` 是文本激励，7 种命令；`inp_txn_to_hexdump.pl` 把每行转成 384 位定宽十六进制 `input.txn.raw`，由 seq 用 `$readmemh` 装入 `cmd_memory`。
- 命令的 32 位地址字段：低 16 位是寄存器**字地址**（×4 得字节偏移），高 16 位打包 CSB 杂项字段（level/wrbe/srcpriv/nposted），`0xffff` 前缀即标准非投递全字访问。
- `csb_master_seq` 用状态机逐条译码，产出 63 位逻辑请求 `mseq2mcsb_pd`，并负责写完成确认、读比较（EQ/LE/GE，bitmask 暂未实现）、轮询重试、`wait` 等中断、超时失败。
- `syn_csb_master` 把逻辑请求翻译成 CSB valid/ready 握手（IDLE→START_REQ→HOLD_REQ/WAIT_FOR_WR_COMP/WAIT_FOR_RD_VALID），区分非投递写、投递写、读三类，并把 DUT 响应回送 seq。
- `id_fifo`/`raddr_fifo`/`wdata_fifo`/`wstrb_fifo` 等是 AXI slave 存储模型的事务 FIFO，配对 AXI 五通道、按 id 还原响应、用 busy 回压——它们让 kick-off 后的引擎存储流量可观察、可回压。
- 真实 trace 的生命周期是「load_mem 装数据 → 配置寄存器 → 下游优先 kick-off → 轮询 GLB done 位并 W1C 清除 → wait → dump_mem 比对」。

## 7. 下一步学习建议

- **u7-l3（C-model 参考模型）**：本讲末尾 `dump_mem` 出来的输出特征图如何与黄金参考比对？去 `cmod/` 看 C 模型如何生成期望输出。
- **u8-l4（端到端编程一个网络层）**：把本讲看到的「配置 + 下游优先 kick-off + 轮询 done」抽象成一份完整的伪代码启动序列，并理解影偶 producer/consumer 在其中的协作。
- **延伸阅读**：对照 `spec/manual/test.rdl`（u8-l2）确认 `S_INTR_STATUS` 各 done 位的寄存器定义，验证本讲引用的掩码与 RDL 单一可信源一致；再读 `verif/sim/Makefile`（u1-l4）弄清 `input.txn.raw` 在 `make run` 里是哪一步生成的。
