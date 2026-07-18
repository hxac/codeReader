# 寄存器堆与 ALU

## 1. 本讲目标

上一讲（u4-l2）我们走完了主状态机 `cpu_state` 的八态全景，把 CPU 当成一台「取指 → 读寄存器 → 执行 → 访存 → 回写」的多周期调度器来看。本讲打开这台调度器里最核心的两块**数据通路硬件**：

1. **寄存器堆（register file）**——32 个通用寄存器 `x0..x31` 到底存在哪里、怎么读、怎么写；为什么 PicoRV32 提供双端口与单端口两种实现，二者对状态机有什么影响。
2. **ALU（算术逻辑单元）**——`add/sub/and/or/xor/slt` 以及分支比较的结果，如何由一段组合逻辑 `alu_out` 一次算出。

学完本讲，你应当能够：

- 说出 `cpuregs` 数组的存储结构、`regfile_size` 怎么由参数推导，以及 `x0` 恒为 0 是怎么用一行代码实现的。
- 解释 `ENABLE_REGS_DUALPORT` 开与关时，读端口数量、`decoded_rs` 多路选择与 `cpu_state_ld_rs2` 状态之间的因果关系。
- 读懂 `alu_add_sub / alu_eq / alu_lts / alu_ltu` 四个中间信号，并追踪 `alu_out` 与 `alu_out_0` 两个 `case` 如何为不同指令挑选最终结果。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：寄存器堆是「带多个端口的小内存」。** RISC-V 的每条运算指令最多要读两个源寄存器（`rs1`、`rs2`）、写一个目的寄存器（`rd`）。如果把寄存器堆看作一块小 RAM，那么它至少需要「读端口 + 写端口」。**读端口越多，面积越大、频率越容易做高**；PicoRV32 给你选择：要 2 个读端口（双端口）还是 1 个读端口（单端口）。

**直觉二：读端口数决定了「一条指令需要几个周期」。** 双端口能在同一个周期里同时读出 `rs1` 和 `rs2`，于是 `add x3,x1,x2` 在 `ld_rs1` 状态一次就把两个源操作数备齐，直接进入执行。单端口一次只能读一个，`add` 这类「双源」指令就得多走一个 `ld_rs2` 状态去读第二个操作数——这是上一讲提到的「单端口 CPI 多 1」的硬件根因。

**直觉三：ALU 是「并行算一堆，再选一个」的组合电路。** PicoRV32 的 ALU 不是「先译码出操作码再决定算什么」，而是**同时**算出加/减、相等、有符号小于、无符号小于、移位等多组结果，再用一个 `case` 根据指令类型挑出那一路输出。这种「算全再选」的风格在面积上很省（一个加法器复用给 add/sub/slt），是 PicoRV32 小尺寸的来源之一。

> 阅读提醒：本讲全部代码都在 [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) 一个文件里。寄存器堆集中在 1300–1400 行附近，ALU 集中在 1220–1290 行附近，二者被 `ld_rs1/ld_rs2` 状态（1579–1803 行）串联起来。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
|------|------------|------|
| `picorv32.v` | 166–167 行：`regfile_size`/`regindex_bits` | 由参数推导寄存器堆容量与索引位宽 |
| `picorv32.v` | 51 行：`PICORV32_REGS` 宏 | 决定寄存器堆是「内联数组」还是「外部模块」 |
| `picorv32.v` | 203 行：`cpuregs` 数组 | 内联寄存器堆的存储实体 |
| `picorv32.v` | 1303–1398 行：`cpuregs_write` 与读写逻辑 | 写端口、双/单端口读端口 |
| `picorv32.v` | 1579–1803 行：`ld_rs1`/`ld_rs2` 状态 | 消费读端口、体现单/双端口差异 |
| `picorv32.v` | 2174–2190 行：`picorv32_regs` 模块 | 可选的外部寄存器堆示例实现 |
| `picorv32.v` | 1221–1290 行：ALU | `alu_add_sub` 等中间量与 `alu_out`/`alu_out_0` 选择 |
| `README.md` | 52–54、174–178 行 | 官方对双/单端口取舍的说明 |

## 4. 核心概念与源码讲解

### 4.1 寄存器堆的存储与读写端口

#### 4.1.1 概念说明

寄存器堆是 CPU 里最贴近指令的数据存储：RV32I 有 32 个 32 位通用寄存器 `x0..x31`，其中 `x0` 硬连线为 0（读永远得 0，写被丢弃）。PicoRV32 用一个 Verilog 数组 `cpuregs` 来实现它，并允许用参数在综合时改变它的**大小**（关掉 `x16..x31` 变成 RV32E；开启中断时额外加 4 个 `q` 寄存器）和**实现方式**（内联数组 vs. 外部模块）。

两个关键设计目标：

- **`x0` 恒为 0**：不靠复位初始化，而靠读写逻辑里的条件判断实现。
- **可替换实现**：通过宏 `PICORV32_REGS`，你能把寄存器堆换成 FPGA 上的专用存储资源（LUTRAM/BRAM）或 ASIC 的定制寄存器阵列。

#### 4.1.2 核心流程

一次寄存器事务的生命周期：

```
                (写端口：只在 cpu_state_fetch 生效)
   上一条指令执行完 ──► cpuregs_write = 1, cpuregs_wrdata = 结果
                          │
                          ▼
                   cpuregs[latched_rd] <= wrdata   （latched_rd != 0 才写）

                (读端口：在 ld_rs1 / ld_rs2 状态被消费)
   取指完成 ──► decoded_rs1 / decoded_rs2 (来自译码器)
                          │
                          ▼
              cpuregs_rs1 = cpuregs[rs1]   （rs1==0 则强制 0）
              cpuregs_rs2 = cpuregs[rs2]   （rs2==0 则强制 0）
                          │
                          ▼
                   reg_op1 / reg_op2（送入 ALU 或访存地址计算）
```

#### 4.1.3 源码精读

**容量与位宽由参数推导。** 寄存器堆有多少项、索引几位，不是写死的，而是综合时根据 `ENABLE_REGS_16_31`（是否保留 `x16..x31`）和 `ENABLE_IRQ*ENABLE_IRQ_QREGS`（是否加 4 个 `q` 寄存器）算出来的：

[picorv32.v:166-167](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L166-L167) 由 `ENABLE_REGS_16_31` 与 `ENABLE_IRQ_QREGS` 推导 `regfile_size`（数组项数）和 `regindex_bits`（索引位宽）。例如默认 RV32I + 中断 q 寄存器时，`regfile_size = 32 + 4 = 36`，`regindex_bits = 5 + 1 = 6`。

**默认走「内联数组」实现。** 文件顶部这行宏默认是注释掉的：

[picorv32.v:50-51](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L50-L51) `PICORV32_REGS` 宏被注释，意味着默认用下面这个内联 `cpuregs` 数组；只有当你想用定制存储资源时，才取消注释，把寄存器堆换成独立模块（见 4.1 末尾）。

于是寄存器堆就是一个最朴素的数组：

[picorv32.v:203-211](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L203-L211) 声明 `reg [31:0] cpuregs [0:regfile_size-1];`；若 `REGS_INIT_ZERO=1`，用 `initial` 块把所有项清零（仅用于仿真/形式化验证，综合时通常忽略）。

**写端口：只在 `cpu_state_fetch` 攒写请求。** `cpuregs_write` 与写数据 `cpuregs_wrdata` 是组合逻辑，只在 fetch 状态根据上一条指令的类型决定「写什么」：

[picorv32.v:1309-1334](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1309-L1334) `cpuregs_write` 默认 0；仅在 `cpu_state_fetch` 时，按分支/普通写回/中断入口几种情况给 `cpuregs_wrdata` 赋值并拉高 `cpuregs_write`。注意写地址不在这里——它来自上一条指令锁存的 `latched_rd`。

真正的写动作在时钟沿完成，并且用 `latched_rd` 做了 `x0` 保护：

[picorv32.v:1337-1346](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1337-L1346) `if (resetn && cpuregs_write && latched_rd)` 才写——`latched_rd` 为 0（即 `x0`）时整个条件为假，写动作被跳过。这就是「写 `x0` 被丢弃」的硬件实现。代码里的 `PICORV32_TESTBUG_001/002` 是故意写错地址/数据的测试用宏，正常综合不定义它们，走第 1344 行的常规写。

**读端口：`x0` 同样靠条件判断实现。** 双端口模式下两路读如下（单端口在 4.2 讲）：

[picorv32.v:1350-1357](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1350-L1357) `cpuregs_rs1 = decoded_rs1 ? cpuregs[decoded_rs1] : 0;`——当 `decoded_rs1 == 0`（即 `x0`），三元表达式走 `: 0` 分支，读出 0 而不是 `cpuregs[0]`。`cpuregs_rs2` 同理。这两行就是「读 `x0` 永远得 0」的全部实现。`RISCV_FORMAL_BLACKBOX_REGS` 分支用 `$anyseq` 把寄存器堆黑盒化，是给形式化验证用的，正常仿真/综合走 `cpuregs[...]` 那一路。

**可选的外部模块实现。** 如果你定义了 `PICORV32_REGS` 宏，寄存器堆就不再是内联数组，而是实例化一个独立模块：

[picorv32.v:1376-1385](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1376-L1385) 实例化 `\`PICORV32_REGS cpuregs (...)`，把 `wen/waddr/raddr1/raddr2/wdata` 接进去，读出 `rdata1/rdata2`。写使能 `wen = resetn && cpuregs_write && latched_rd`，把 `x0` 保护挪到了端口连接里。

配套的示例模块长这样：

[picorv32.v:2174-2190](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2174-L2190) `module picorv32_regs` 存 31 个字 `regs [0:30]`，用位反转地址 `~waddr[4:0]` 访问——这是引导综合工具推断分布式 RAM 的常见技巧。`x0` 不占真实槽位：读 `x0`（`raddr=0` → `~0=31`，落在数组外）读到的 `x` 会被外层 `decoded_rs ? rdata : 0` 屏蔽成 0，写 `x0` 因 `wen=0` 而无效。这是「算全再选」思想在存储层的体现：把边界情况交给使用者屏蔽，存储体本身尽量简单、对综合友好。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「`x0` 读出 0、写入被丢弃」的行为，并理解写端口只在 fetch 状态生效。

**操作步骤**：

1. 打开 [picorv32.v:1309-1346](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1309-L1346)，确认写条件 `cpuregs_write && latched_rd`。
2. 打开 [picorv32.v:1350-1357](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1350-L1357)，确认读条件 `decoded_rs1 ? ... : 0`。
3. 做一次思维实验：假设某条指令是 `add x0, x1, x2`（结果写回 `x0`）。回答：`latched_rd` 会是多少？写动作是否发生？`x0` 的值会改变吗？

**需要观察的现象 / 预期结果**：

- `latched_rd` = 0 → `cpuregs_write && latched_rd` = 0 → 不写 → `x0` 保持 0。
- 即使把 `cpuregs[0]` 强行改写（比如调试时），下一条读 `x0` 的指令仍会因 `decoded_rs ? ... : 0` 得到 0。
- 结论：`x0` 的恒零属性**不依赖**复位或初值，完全由读写逻辑的三元判断保证。

**待本地验证**：以上为源码推演。若想在仿真中观察，可在 `testbench_ez.v` 的 `memory[]` 里放一条 `addi x0, x0, 5`，再读 `x0`，预期读到 0（不会读到 5）。

#### 4.1.5 小练习与答案

**练习 1**：`ENABLE_REGS_16_31=0`、`ENABLE_IRQ=1`、`ENABLE_IRQ_QREGS=1` 时，`regfile_size` 和 `regindex_bits` 各是多少？

**答案**：`regfile_size = 16 + 4*1*1 = 20`；`regindex_bits = 4 + 1*1 = 5`。即 RV32E（16 个通用寄存器）加 4 个 q 寄存器，索引 5 位刚好够编址 20 项。

**练习 2**：为什么 `picorv32_regs` 示例模块只存 31 个字，而不是 32 个？

**答案**：因为 `x0` 永远读 0、永不写入，它不需要真实的存储槽。模块用位反转地址访问，`x0` 对应的地址落在新数组外，读到的值被外层 `decoded_rs ? rdata : 0` 屏蔽，省下一个存储字。

---

### 4.2 单端口 vs 双端口：为什么需要 `ld_rs2`

#### 4.2.1 概念说明

「双端口」「单端口」指的是寄存器堆的**读端口数**：

- **双端口（`ENABLE_REGS_DUALPORT=1`，默认）**：同一周期可同时读 `rs1` 和 `rs2` 两个寄存器。优点是 `add/sub/branch/store` 这类双源指令少一个周期；代价是面积略大。
- **单端口（`ENABLE_REGS_DUALPORT=0`）**：只有一个读端口，一个周期只能读一个寄存器。双源指令需要分两周期读，状态机多走一个 `cpu_state_ld_rs2`；优点是核心更小（在 ASIC 标准单元库上尤其明显）。

README 对此有一句精炼总结：

[picorv32.v README:52-54](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L52-L54)（在 README 中）「dual-port 性能更好，single-port 核心更小」。并提醒：在许多 FPGA 上，寄存器堆本就用双端口存储资源实现，关掉双端口不一定能进一步缩小核心。

#### 4.2.2 核心流程

关键在于「读端口数 ↔ 状态机周期数」的耦合。用一个 `decoded_rs` 多路选择器来复用唯一的读端口：

```
双端口（2 个读端口）：
  ld_rs1:  同时读 rs1, rs2  ──►  reg_op1, reg_op2 都备齐  ──►  直接进 exec/stmem/shift

单端口（1 个读端口）：
  ld_rs1:  decoded_rs = rs1, 读 rs1 ──► reg_op1 备齐 ──► 进 ld_rs2
  ld_rs2:  decoded_rs = rs2, 读 rs2 ──► reg_op2 备齐 ──► 再进 exec/stmem/shift
```

注意：**只有双源指令**才需要读两次。立即数指令（`addi/slti/...`）、`lui/auipc/jal`、立即数移位等只用 `rs1` 一个源（另一个操作数是立即数 `decoded_imm`），它们在单端口下也不走 `ld_rs2`——这是状态机里精心安排的分流。

#### 4.2.3 源码精读

**单端口的读端口复用。** 当 `ENABLE_REGS_DUALPORT=0` 时，读逻辑变成下面这段：

[picorv32.v:1358-1366](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1358-L1366) 关键一行是 `decoded_rs = (cpu_state == cpu_state_ld_rs2) ? decoded_rs2 : decoded_rs1;`——同一个读端口，在 `ld_rs1` 状态读 `rs1`、在 `ld_rs2` 状态读 `rs2`，靠当前状态切换地址。然后把读出值同时赋给 `cpuregs_rs1` 和 `cpuregs_rs2`（`cpuregs_rs2 = cpuregs_rs1`），让下游逻辑不必关心现在是哪个状态在读。

**状态机里的分流。** 在 `ld_rs1` 的 `default` 分支（处理 `add/sub/and/or/xor/slt`、分支、store 等双源指令），双/单端口的差异一目了然：

[picorv32.v:1724-1755](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1724-L1755) 先 `reg_op1 <= cpuregs_rs1`（读第一个源）；随后 `if (ENABLE_REGS_DUALPORT)` 分支里**同时** `reg_op2 <= cpuregs_rs2` 并立刻转入 `stmem/shift/exec`；`else` 分支则只 `cpu_state <= cpu_state_ld_rs2`，把第二个源的读取推迟到下一周期。这段 `if/else` 就是「双端口省一个周期」的全部秘密。

**`ld_rs2` 状态：单端口专属。** 这个状态在双端口模式下基本不被触发：

[picorv32.v:1759-1764](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1759-L1764) `ld_rs2` 里 `reg_sh <= cpuregs_rs2; reg_op2 <= cpuregs_rs2;`——此刻 `decoded_rs` 已被切到 `decoded_rs2`，读端口读出的是第二个源操作数。补齐 `reg_op2` 后，状态机才转入 `exec/stmem/shift`。

形式化验证里有一处断言印证了这个分工：

[picorv32.v:2129](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2129) `if (cpu_state == cpu_state_ld_rs2) ok = !ENABLE_REGS_DUALPORT;`——它声明「处于 `ld_rs2` 状态是合法的，当且仅当不是双端口配置」，从侧面确认双端口永远不会进入 `ld_rs2`。

#### 4.2.4 代码实践

**实践目标**：通过改一个参数，把默认的双端口核变成单端口核，验证功能不变、并理解周期差异。

**操作步骤**：

1. 复制最小测试台：`cp testbench_ez.v testbench_ez_sp.v`。
2. 把第 47–48 行的实例化从 `picorv32 #(` 改为 `picorv32 #(.ENABLE_REGS_DUALPORT(0))`（这是**示例修改**，请在你自己的副本上改，不要动原文件）。
3. 仿照 [Makefile:69-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L70) 的命令编译运行：
   `iverilog -o testbench_ez_sp.vvp testbench_ez_sp.v picorv32.v && vvp testbench_ez_sp.vvp`。
4. 对比 `make test_ez`（双端口）的输出与新输出。

**需要观察的现象 / 预期结果**：

- 两次打印的 `write 0x3fc ...` 数值序列应当**完全一致**——单端口不改变指令语义，只改变执行周期数。
- `testbench_ez` 只在 `mem_valid && mem_ready` 那一拍打印，而 `ld_rs2` 不产生访存，所以**周期差异在这个测试台里看不出来**。

**待本地验证**：要直接看到「单端口每条双源指令多 1 周期」，需要开 `ENABLE_TRACE` 并接 `trace_valid/trace_data`（如 `testbench.v` + `make test_vcd`），用 `showtrace.py` 数每条指令的拍数——这超出 `testbench_ez` 的能力，留作可选进阶验证。

#### 4.2.5 小练习与答案

**练习 1**：`addi x1, x2, 5` 在单端口模式下会进入 `ld_rs2` 状态吗？为什么？

**答案**：不会。`addi` 只用 `rs1`（`x2`）一个源，第二操作数是立即数 `decoded_imm=5`。它在 `ld_rs1` 里命中 `is_jalr_addi_slti_sltiu_xori_ori_andi` 分支（[1712 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1712)），直接读 `rs1`、把立即数送进 `reg_op2` 后转入 `exec`，无需读第二个寄存器。

**练习 2**：为什么 README 说在 FPGA 上关掉双端口「不一定能进一步缩小核心」？

**答案**：许多 FPGA 的 LUT RAM / 块 RAM 本身就是双端口存储资源。即使你写单端口逻辑，综合工具映射上去的物理资源仍是一个双端口 RAM 的一部分，空闲的第二端口不会变省。只有用 ASIC 标准单元或单端口存储宏时，单端口才真正省面积。

---

### 4.3 ALU：加减、比较与逻辑运算的选择

#### 4.3.1 概念说明

PicoRV32 的 ALU 是一段**组合逻辑**，输入是两个操作数 `reg_op1`、`reg_op2` 和一组指令译码信号（`instr_add/instr_sub/instr_beq/...`），输出是 32 位结果 `alu_out`（用于运算类指令）和 1 位结果 `alu_out_0`（用于比较/分支指令）。

它的设计哲学是「**先并行算全，再按指令选一个**」：

- 一个加减法器 `alu_add_sub` 同时服务 `add/addi/jal/jalr/lui/auipc`（加）和 `sub/slt/sltu`（减）。
- 一个相等比较 `alu_eq` 服务 `beq/bne`。
- 两个大小比较 `alu_lts`（有符号）/`alu_ltu`（无符号）服务 `slt/sltu/blt/bge/bltu/bgeu`。

复用一个加法器/比较器给多条指令，是 PicoRV32 面积小的重要手段。

#### 4.3.2 核心流程

ALU 分两层：

```
第一层：并行算出 6 个「中间结果」（恒定计算，不看具体指令）
  alu_add_sub = sub ? op1 - op2 : op1 + op2
  alu_eq      = (op1 == op2)
  alu_lts     = $signed(op1) < $signed(op2)     // 有符号小于
  alu_ltu     = (op1 < op2)                      // 无符号小于
  alu_shl     = op1 << op2[4:0]                  // 仅 BARREL_SHIFTER 用
  alu_shr     = ($signed) op1 >>> op2[4:0]       // 仅 BARREL_SHIFTER 用

第二层：两个 case 选出最终输出
  alu_out_0 (1位，比较/分支结果): beq→eq, bne→!eq, blt→lts, bge→!lts, ...
  alu_out  (32位，运算结果):      add族→add_sub, 比较族→alu_out_0, xor/or/and, 移位
```

关于有符号比较的原理：`alu_lts` 用 `$signed` 把两个 32 位数当作二进制补码有符号数比较，等价于比较它们的代数值。设两数最高位（符号位）为 \(s_1, s_2\)，则：

\[
\text{alu\_lts} \iff \text{int32}(op1) < \text{int32}(op2)
\]

而 `alu_ltu` 直接按无符号数值比较。于是 6 条分支指令可以由 `eq/lts/ltu` 三个量组合出来：

| 指令 | 条件 | 用到的中间量 |
|------|------|------------|
| `beq` | 相等 | `alu_eq` |
| `bne` | 不等 | `!alu_eq` |
| `blt` | 有符号小于 | `alu_lts` |
| `bge` | 有符号大于等于 | `!alu_lts` |
| `bltu` | 无符号小于 | `alu_ltu` |
| `bgeu` | 无符号大于等于 | `!alu_ltu` |

#### 4.3.3 源码精读

**第一层：并行算出中间结果。** 这 6 个量用 `generate` 根据 `TWO_CYCLE_ALU` 选择是组合逻辑还是寄存一拍：

[picorv32.v:1229-1247](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1229-L1247) `TWO_CYCLE_ALU=0`（默认）时是 `always @*` 组合逻辑；`=1` 时是 `always @(posedge clk)` 寄存一拍，牺牲一个周期换取更高的 fmax（关键路径从「读寄存器→ALU」缩短为「读寄存器」）。注意 `alu_add_sub` 用 `instr_sub` 选择加减——同一个加法器复用给 add/sub。`alu_shr` 的 `{instr_sra||instr_srai ? op1[31]:1'b0, op1}` 是在最高位前拼一个符号位，配合 `>>>` 算术右移实现 `sra/srai`（算术移位）。

**第二层之一：比较/分支结果 `alu_out_0`。**

[picorv32.v:1249-1265](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1249-L1265) 用 `case (1'b1)`（one-hot 选择）按指令挑出 1 位结果：`beq→alu_eq`、`bne→!alu_eq`、`bge→!alu_lts`、`bgeu→!alu_ltu`、`slt/slti/blt→alu_lts`、`sltu/sltiu/bltu→alu_ltu`。`(* parallel_case, full_case *)` 是给综合工具的提示：各分支互斥且完备，便于优化。行 1261–1264 里 `(!TWO_CYCLE_COMPARE || !{...})` 的逻辑是为了在两周期比较模式下避免组合冒险，把某些分支的比较推迟一拍。

**第二层之二：运算结果 `alu_out`。**

[picorv32.v:1267-1284](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1267-L1284) 同样是 `case (1'b1)`：`add/addi/sub/jal/jalr/lui/auipc` 一族选 `alu_add_sub`；`slt/sltu/slti/sltiu` 选 `alu_out_0`（即上一步算出的 1 位比较值，零扩展到 32 位）；`xor/xori`、`or/ori`、`and/andi` 直接用 `reg_op1 ^|& reg_op2`；最后两条移位分支被 `BARREL_SHIFTER` 门控——只有开启桶形移位器时，移位才在 ALU 里单周期完成，否则移位走独立的 `cpu_state_shift` 迭代状态机（见下一讲 u5-l2）。`RISCV_FORMAL_BLACKBOX_ALU` 分支用 `$anyseq` 把 ALU 黑盒化，供形式化验证使用。

**消费 ALU 结果：`exec` 状态。** 算出的 `alu_out` 在 `cpu_state_exec` 被消费：

[picorv32.v:1805-1827](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1805-L1827) 分支指令（`is_beq_bne_blt_bge_bltu_bgeu`）把 `alu_out_0`（或两周期模式下的 `alu_out_0_q`）存入 `latched_branch` 决定是否跳转；其余运算指令设 `latched_stalu=1`，让 fetch 状态把 `alu_out_q` 写回 `rd`（见 [1321 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1321) `latched_stalu ? alu_out_q : reg_out`）。

**两周期模式的寄存器。** 当 `TWO_CYCLE_ALU` 或 `TWO_CYCLE_COMPARE` 打开时，结果晚一拍出来，于是在每个时钟沿把组合结果锁存一拍：

[picorv32.v:1410-1411](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1410-L1411) `alu_out_q <= alu_out; alu_out_0_q <= alu_out_0;`——这两个 `_q` 寄存器就是给两周期模式用的延迟版本，配合 `exec` 状态里的 `alu_wait` 握手。

#### 4.3.4 代码实践

**实践目标**：追踪一条具体指令的 ALU 选择路径，亲手把「32 位编码 → 中间量 → `alu_out`」的映射走一遍。

**操作步骤**：

1. 取指令 `sub x5, x6, x7`（编码 `0x40738333`：opcode=0110011, funct3=000, funct7=0100000, rd=5, rs1=6, rs2=7）。
2. 在 [picorv32.v:1229-1247](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1229-L1247) 确认：因 `instr_sub=1`，`alu_add_sub = reg_op1 - reg_op2 = x6 - x7`。
3. 在 [picorv32.v:1267-1284](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1267-L1284) 确认：`sub` 命中 `is_lui_auipc_jal_jalr_addi_add_sub`，故 `alu_out = alu_add_sub = x6 - x7`。
4. 在 [picorv32.v:1805-1827](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1805-L1827) 确认：非分支，走 `else` 设 `latched_stalu=1`；回到 fetch 后由 [1321 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1321) 把 `alu_out_q` 写回 `x5`。

**需要观察的现象 / 预期结果**：

- `sub` 复用了加法器（`alu_add_sub` 的减法路），没有独立的减法器。
- 若把同一条改成 `slt x5, x6, x7`（funct3=010, funct7=0000000），则 `alu_add_sub` 仍是减（`instr_sub` 由 slt 触发），但 `alu_out` 改走 `is_compare → alu_out_0 → alu_lts`，结果是 0 或 1。

**待本地验证**：以上为源码推演。可在 `testbench_ez.v` 的 `memory[]` 里放 `sub` 与 `slt` 两条指令，把结果 `sw` 到内存观察打印值，确认 `slt` 结果确为 0/1。

#### 4.3.5 小练习与答案

**练习 1**：`alu_add_sub` 同时被 `add` 和 `sub` 使用。`instr_sub` 这个信号除了 `sub`，还被哪些指令置位？

**答案**：从 [1240 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1240) `alu_add_sub = instr_sub ? reg_op1 - reg_op2 : reg_op1 + reg_op2` 看，凡是用减法的指令都该置 `instr_sub`。这包括 `sub` 本身，以及需要比较的 `slt/slti/sltu/sltiu`（它们的最终结果取自 `alu_lts/alu_ltu` 而非 `alu_add_sub`，但比较器 `$signed(a)<$signed(b)` 是独立硬件，并不真的读 `alu_add_sub`——所以 `instr_sub` 对 slt 族其实不影响最终结果，这是 PicoRV32 里一处可读性 > 必要性的设计）。

**练习 2**：为什么 `alu_out` 的移位分支要用 `BARREL_SHIFTER &&` 门控？

**答案**：默认 `BARREL_SHIFTER=0`，移位不走 ALU，而是由 `cpu_state_shift` 状态机迭代完成（两级移位，见下一讲）。只有显式开启桶形移位器，移位才在 ALU 单周期算出，此时 `alu_shl/alu_shr` 才被 `alu_out` 选中。门控避免了两套移位电路同时生效。

**练习 3**：`TWO_CYCLE_ALU=1` 时，`alu_add_sub` 从组合逻辑变成寄存器。这对 `exec` 状态有什么影响？

**答案**：结果晚一拍才稳定，`exec` 不能在同一拍取 `alu_out`。代码用 `alu_wait` 标志让 `exec` 多停留一拍，并改读延迟版 `alu_out_q`（[1410 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1410)），用多一个周期换取更高的时钟频率。

## 5. 综合实践

把本讲三块知识串起来，追踪一条双源运算指令 `add x5, x6, x7` 从取指到回写的完整数据通路，并对比双/单端口的周期差异。

**任务**：

1. **画数据通路图**。在一张图上标出：`cpuregs`（含 2 个读端口、1 个写端口）、`reg_op1/reg_op2` 锁存、`alu_add_sub` 加减法器、`alu_out` 选择器、`latched_rd` 写地址。把 `add` 指令的数据流用箭头连起来：`cpuregs[x6]→reg_op1`、`cpuregs[x7]→reg_op2`、`alu_add_sub(reg_op1+reg_op2)→alu_out→alu_out_q→cpuregs[x5]`。
2. **画双端口时序**。列出状态序列 `fetch → ld_rs1 → exec → fetch`，在每个状态旁标注：「ld_rs1 同时读 x6、x7（双端口）」「exec 算 alu_out=add_sub」「fetch 把 alu_out_q 写回 x5」。
3. **画单端口时序**。列出状态序列 `fetch → ld_rs1 → ld_rs2 → exec → fetch`，标注：「ld_rs1 读 x6（decoded_rs=rs1）」「ld_rs2 读 x7（decoded_rs=rs2）」「exec 算 alu_out」「fetch 写回」。指出多出的那个 `ld_rs2` 周期就是单端口的性能代价。
4. **验证你的图**。对照源码：双端口读在 [1350-1357 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1350-L1357)，单端口读在 [1358-1366 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1358-L1366)，分流在 [1724-1755 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1724-L1755)，ALU 选择在 [1267-1284 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1267-L1284)，写回在 [1321 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1321)。

**预期结果**：你应得到两张状态序列图，唯一区别是单端口多一个 `ld_rs2` 状态；以及一张数据通路图，清晰展示「寄存器堆双读 → ALU 算全选一 → 回写」的闭环。

## 6. 本讲小结

- 寄存器堆默认是内联数组 `cpuregs[0:regfile_size-1]`，其容量与索引位宽由 `ENABLE_REGS_16_31`、`ENABLE_IRQ_QREGS` 推导（[166-167 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L166-L167)）；也可通过 `PICORV32_REGS` 宏替换为外部模块（[2174-2190 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2174-L2190)）。
- `x0` 恒为 0 不靠复位，靠读写逻辑的三元判断：读时 `decoded_rs ? cpuregs[rs] : 0`，写时 `cpuregs_write && latched_rd`（[1338 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1338)、[1352 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1352)）。
- 写端口只在 `cpu_state_fetch` 生效（[1313 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1313)），是上一讲「fetch 是唯一回写点」的硬件兑现。
- 双端口（默认）同周期读 `rs1+rs2`；单端口用一个 `decoded_rs` 多路选择器复用唯一读端口，双源指令多走 `cpu_state_ld_rs2`（[1358-1366 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1358-L1366)、[1753-1754 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1753-L1754)）。
- ALU 采用「并行算 6 个中间量、再两个 case 选一个」的风格，一个加减法器复用给 add/sub，比较结果由 `alu_eq/alu_lts/alu_ltu` 组合（[1229-1284 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1229-L1284)）。
- `TWO_CYCLE_ALU` / `TWO_CYCLE_COMPARE` 可把 ALU/比较寄存一拍换取 fmax，配套用 `alu_out_q/alu_out_0_q` 与 `alu_wait` 处理延迟（[1410-1411 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1410-L1411)）。

## 7. 下一步学习建议

本讲把 ALU 里被 `BARREL_SHIFTER` 门控的两条移位分支暂时搁置了——默认配置下移位并不在 ALU 单周期完成。下一讲 **u5-l2 移位运算：两阶段移位与桶形移位器** 会专门讲 `cpu_state_shift` 状态机：PicoRV32 如何用「先移 4 位、再移 1 位」的两级算法（`TWO_STAGE_SHIFT`）在没有桶形移位器时迭代完成任意位移，以及开启 `BARREL_SHIFTER` 后移位如何并入 ALU 单周期完成。建议阅读 [picorv32.v:1829-1852](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1829-L1852) 的 `cpu_state_shift` 状态作为预习。
