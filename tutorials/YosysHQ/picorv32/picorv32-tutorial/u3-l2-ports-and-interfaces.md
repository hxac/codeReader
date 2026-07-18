# 端口与四大接口

## 1. 本讲目标

上一讲（u3-l1）我们已经学会用 `parameter` 在综合期「裁剪」CPU：开哪些功能、用哪种实现、给什么复位初值。但 `parameter` 只是 CPU 的「内部配置」，CPU 要真正放进一个系统里工作，还必须和外界打交道——取指令、读写数据、接协处理器、响应中断、输出调试信息，这些都通过**模块端口（port）**完成。

本讲学完后你应当能够：

1. 看懂 `picorv32` 模块端口列表里每一根信号的方向（input/output）、位宽与含义。
2. 说清**原生内存接口** `mem_valid`/`mem_ready`/`mem_wstrb`/`mem_rdata` 的 valid-ready 握手规则与字节写使能编码。
3. 区分**原生接口**与 **Look-Ahead 前瞻接口**、理解 PCPI 协处理器接口与 IRQ 中断接口的握手时序。
4. 知道 `trace` 与 `rvfi` 两组「只读可观测」端口在什么编译开关下才存在。
5. 画出 PicoRV32 的外部接口方框图，标出每根信号的方向与作用。

> 本讲只看 CPU 的「外观」（端口契约），**不**深入端口内部的微架构实现——那分别是 u4（译码与状态机）、u5（数据通路与内存状态机）、u6（PCPI 与 IRQ 内部机制）的主题。本讲建立的是「CPU 当黑盒时，外面该如何接线」的认知。

## 2. 前置知识

### 2.1 什么是模块端口

Verilog 模块用 `module ... (端口列表)` 声明它与外界的连接点。每个端口有：

- **方向**：`input`（输入）、`output`（输出）、`inout`（双向）。PicoRV32 几乎不用 `inout`。
- **位宽**：如 `[31:0]` 表示 32 位，`[3:0]` 表示 4 位，省略不写则是 1 位。
- **是否寄存器化**：`output reg` 表示该输出在 `always @(posedge clk)` 块里被赋值（即寄存到触发器，下个时钟沿才更新）；`output`（无 `reg`）则通常是 `assign` 驱动的组合逻辑（当前周期就反映输入变化）。

### 2.2 valid-ready 握手

PicoRV32 的内存接口是一种最简单的 valid-ready 握手：主设备（CPU）拉高 `valid` 表示「我有一次事务要做」；从设备（内存）准备好后拉高 `ready` 表示「我做完了」。在 `valid` 拉高到 `ready` 拉高之间，主设备输出的地址、数据、控制信号**保持稳定不变**。事务在 `valid && ready` 同时为高的那一拍完成。

### 2.3 同步复位

PicoRV32 全部时序逻辑块都写成 `always @(posedge clk)`，内部用 `if (!resetn)` 判断复位。这意味着复位是**同步的**：`resetn` 必须在时钟上升沿处为 0，寄存器才被复位。`resetn` 是低有效（名字里的 `n` = active-low）。

### 2.4 接口与协议的关系

「接口（interface）」指一组协同工作的端口集合；「协议（protocol）」指这组端口上信号变化的时序约定。本讲的「四大接口」分别是：内存接口（含 Look-Ahead）、PCPI 协处理器接口、IRQ 中断接口、调试/可观测接口（trace + rvfi）。它们各自有独立的握手协议。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `picorv32.v` | CPU 主体。模块端口列表集中在前 160 行，是本讲的主要精读对象。 |
| `README.md` | 官方对内存接口、Look-Ahead、PCPI、IRQ 的协议描述，是端口语义的权威说明。 |
| `testbench.v` | 用 `picorv32_wrapper` 把端口接到 AXI 内存模型上，是「端口怎么接线」的真实范例。 |

本讲所有永久链接基于当前 HEAD `87c89acc18994c8cf9a2311e871818e87d304568`。

## 4. 核心概念与源码讲解

PicoRV32 的端口列表整体在 [picorv32.v:89-160](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L89-L160)（`) (` 之后到 `);` 之前）。它前面紧跟的 `#(...)` 是上一讲讲过的参数块 [picorv32.v:62-89](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62-L89)。参数决定「内部电路长什么样」，端口决定「对外长什么样」。下面按六大类逐一拆解。

### 4.1 时钟、复位与 trap 输出

#### 4.1.1 概念说明

任何同步 CPU 都先要有时钟和复位。PicoRV32 用**单一时钟 `clk`** 和**低有效同步复位 `resetn`**。此外还有一根特殊的输出 `trap`：它为 1 表示 CPU 进入了**不可恢复的死状态**（halted），只有重新复位才能退出。`trap` 在自检测试台里常被当作「仿真应当结束」的信号，也供外部系统监测 CPU 是否已挂死。

#### 4.1.2 核心流程

- 复位期间 `resetn == 0`：所有时序块在每个上升沿把内部寄存器置为初值（如 `reg_pc <= PROGADDR_RESET`、`irq_mask <= ~0`）。
- 复位释放后 `resetn == 1`：CPU 从 `PROGADDR_RESET` 取第一条指令，正常执行。
- 若运行中遇到**不可恢复**条件（例如关闭了 `CATCH_MISALIGN`/`CATCH_ILLINSN` 时发生未对齐访问或非法指令，详见 u3-l1），状态机进入 `cpu_state_trap`，`trap` 被拉高并**永久保持**，直到下一次复位。

注意 `trap` 与中断（IRQ）不同：中断是「可恢复」的，处理完会返回；`trap` 是「CPU 已经无路可走」的终态。

#### 4.1.3 源码精读

端口声明里时钟、复位、trap 三根线非常简洁：

```verilog
input clk, resetn,
output reg trap,
```

完整位置见 [picorv32.v:90-91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L90-L91)。`clk` 与 `resetn` 同为 1 位 `input` 写在一行；`trap` 是 `output reg`，说明它在时序块里赋值。

`trap` 的赋值逻辑：主时序块默认每拍把它清 0（[picorv32.v:1403](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1403)），只有当状态机处在 `cpu_state_trap` 时才置 1：

```verilog
cpu_state_trap: begin
    trap <= 1;
end
```

见 [picorv32.v:1487-1489](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1487-L1489)。`cpu_state_trap` 是状态编码 `8'b10000000`，定义在 [picorv32.v:1172](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1172)。一旦进入这个状态，case 里没有把它迁出的分支，于是它**自锁**，`trap` 一直为 1。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `trap` 被拉高。

**操作步骤**：

1. 打开 `testbench_ez.v`（u1-l3 用过的最小测试台），找到它在 `trap` 拉高时通常会如何结束仿真。
2. 把内置 `memory[]` 里的某条合法指令改成一个 CPU 不支持、且 `CATCH_ILLINSN` 关闭时会陷入的编码——更简单的做法是：直接确认 `testbench_ez` 实例化 `picorv32` 时 `CATCH_ILLINSN` 的默认值（默认为 1，即走非法指令中断而非 trap）。
3. 若想强制触发 trap：把实例化参数改为 `.CATCH_ILLINSN(0)`，并把 `memory[0]` 改成一条全 0 的非法指令（`0x00000000` 不是合法 RV32I 指令）。
4. 重新 `make test_ez`。

**需要观察的现象**：仿真在很早的时刻停止，`trap` 信号波形从 0 跳到 1 并保持。

**预期结果**：因为 `cpu_state_trap` 自锁，`trap` 一旦置 1 就不再回 0，除非复位。

> 说明：本实践的具体终止行为取决于测试台对 `trap` 的处理；若不确定 `testbench_ez.v` 是否监听 `trap`，请先阅读该文件——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`resetn` 是高有效还是低有效？复位是同步还是异步？

**答案**：低有效（active-low，名字带 `n`）；同步复位——所有时序块都写作 `always @(posedge clk)` + `if (!resetn)`，没有 `negedge resetn` 进敏感列表。

**练习 2**：`trap` 拉高后，CPU 不复位的情况下还能自己恢复吗？

**答案**：不能。`cpu_state_trap` 在状态机的 case 里没有任何迁出分支，是自锁状态；`trap` 会一直保持，只能靠重新拉低 `resetn` 来退出。

**练习 3**：为什么 `trap` 声明成 `output reg` 而不是 `output`（组合输出）？

**答案**：因为 `trap` 反映的是 CPU **状态机所处的状态**（是否在 `cpu_state_trap`），这是一种需要被寄存器记住的时序信息，所以在 `always @(posedge clk)` 里赋值，声明为 `reg`。

---

### 4.2 原生内存接口（Native Memory Interface）

#### 4.2.1 概念说明

CPU 取指令、读写数据，都要走内存接口。PicoRV32 的**原生内存接口**是一个一次只做一笔事务的 valid-ready 接口，冯·诺依曼式（指令与数据共用同一组端口）。它是 `picorv32` 这一个变体的「直连」接口；`picorv32_axi` 和 `picorv32_wb` 是在它外面再套一层适配器换成 AXI4-Lite / Wishbone（见 u7-l1）。也就是说：**无论最终对外是哪种总线，CPU 内部都在用这同一组原生接口。**

#### 4.2.2 核心流程

一次内存事务的时序（读或写都适用）：

1. CPU 把地址放 `mem_addr`、写数据放 `mem_wdata`、字节写使能放 `mem_wstrb`、是否取指放 `mem_instr`，然后拉高 `mem_valid`。
2. 在 `mem_valid` 保持高期间，以上所有输出**保持稳定**。
3. 从设备完成事务后拉高 `mem_ready` 一拍。
4. 当 `mem_valid && mem_ready` 同为高的那一拍：
   - 若是**读**事务，从设备须在这一拍把读出数据放到 `mem_rdata`；
   - 若是**写**事务，从设备在这一拍把 `mem_wdata` 按 `mem_wstrb` 写入。
5. 事务完成，CPU 撤下 `mem_valid`。

读事务的两种合法实现：异步读（`mem_ready` 与 `mem_valid` 同拍高，组合给出 `mem_rdata`）或把 `mem_ready` 直接常接 1。写事务同理。所以最简单的从设备就是「永远 ready」的组合 RAM。

#### 4.2.3 源码精读

端口声明 [picorv32.v:93-100](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L93-L100)：

```verilog
output reg        mem_valid,   // 我要做一次事务
output reg        mem_instr,   // 1=取指, 0=数据
input             mem_ready,   // 从设备完成

output reg [31:0] mem_addr,    // 字节地址
output reg [31:0] mem_wdata,   // 写数据
output reg [ 3:0] mem_wstrb,   // 字节写使能（4 位）
input      [31:0] mem_rdata,   // 读数据
```

方向表：

| 信号 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `mem_valid` | output reg | 1 | 事务有效 |
| `mem_instr` | output reg | 1 | 本次是取指（区别于数据访存） |
| `mem_ready` | input | 1 | 从设备应答 |
| `mem_addr` | output reg | 32 | 字节地址 |
| `mem_wdata` | output reg | 32 | 待写入数据 |
| `mem_wstrb` | output reg | 4 | 4 个字节的写使能 |
| `mem_rdata` | input | 32 | 读出数据 |

**`mem_instr` 的意义**：它让从设备（或调试者）能区分这次访问是取指还是数据读写。u1-l3 里测试台就是靠这一位在 `$display` 时区分打印 `ifetch`/`read`/`write`。它的赋值见 [picorv32.v:585](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L585)（取指时为 1）和 [picorv32.v:591](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L591)（数据访问时清 0）。

**`mem_wstrb` 的合法取值**：README 明确只有 8 种——`0000`（不写）、`1111`（写 32 位）、`1100`（高半字）、`0011`（低半字）、`1000/0100/0010/0001`（单字节）。它的值由 `mem_wordsize`（字/半字/字节）与地址低位组合产生，见 [picorv32.v:401-428](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L401-L428)，例如字节写时：

```verilog
2: begin  // mem_wordsize == 2：字节访问
    mem_la_wdata = {4{reg_op2[7:0]}};          // 把字节复制到 4 个字节位
    mem_la_wstrb = 4'b0001 << reg_op1[1:0];    // 按地址低 2 位选中某一字节
```

这段同时算出了 Look-Ahead 的 `mem_la_wstrb`/`mem_la_wdata`，原生接口的 `mem_wstrb`/`mem_wdata` 在下一拍从它们拷过来（见 [picorv32.v:576-579](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L576-L579)）：

```verilog
mem_wstrb <= mem_la_wstrb & {4{mem_la_write}};  // 读事务时强制 0000
...
mem_wdata <= mem_la_wdata;
```

注意 `& {4{mem_la_write}}`：读事务时把写使能屏蔽为 0，正好对应「读时 `mem_wstrb` 为 0」的约定。

#### 4.2.4 代码实践

**实践目标**：用一个最简双口 RAM 当从设备，跑通 valid-ready 握手。

**操作步骤**：

1. 阅读本讲端口方向表，再读 `testbench_ez.v` 里那块 `always @(posedge clk)` 的内存模型（u1-l3 已分析），确认它如何用 `mem_valid && mem_ready` 命中、如何按 `mem_wstrb` 选择性写入。
2. 自己写一段示意性的「永远 ready」从设备（**示例代码，非项目原有**）：

```verilog
// 示例代码：最简组合 RAM，mem_ready 恒为 1
assign mem_ready = 1'b1;
always @(posedge clk) begin
    if (mem_valid && mem_wstrb[0]) memory[mem_addr>>2][ 7: 0] <= mem_wdata[ 7: 0];
    if (mem_valid && mem_wstrb[1]) memory[mem_addr>>2][15: 8] <= mem_wdata[15: 8];
    if (mem_valid && mem_wstrb[2]) memory[mem_addr>>2][23:16] <= mem_wdata[23:16];
    if (mem_valid && mem_wstrb[3]) memory[mem_addr>>2][31:24] <= mem_wdata[31:24];
end
assign mem_rdata = memory[mem_addr>>2];  // 组合读
```

3. 对照 `testbench_ez.v` 检查：你的模型与官方测试台在「`mem_ready` 恒 1」和「按 `mem_wstrb` 逐字节写」上是否一致。

**需要观察的现象**：取指（`mem_instr==1`）读出的 32 位字正是程序里下一条要执行的指令；写数据时只有 `mem_wstrb` 为 1 的字节被改写。

**预期结果**：CPU 能连续取指并自检通过——这正说明你的从设备正确遵循了原生内存接口协议。

#### 4.2.5 小练习与答案

**练习 1**：为什么读事务时 `mem_wstrb` 必须是 `0000`？

**答案**：`mem_wstrb` 是「写使能」，全 0 表示「不写任何字节」，即纯读。CPU 在 [picorv32.v:576](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L576) 用 `& {4{mem_la_write}}` 在读事务时把它屏蔽为 0。

**练习 2**：`mem_rdata` 是 input 还是 output？它在哪一拍必须有效？

**答案**：input。在 `mem_valid && mem_ready` 同为高的那一拍必须给出读出值（异步读可组合给，同步读可提前一拍给）。

**练习 3**：`mem_addr` 是字节地址还是字地址？

**答案**：字节地址（32 位）。从设备内部若按字存取，要自己 `>>2` 换算成字索引——`testbench_ez.v` 正是这么做的。

---

### 4.3 Look-Ahead 前瞻内存接口

#### 4.3.1 概念说明

原生接口在 `mem_valid` 拉高**那一拍**才给出地址/数据。但有些高性能场景（比如要让外部异步 RAM、或者要把地址提前送进流水化的总线）希望**提前一拍**就知道「下一次事务是什么」。Look-Ahead 接口就是干这个的：它在 `mem_valid` 拉高的**前一拍**，用脉冲 `mem_la_read`/`mem_la_write` 预告下一拍的事务，并同步给出地址、数据、写使能。

它和原生接口描述的是**同一次事务**，只是早一拍露面。所以二者要么都用、要么都对照——不能给出互相矛盾的值（代码里有形式化断言保证二者一致，见 [picorv32.v:2159-2160](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2159-L2160)）。

#### 4.3.2 核心流程

- 在 `mem_valid` 即将拉高的**前一拍**：
  - 若下笔是读，拉高 `mem_la_read` 一拍；
  - 若下笔是写，拉高 `mem_la_write` 一拍；
  - 同时给出 `mem_la_addr`、`mem_la_wdata`、`mem_la_wstrb`。
- 下一拍，原生接口的 `mem_valid` 拉高，`mem_addr`/`mem_wdata`/`mem_wstrb` 跟上（值与 Look-Ahead 一致）。

代价：`mem_la_read`/`mem_la_write`/`mem_la_addr` 是**组合逻辑**直接驱动（`assign`），不经过寄存器，所以它们到外的路径更短、更难做时序收敛——README 明确这一点。

#### 4.3.3 源码精读

端口声明 [picorv32.v:102-107](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L102-L107)：

```verilog
// Look-Ahead Interface
output            mem_la_read,    // assign 驱动
output            mem_la_write,   // assign 驱动
output     [31:0] mem_la_addr,    // assign 驱动
output reg [31:0] mem_la_wdata,   // 组合 always @* 赋值
output reg [ 3:0] mem_la_wstrb,
```

注意前三个是 `output`（无 `reg`），由 `assign` 直接组合驱动 [picorv32.v:379-382](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L379-L382)：

```verilog
assign mem_la_write = resetn && !mem_state && mem_do_wdata;
assign mem_la_read  = resetn && ((!mem_la_use_prefetched_high_word && !mem_state &&
                                  (mem_do_rinst || mem_do_prefetch || mem_do_rdata)) || ...);
assign mem_la_addr  = (mem_do_prefetch || mem_do_rinst) ?
                      {next_pc[31:2] + mem_la_firstword_xfer, 2'b00} :
                      {reg_op1[31:2], 2'b00};
```

`mem_la_wdata`/`mem_la_wstrb` 虽声明为 `reg`，却在 `always @*`（纯组合）块里赋值，见 [picorv32.v:401-428](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L401-L428)——所以本质上也是组合输出。这里的 `reg` 只是 Verilog 语法要求（`always @*` 里被赋值的左值必须是 `reg` 类型），不代表物理寄存器。

原生接口与 Look-Ahead 的「一致性」由形式化断言守护（[picorv32.v:2144-2160](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2144-L2160)）：把 Look-Ahead 的值缓存一拍（`last_mem_la_wdata`/`last_mem_la_wstrb`），再断言下一拍的 `mem_wdata`/`mem_wstrb` 与之相等。

#### 4.3.4 代码实践

**实践目标**：直观对比「Look-Ahead 早一拍」与「原生接口」的波形关系。

**操作步骤**：

1. 用 `make test_vcd`（或直接给 iverilog 加 `+vcd`）生成一段波形（这步在 u8-l2 会详细讲，这里先用）。
2. 用 GTKWave 打开，把 `clk`、`mem_valid`、`mem_addr`、`mem_la_read`、`mem_la_write`、`mem_la_addr` 一起拖出来。
3. 找一个 `mem_valid` 由 0 变 1 的上升沿。

**需要观察的现象**：在 `mem_valid` 变高的**前一拍**，`mem_la_read` 或 `mem_la_write` 有一个单拍脉冲，且那一拍 `mem_la_addr` 的值等于下一拍 `mem_addr` 的值。

**预期结果**：Look-Ahead 严格比原生接口早一拍揭示事务信息；`mem_la_read`/`mem_la_write` 是互斥的单拍脉冲。

> 说明：波形生成的具体命令与参数详见 u8-l2；若暂无 iverilog/GTKWave 环境，可仅阅读 [picorv32.v:379-382](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L379-L382) 与 [picorv32.v:576-579](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L576-L579) 在源码层面确认「提前一拍」关系——**待本地验证**波形。

#### 4.3.5 小练习与答案

**练习 1**：Look-Ahead 接口相对原生接口「早」多少？

**答案**：早一个时钟周期（一拍）。它在 `mem_valid` 拉高的前一拍给出脉冲与地址。

**练习 2**：为什么 README 说 Look-Ahead「更难做时序收敛」？

**答案**：因为 `mem_la_read`/`mem_la_write`/`mem_la_addr` 是 `assign` 组合驱动，信号从 CPU 内部组合逻辑直达外部引脚，没有寄存器打断路径，关键路径更长；原生接口则把同样的值寄存了一拍再输出。

**练习 3**：既然 Look-Ahead 更难收敛，为什么还要提供它？

**答案**：它让外部能在 `mem_valid` 拉高前就准备好（例如异步 RAM 提前译码地址、或把地址提前送入下一级总线），整体上可以把外部路径也做短、提高系统 fmax。README 指出不用 Look-Ahead 时 Dhrystone 性能会从 0.516 掉到 0.305 DMIPS/MHz。

---

### 4.4 PCPI 协处理器接口

#### 4.4.1 概念说明

Pico Co-Processor Interface（PCPI）让外部逻辑替 CPU 执行它自己不认识的「非分支」指令——最典型的就是 M 扩展的乘除法。当 CPU 译码出一条不支持的指令，它不会立刻陷入，而是把这条指令「外包」给 PCPI：把指令编码和两个源操作数送出去，等协处理器算完把结果送回来。PicoRV32 自带 `picorv32_pcpi_mul`/`picorv32_pcpi_fast_mul`/`picorv32_pcpi_div` 三个内置协处理器（也是用这套接口接进来的），所以 PCPI 既是「对外扩展点」也是「内部乘除法的接法」。

只有当上一讲的 `WITH_PCPI`（即 `ENABLE_PCPI || ENABLE_MUL || ENABLE_FAST_MUL || ENABLE_DIV`，见 [picorv32.v:169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L169)）为真时，这些端口才有意义。

#### 4.4.2 核心流程

PCPI 的一次握手：

1. CPU 遇到不认识的指令，拉高 `pcpi_valid`，同时：
   - 把 32 位指令字放 `pcpi_insn`；
   - 把 `rs1` 字段的寄存器值放 `pcpi_rs1`；
   - 把 `rs2` 字段的寄存器值放 `pcpi_rs2`。
2. 协处理器译码 `pcpi_insn`：
   - 如果这条不是它能处理的，它什么都不做（不响应）；
   - 如果是它能处理的，就开始算。若算的时间较长，应**尽早**拉高 `pcpi_wait`，防止 CPU 超时判定为非法指令。
3. 算完后，协处理器拉高一拍 `pcpi_ready`；若要把结果写回寄存器，同时拉高 `pcpi_wr` 并把结果放 `pcpi_rd`。
4. CPU 收到 `pcpi_ready`：若 `pcpi_wr` 有效，按指令的 `rd` 字段把 `pcpi_rd` 写入对应寄存器，本条指令完成。
5. **超时**：如果 16 个时钟周期内没有任何协处理器拉高 `pcpi_ready` 或 `pcpi_wait`，CPU 抛出「非法指令」异常（走 IRQ 或 trap，取决于 `CATCH_ILLINSN`）。

#### 4.4.3 源码精读

端口声明 [picorv32.v:109-117](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L109-L117)：

```verilog
// Pico Co-Processor Interface (PCPI)
output reg        pcpi_valid,   // 派发一条指令给协处理器
output reg [31:0] pcpi_insn,    // 指令字本身
output     [31:0] pcpi_rs1,     // rs1 寄存器值
output     [31:0] pcpi_rs2,     // rs2 寄存器值
input             pcpi_wr,      // 协处理器要写回
input      [31:0] pcpi_rd,      // 写回的值
input             pcpi_wait,    // 协处理器还在算，别超时
input             pcpi_ready,   // 协处理器算完了
```

方向表：

| 信号 | 方向 | 含义 |
| --- | --- | --- |
| `pcpi_valid` | output reg | CPU 发起协处理器请求 |
| `pcpi_insn` | output reg | 待执行指令的 32 位编码 |
| `pcpi_rs1`/`pcpi_rs2` | output（组合） | 两个源操作数 |
| `pcpi_wr` | input | 协处理器要求写回寄存器 |
| `pcpi_rd` | input | 写回值 |
| `pcpi_wait` | input | 协处理器占线（防超时） |
| `pcpi_ready` | input | 协处理器完成 |

注意 `pcpi_rs1`/`pcpi_rs2` 是 `assign` 组合输出，直接来自内部操作数寄存器（[picorv32.v:191-192](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L191-L192)）：

```verilog
assign pcpi_rs1 = reg_op1;
assign pcpi_rs2 = reg_op2;
```

内置协处理器与外部接口的「多选一」：当同时存在多个协处理器（如外部 PCPI + 内部 MUL + 内部 DIV）时，CPU 用一个组合 mux 把它们的 `wr/rd/wait/ready` 合并，见 [picorv32.v:325-346](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L325-L346)。`wait` 与 `ready` 用按位或（任何一个协处理器拉高即生效），`wr/rd` 用 `case` 优先选出 `ready` 的那个。

**16 周期超时**计数器见 [picorv32.v:1423-1430](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1423-L1430)：

```verilog
if (WITH_PCPI && CATCH_ILLINSN) begin
    if (resetn && pcpi_valid && !pcpi_int_wait) begin
        if (pcpi_timeout_counter)
            pcpi_timeout_counter <= pcpi_timeout_counter - 1;
    end else
        pcpi_timeout_counter <= ~0;       // 复位计数器到全 1
    pcpi_timeout <= !pcpi_timeout_counter; // 数到 0 触发超时
end
```

只要协处理器拉高 `pcpi_wait`，`pcpi_int_wait` 即为真，计数器被重填满，不会超时——这就是「长时间运算的协处理器必须尽早拉 `pcpi_wait`」的原因。

#### 4.4.4 代码实践

**实践目标**：读懂一个最小 PCPI 协处理器，建立「怎么自己写一个」的直觉。

**操作步骤**：

1. 阅读 `picorv32.v` 中内置的 `picorv32_pcpi_mul` 模块（搜索 `module picorv32_pcpi_mul`）。重点关注它如何：
   - 在 `pcpi_valid` 时译码 `pcpi_insn`，判断是不是 `MUL` 类指令；
   - 计算完成后拉高 `pcpi_ready`（必要时配合 `pcpi_wait`）；
   - 通过 `pcpi_wr` + `pcpi_rd` 把结果送回。
2. 在纸上画一个最小 PCPI 协处理器的状态机：空闲态 → 收到 `pcpi_valid` 且指令匹配 →（可选）拉 `pcpi_wait` → 算完拉 `pcpi_ready`+`pcpi_wr`+`pcpi_rd` → 回空闲。

**需要观察的现象**：协处理器在 `pcpi_valid` 拉高后若干拍才拉 `pcpi_ready`；如果它耗时超过 16 拍且没拉 `pcpi_wait`，CPU 端会出现非法指令异常。

**预期结果**：你能说出 `pcpi_wait` 与 `pcpi_ready` 的分工——前者「续命防超时」，后者「宣告完成」。

> 说明：亲手实现一个自定义 PCPI 协处理器（如 popcount）是 u6-l1 的实践任务；本讲只需读懂内置 `picorv32_pcpi_mul` 的端口用法。

#### 4.4.5 小练习与答案

**练习 1**：`pcpi_rs1`/`pcpi_rs2` 上的值是寄存器**编号**还是寄存器**内容**？

**答案**：是寄存器**内容**（值）。CPU 已替你把 `rs1`/`rs2` 字段指向的寄存器读出来放好了，协处理器不用自己去查寄存器堆。

**练习 2**：一个协处理器要花 30 拍才算完，它必须做什么？

**答案**：在译码成功后**立刻**拉高 `pcpi_wait` 并保持到 `pcpi_ready` 拉高前。否则 CPU 在第 16 拍就会因 `pcpi_timeout` 判定为非法指令。

**练习 3**：`pcpi_wr` 和 `pcpi_ready` 必须同时拉高吗？

**答案**：`pcpi_ready` 必须拉高表示完成；`pcpi_wr` 是可选的——若该指令不需要写回寄存器（比如某条「副作用」指令），协处理器可以只拉 `pcpi_ready` 而不拉 `pcpi_wr`。

---

### 4.5 IRQ 中断接口

#### 4.5.1 概念说明

PicoRV32 内置一个 32 路中断控制器，用一对端口与外界交互：32 位 `irq` 输入汇集外部中断源，32 位 `eoi`（End Of Interrupt）输出告诉外界「这些中断正在被处理」。注意 PicoRV32 的中断机制**不遵循** RISC-V 特权架构规范，而是用一组自定义指令（getq/setq/retirq/maskirq/waitirq/timer）实现极简中断——那是 u6-l2 的主题。本讲只看端口层面：`irq` 怎么进、`eoi` 怎么出。

只有当 `ENABLE_IRQ` 开启时，中断相关端口才有实际行为（否则 IRQ 子系统不存在，见 u3-l1）。

#### 4.5.2 核心流程

- 外部设备把想要触发的中断位拉高在 `irq[31:0]` 上。其中 bit0/1/2 有内置含义：
  - bit0 = 定时器中断（`irq_timer`）
  - bit1 = EBREAK/ECALL 或非法指令（`irq_ebreak`）
  - bit2 = 总线错误/未对齐访问（`irq_buserror`）
  - bit3..31 可由外部源触发（也可来自 PCPI 协处理器）。
- CPU 在合适的时机（通常是一条指令执行完、准备取下一条之前）检查 `irq & ~irq_mask`（未被屏蔽的中断）。若有 pending 且未屏蔽的中断，跳到 `PROGADDR_IRQ` 执行中断处理程序。
- 进入处理程序时，CPU 在 `eoi` 上拉高「本次处理的那些中断位」，告诉外界「我正在处理它们」。
- 中断处理程序返回（`retirq`）时，`eoi` 拉低。

`irq_mask` 的复位初值由软件（`maskirq` 指令）管理，复位时为 `~0`（全部屏蔽）。`MASKED_IRQ` 参数可在硬件层永久屏蔽某些位。

#### 4.5.3 源码精读

端口声明 [picorv32.v:119-121](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L119-L121)：

```verilog
// IRQ Interface
input      [31:0] irq,      // 32 路外部中断请求
output reg [31:0] eoi,      // 正在被处理的中断位（应答）
```

方向表：

| 信号 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `irq` | input | 32 | 外部中断源（高=有请求） |
| `eoi` | output reg | 32 | CPU 正在处理的中断位 |

内置中断源编号定义见 [picorv32.v:161-163](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L161-L163)：

```verilog
localparam integer irq_timer     = 0;
localparam integer irq_ebreak    = 1;
localparam integer irq_buserror  = 2;
```

复位时 `eoi <= 0`（[picorv32.v:1476](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1476)）。进入中断处理时，CPU 把「正在处理的中断」放到 `eoi`（[picorv32.v:1512](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1512)）：

```verilog
ENABLE_IRQ && irq_state[1]: begin
    eoi <= irq_pending & ~irq_mask;        // 拉高正在处理的中断位
    next_irq_pending = next_irq_pending & irq_mask;
end
```

中断返回时 `eoi <= 0`（[picorv32.v:1668](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1668)）。所以 `eoi` 的生命周期与「一次中断处理」对齐：从进入处理程序到返回之间为高。

#### 4.5.4 代码实践

**实践目标**：搞清 `irq`/`eoi` 与软件中断屏蔽 `irq_mask` 的协作。

**操作步骤**：

1. 阅读 `testbench.v` 里 `picorv32_wrapper` 是如何把 `irq` 接到外部的（[testbench.v:197](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L197) 处的 `.irq(irq)`），确认 `irq` 在测试台里是常 0 还是有驱动。
2. 跟踪一次软件写屏蔽的过程：固件用 `maskirq` 自定义指令设置 `irq_mask`（详见 u6-l2），理解「写 `irq_mask` 后，对应 bit 即使在 `irq` 上为高也不会被处理」。
3. 在源码里找到 CPU 判定「是否有未屏蔽 pending 中断」的表达式 `irq_pending & ~irq_mask`（如 [picorv32.v:1512](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1512)、[picorv32.v:1538](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1538)）。

**需要观察的现象**：`eoi` 在中断处理期间持续为高，且其置位的 bit 恰好是被处理的中断位；处理返回后 `eoi` 归零。

**预期结果**：你能用一句话说清 `irq`（请求）、`irq_mask`（屏蔽）、`eoi`（应答）三者的分工。

#### 4.5.5 小练习与答案

**练习 1**：`irq` 的 bit0、bit1、bit2 各代表什么？

**答案**：bit0 = 定时器中断；bit1 = EBREAK/ECALL 或非法指令；bit2 = 总线错误（未对齐访问）。见 [picorv32.v:161-163](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L161-L163)。

**练习 2**：`eoi` 何时为高、何时为低？

**答案**：CPU 进入中断处理程序时把正在处理的中断位拉高到 `eoi`；中断处理返回（`retirq`）时把 `eoi` 清 0。外部设备可据此知道自己的中断是否正在被服务。

**练习 3**：复位后 `irq_mask` 的初值是什么？意味着什么？

**答案**：`irq_mask <= ~0`（全 1，即全部中断被屏蔽）。意味着复位后默认**不响应任何中断**，必须由固件用 `maskirq` 指令主动开闸。

---

### 4.6 调试与可观测接口：trace 与 rvfi

#### 4.6.1 概念说明

前面四组接口都参与 CPU 的「功能行为」；最后两组是**纯可观测**端口——它们不影响 CPU 执行，只是把内部发生的事「广播」出来供调试和验证。PicoRV32 提供两种：

- **trace 接口**（`trace_valid`/`trace_data`）：每执行完一条指令（或发生分支/中断），打一拍 `trace_valid` 并把这条指令的摘要编码到 `trace_data` 上。配合仓库里的 `showtrace.py` 可以解码成可读的执行轨迹。
- **rvfi 接口**（RISC-V Formal Interface）：一组只在 `` `ifdef RISCV_FORMAL `` 下才编译的端口，遵循 RISC-V 形式化验证标准（riscv-formal），用于用 SMT 求解器证明 CPU 的正确性（u8-l3 主题）。

`trace` 由参数 `ENABLE_TRACE` 控制；`rvfi` 由 Verilog 宏 `RISCV_FORMAL` 控制（注意一个是 `parameter`、一个是宏，区别见 u1-l2）。

#### 4.6.2 核心流程

- **trace**：当 `ENABLE_TRACE=1` 且某条指令产生了可记录事件（普通执行/分支/中断）时，CPU 在合适的拍拉高 `trace_valid` 一拍，同时 `trace_data` 给出 36 位编码。`trace_data` 高 4 位是类型标签（`TRACE_BRANCH`/`TRACE_ADDR`/`TRACE_IRQ`），低 32 位是数据（目标地址或写回值等）。
- **rvfi**：每条「退役」指令在 `rvfi_valid` 拉高一拍，同时给出该指令的完整信息——指令字、PC、读/写的寄存器号与值、内存访问掩码与数据、以及 `rvfi_order` 全局序号。形式化验证工具据此检查 ISA 一致性。

#### 4.6.3 源码精读

trace 端口声明 [picorv32.v:157-159](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L157-L159)：

```verilog
// Trace Interface
output reg        trace_valid,
output reg [35:0] trace_data
```

trace 类型标签定义见 [picorv32.v:171-173](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L171-L173)：

```verilog
localparam [35:0] TRACE_BRANCH = {4'b 0001, 32'b 0};
localparam [35:0] TRACE_ADDR   = {4'b 0010, 32'b 0};
localparam [35:0] TRACE_IRQ    = {4'b 1000, 32'b 0};
```

trace 赋值示例（分支与普通写回）见 [picorv32.v:1517-1524](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1517-L1524)：

```verilog
if (ENABLE_TRACE && latched_trace) begin
    latched_trace <= 0;
    trace_valid <= 1;
    if (latched_branch)
        trace_data <= (irq_active ? TRACE_IRQ : 0) | TRACE_BRANCH | (current_pc & 32'hfffffffe);
    else
        trace_data <= (irq_active ? TRACE_IRQ : 0) | (latched_stalu ? alu_out_q : reg_out);
end
```

跳转类指令（JAL/JALR）的 trace 还会带上目标地址，见 [picorv32.v:1866-1867](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1866-L1867)。

rvfi 端口声明在 `` `ifdef RISCV_FORMAL `` 宏保护下，见 [picorv32.v:123-155](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L123-L155)，包含 `rvfi_valid`、`rvfi_order`（64 位全局序号）、`rvfi_insn`、`rvfi_pc_rdata`/`rvfi_pc_wdata`、`rvfi_rs1_*`/`rvfi_rs2_*`/`rvfi_rd_*`、`rvfi_mem_*` 以及两组 CSR（`mcycle`/`minstret`）的读/写掩码与数据。典型一行：

```verilog
output reg        rvfi_valid,        // 本拍有指令退役
output reg [63:0] rvfi_order,        // 指令全局序号
output reg [31:0] rvfi_insn,         // 该指令的编码
...
output reg [31:0] rvfi_pc_rdata,     // 该指令的 PC
output reg [31:0] rvfi_pc_wdata,     // 下一条指令的 PC
```

`rvfi_trap` 直接复用 `trap` 信号（[picorv32.v:1991](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1991)：`rvfi_trap <= trap;`）。

方向表（节选）：

| 信号 | 方向 | 含义 |
| --- | --- | --- |
| `trace_valid`/`trace_data` | output reg | 执行轨迹脉冲与 36 位编码 |
| `rvfi_valid` | output reg | 指令退役指示（仅 `RISCV_FORMAL`） |
| `rvfi_order` | output reg [63:0] | 指令全局序号 |
| `rvfi_insn` | output reg [31:0] | 退役指令编码 |
| `rvfi_pc_rdata`/`rvfi_pc_wdata` | output reg [31:0] | 当前/下一 PC |
| `rvfi_rd_addr`/`rvfi_rd_wdata` | output reg | 写回寄存器号与值 |
| `rvfi_mem_*` | output reg | 该指令的内存访问掩码与数据 |

#### 4.6.4 代码实践

**实践目标**：用 `showtrace.py` 把 trace 端口的输出解码成可读轨迹。

**操作步骤**：

1. 确认 `testbench.v` 里 `picorv32_wrapper` 把 `ENABLE_TRACE` 设为 1、并把 `trace_valid`/`trace_data` 接出（[testbench.v:219-220](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L219-L220) 处）。
2. 运行 `make test_vcd` 生成 `testbench.trace` 文件（详见 u8-l2）。
3. 运行 `python3 showtrace.py testbench.trace firmware/firmware.elf`。

**需要观察的现象**：解码输出按执行顺序列出每条指令的 PC 与汇编，可与 `riscv32-objdump -d firmware/firmware.elf` 的反汇编对照。

**预期结果**：trace 序列与固件实际执行流一致；分支处 `trace_data` 的高 4 位标签会切换为 `TRACE_BRANCH`/`TRACE_ADDR`。

> 说明：`make test_vcd` 的具体规则见根 `Makefile` 与 u8-l2；若当前环境无 RISC-V 工具链生成 `firmware.elf`，则 `showtrace.py` 缺少符号信息但仍可解码原始 trace——**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：`trace` 和 `rvfi` 各自由什么控制是否存在？

**答案**：`trace` 由参数 `ENABLE_TRACE` 控制（综合期 `parameter`）；`rvfi` 由 Verilog 宏 `RISCV_FORMAL` 控制（编译期 `` `ifdef ``）。两者都不影响 CPU 功能行为，是纯可观测端口。

**练习 2**：`trace_data` 是 36 位，高 4 位的作用是什么？

**答案**：高 4 位是类型标签（`TRACE_BRANCH`=0001、`TRACE_ADDR`=0010、`TRACE_IRQ`=1000），告诉 `showtrace.py` 低 32 位该当作「分支目标地址」「普通写回值」还是「中断」来解读。

**练习 3**：为什么 rvfi 这么多端口（rs1/rs2/rd/mem/CSR……）？

**答案**：因为形式化验证要证明「每一条退役指令都符合 RISC-V ISA 规范」，需要把该指令的**全部**输入输出（读了哪些寄存器、写了哪个寄存器什么值、访问了内存哪里、改了哪些 CSR）都暴露给外部检查器。这是 riscv-formal 标准的接口要求。

---

## 5. 综合实践

把本讲所有接口串起来，画一张完整的 **PicoRV32 外部接口方框图**。要求：

1. 画一个方框代表 `picorv32` 模块。
2. 按六大类把端口分组，**每根信号标出方向（箭头朝向 CPU 为 input，朝外为 output）与位宽**：
   - 时钟/复位/trap：`clk`(in,1)、`resetn`(in,1)、`trap`(out,1)。
   - 原生内存：`mem_valid`/`mem_instr`(out,1)、`mem_ready`(in,1)、`mem_addr`/`mem_wdata`(out,32)、`mem_wstrb`(out,4)、`mem_rdata`(in,32)。
   - Look-Ahead：`mem_la_read`/`mem_la_write`(out,1)、`mem_la_addr`(out,32)、`mem_la_wdata`(out,32)、`mem_la_wstrb`(out,4)。
   - PCPI：`pcpi_valid`(out,1)、`pcpi_insn`(out,32)、`pcpi_rs1`/`pcpi_rs2`(out,32)、`pcpi_wr`(in,1)、`pcpi_rd`(in,32)、`pcpi_wait`/`pcpi_ready`(in,1)。
   - IRQ：`irq`(in,32)、`eoi`(out,32)。
   - trace/rvfi：`trace_valid`(out,1)、`trace_data`(out,36)；rvfi 一组（条件存在）。
3. 在方框外围画三个「对端」：内存子系统（接原生/Look-Ahead）、协处理器（接 PCPI）、外设/中断源（接 IRQ）；时钟源接 clk/resetn；调试主机接 trace/rvfi。
4. 标注：哪些端口是 `output reg`（寄存器化、下拍更新），哪些是 `assign`（组合、当拍反映）。对照本讲各小节的源码精读核对。
5. 用一句话总结每类接口的握手关键点（如内存接口的 `valid && ready` 完成点、PCPI 的 16 拍超时、IRQ 的 `eoi` 生命周期）。

完成后，你应当能看着这张图，把 PicoRV32 当黑盒正确地接进任意系统——这正是后续 u5（数据通路与内存状态机）、u6（PCPI/IRQ 内部机制）、u7（AXI/Wishbone 适配）将要打开的「黑盒内部」。

## 6. 本讲小结

- PicoRV32 的端口分六大类：时钟/复位/trap、原生内存、Look-Ahead 前瞻、PCPI 协处理器、IRQ 中断、trace/rvfi 可观测。
- 时钟为单 `clk`，复位 `resetn` 低有效且**同步**；`trap` 是 CPU 进入 `cpu_state_trap` 后永久保持的「死锁」指示。
- 原生内存接口是 valid-ready 握手（`mem_valid`/`mem_ready`），用 `mem_wstrb` 4 位做字节写使能（仅 8 种合法值），`mem_instr` 区分取指与数据。
- Look-Ahead 把同一事务**提前一拍**用 `mem_la_*` 暴露，是组合输出，利于系统提速但更难做时序收敛。
- PCPI 用 `pcpi_valid/insn/rs1/rs2` 派发、`pcpi_ready/wr/rd` 回收，配 `pcpi_wait` 防 16 拍超时。
- IRQ 用 32 位 `irq` 输入和 32 位 `eoi` 应答；bit0/1/2 为内置源，复位后 `irq_mask` 全 1（默认全屏蔽）。
- `trace`（参数 `ENABLE_TRACE`）与 `rvfi`（宏 `RISCV_FORMAL`）是纯可观测端口，不影响功能。

## 7. 下一步学习建议

- 想看「端口内部的内存事务是怎么驱动出来的」→ 进入 u5-l3《原生内存接口与传输状态机》，精读 `mem_state` 状态机与 look-ahead 的产生细节。
- 想自己写一个 PCPI 协处理器 → u6-l1《PCPI 协处理器接口》会带你实现一个自定义指令核。
- 想搞清 IRQ 自定义指令与软件中断栈 → u6-l2《IRQ 与自定义中断指令》。
- 想把原生接口桥接到 AXI4-Lite / Wishbone → u7-l1《AXI4-Lite 与 Wishbone 适配》。
- 建议同时重读 `testbench.v` 的 `picorv32_wrapper`（[testbench.v:163-221](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L163-L221)），它是本讲所有端口的一次性「接线范例」，对照方框图阅读效果最好。
