# 单端口与双端口 RAM 推断

## 1. 本讲目标

本讲进入「片上存储器」单元。读完本讲，你应当能够：

- 说清楚 FPGA 上「推断（inference）一块 RAM」是什么意思，以及它和「实例化厂商原语」的区别。
- 读懂 `RAM_Single_Port` 的端口、存储数组与初始化逻辑，并解释它为什么**没有**同步清零输出。
- 抓住本讲的核心机关：**同一个 always 块，只把 `<=` 换成 `=`，就让综合器推断出「读旧值」与「读新值（写转发）」两种完全不同的硬件**——并能从 Verilog 调度语义层面解释为什么。
- 区分三种 RAM 形态（单端口 1RW、Simple 双端口 1W1R、True 双端口 2WR），并能为一个寄存器堆（register file）做出合理选型。

## 2. 前置知识

本讲建立在 u6-l1（`Register` 家族）之上。请先回忆两件事：

1. **一个时钟 always 块 + 非阻塞赋值 `<=` 就是一个寄存器**。把「一个寄存器」推广成「一整组寄存器」，就得到了存储器（memory）——本讲的 `reg [WORD_WIDTH-1:0] ram [DEPTH-1:0]` 正是 `DEPTH` 个宽度为 `WORD_WIDTH` 的寄存器排成一排。
2. u3-l1 讲过**阻塞 `=` 与非阻塞 `<=` 的根本区别**：阻塞「立即生效，下一行可见」，非阻塞「先采样、时间步末统一写入」。本讲会把这个区别用到极致——它直接决定综合器推断出哪一种 BRAM 行为。

再补充三个 FPGA 存储相关的常识（本讲会用到）：

- **推断（inference）vs 实例化（instantiation）**：推断是指你用行为级 Verilog（数组 + 时钟块）描述存储器的读写行为，让综合器（CAD 工具）自己识别出这个模式，映射到专用硬件资源；实例化则是直接调用厂商提供的底层原语（如 Xilinx 的 `RAMB36`、Intel 的 `altsyncram`）。本书一律用推断，好处是可移植、可读、让 CAD 自由优化。
- **三种片上存储资源**（从贵到便宜、从小到大）：触发器（flip-flop，1 位/个，最贵）、**分布式/LUT RAM**（Intel 叫 MLAB，小而快，同址读写一般返回**旧值**）、**块 RAM / BRAM**（Intel 叫 M10K/M20K，Xilinx 叫 RAMB36 等，大容量专用存储块）。
- **BRAM 的读是「寄存输出」的**：也就是说，BRAM 给出的读数据天然延迟 1 拍。所以本讲三个模块的 `read_data` 都是 `output reg`——这正是为了匹配 BRAM 的寄存输出特性，让综合器愿意把它推断成 BRAM 而不是触发器堆。

> 术语提示：下文反复出现的 RDW（read-during-write，读-写-同址）指「同一时钟沿、对同一地址又读又写」这一边界情形。它是本讲一切讨论的焦点。

## 3. 本讲源码地图

| 文件 | 作用 | 关键看点 |
| --- | --- | --- |
| `RAM_Single_Port.v` | 单端口 RAM（1 个读/写口，1RW） | `READ_NEW_DATA` 参数 + 阻塞/非阻塞推断两种行为；存储数组与初始化 |
| `RAM_Simple_Dual_Port.v` | 简单双端口 RAM（1 写口 + 1 读口，1W1R） | 分离的读写地址；作者称其为「配合写转发时最快的 BRAM 配置」 |
| `RAM_True_Dual_Port.v` | 真双端口 RAM（两个读/写口，2WR） | A/B 两口分置两个 always 块以表达「无优先级」，贴合底层 BRAM |
| `RAM_generate_empty_init_file.py` | 生成空白内存初始化文件的工具 | 三个模块都引用它来配合 `$readmemh` |

这三个 RAM 模块在 `index.html` 里都属于 **Memory** 分类（[index.html:L166-L177](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L166-L177)）。同分类下还有「双时钟版 Simple Dual-Port」「多端口 LE/复制/LVT/XOR」等，其中多端口存储器留到下一讲 u7-l2。

## 4. 核心概念与源码讲解

### 4.1 单端口 RAM：把「一个寄存器」扩成「一组寄存器」

#### 4.1.1 概念说明

单端口 RAM 只有一组地址/数据线，读和写**共用同一个地址端口**，每个时钟周期要么写、要么读（或同址又读又写）。它是三种 RAM 里端口最少、带宽最低，但也最省资源的一种。把它放在最前面，是因为它的结构最干净，能让我们先把「存储数组 + 寄存输出读 + 初始化」这套骨架看清，再在 4.2 里专攻写转发。

#### 4.1.2 核心流程

一次访问的流程：

1. 时钟上升沿到来。
2. 若 `wren == 1`，把 `write_data` 写入 `ram[addr]`。
3. 无论是否写，都把 `ram[addr]` 读到 `read_data`（寄存输出，延迟 1 拍）。
4. 是否「同址又读又写时返回新值还是旧值」——由 `READ_NEW_DATA` 决定，详见 4.2。

容量关系很简单：总位数 = `DEPTH` × `WORD_WIDTH`。

#### 4.1.3 源码精读

模块头部参数与端口：[RAM_Single_Port.v:L51-L69](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L51-L69)。注意几个本书惯例：所有参数默认 `0`/空串（u1-l2 讲过的「吵闹失败」栅栏，忘设参数就会让 `[WORD_WIDTH-1:0]` 退化为非法的 `[-1:0]`）；`read_data` 是 `output reg`，体现 BRAM 的寄存输出读。`addr` 只有一个，读写共用。

`read_data` 用 `initial` 初始化为零（[RAM_Single_Port.v:L71-L73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L71-L73)），呼应 u2-l1「reg 输出端口不能在声明处初始化、必须紧跟 initial」的规矩。

存储数组本体与厂商属性：[RAM_Single_Port.v:L78-L84](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L78-L84)。`(* ramstyle = RAMSTYLE *)`（Quartus）、`(* ram_style = RAMSTYLE *)`（Vivado）、`(* rw_addr_collision = RW_ADDR_COLLISION *)`（Vivado）三个属性贴在数组声明上，把实现风格（用 BRAM 还是 LUT RAM、是否写转发）的偏好告诉综合器——这正是 u4-l2 讲过的「属性随声明走、随实例化自动生效」。

> 设计取舍：作者明确说 `read_data` **不给同步清零**（[RAM_Single_Port.v:L4-L8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L4-L8)）。因为在 Quartus 里，驱动输出的寄存器一旦带 clear 就**不能被重定时**（retime），也不够可移植。想要清零输出，请在下游另接一个 `Annuller`（u5-l1）。

初始化有两种方式，由 `USE_INIT_FILE` 选择（[RAM_Single_Port.v:L146-L160](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L146-L160)）：不用文件时，用一个 `initial` 里的 `for` 循环把每个 `ram[i]` 设成 `INIT_VALUE`（综合器据此生成内存初始化文件，适合用 `RAMSTYLE="logic"` 实现的小型寄存器集合）；用文件时则 `$readmemh(INIT_FILE, ram)`。文件格式是「每行一个十六进制值，从 0 到 `DEPTH-1`」，可用 `RAM_generate_empty_init_file.py` 生成。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认单端口 RAM 的「单一地址端口」与「每拍都读」两点。
2. **步骤**：打开 [RAM_Single_Port.v:L107-L129](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L107-L129) 的 generate 块。注意：`read_data <= ram[addr]` 这一行**在 `if(wren)` 之外**，且模块**没有 `rden` 端口**。
3. **观察**：得出结论——单端口 RAM 每个时钟沿都会更新 `read_data`，写只是顺带发生；它只有一个 `addr`，读写无法指向不同地址。
4. **预期结果**：你能向别人解释「为什么单端口 RAM 不能在同一拍读 A 地址、写 B 地址」——因为物理上只有一个地址口。

#### 4.1.5 小练习与答案

- **练习**：`RAM_Single_Port` 想把整块内存初始化为 `8'h5A`、深度 256、字宽 8，应该怎么实例化？
  **答案**：设 `.WORD_WIDTH(8)、.DEPTH(256)、.ADDR_WIDTH(8)、.USE_INIT_FILE(0)、.INIT_VALUE(8'h5A)`，其余默认即可。`INIT_VALUE` 会被 for 循环写进每个 `ram[i]`。
- **练习**：为什么不直接给 `read_data` 加一个 `clear` 端口？
  **答案**：作者注释指出，带 clear 的输出寄存器在 Quartus 里不可重定时、可移植性差；本书选择把清零交给下游 `Annuller`，让 RAM 本身保持「纯净」以便综合器自由优化。

---

### 4.2 写转发：`READ_NEW_DATA` 与阻塞/非阻塞赋值的玄机

这是本讲的重头戏。同一个时钟 always 块，**唯一**的差别是把 `<=` 换成 `=`，综合器就推断出两种不同的硬件。

#### 4.2.1 概念说明

「写转发（write forwarding）」指的是：当同一时钟沿对同一地址又读又写（RDW）时，读口直接返回**刚写入的新值**，而不是内存里原来的旧值。

- **读旧值（`READ_NEW_DATA = 0`，无写转发）**：RDW 时返回内存里的**旧**值。这种行为天然契合 **LUT/分布式 RAM**（如 Intel MLAB）。
- **读新值（`READ_NEW_DATA = 1`，有写转发）**：RDW 时返回**新**值，综合器会在 BRAM 周围**推断出一圈写转发逻辑**。这种行为契合专用 **BRAM**（如 Intel M10K）。

为什么写转发还和「速度」有关？作者解释（[RAM_Single_Port.v:L24-L32](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L24-L32)）：有了写转发逻辑，那个「被同址写污染」的读结果会在输出选择器处被丢弃、换成写值；否则 BRAM 只能「一个沿写、另一个沿读」，周期更长，频率上不去。所以 Simple Dual-Port 配合写转发往往是**最快的 BRAM 配置**。

#### 4.2.2 核心流程

关键在 Verilog 的调度语义（u3-l1）：

- **非阻塞 `<=`（读旧值）**：时钟沿到达时，**先**对所有 RHS 用「沿之前的旧值」采样，**再**在时间步末统一写入。于是：
  ```
  ram[addr] <= write_data;   // 安排：本次沿后 ram[addr] 变为 write_data
  read_data <= ram[addr];    // 采样的是 ram[addr] 的【旧值】
  ```
  两条都基于沿前状态采样，`read_data` 拿到的是**旧值**。

- **阻塞 `=`（读新值）**：按语句顺序逐条执行、每条立即生效：
  ```
  ram[addr] = write_data;    // 立刻把 ram[addr] 改成 write_data
  read_data = ram[addr];     // 此时再读 ram[addr]，已是【新值】
  ```
  于是 `read_data` 拿到**新值**——这就是写转发。

用一张时序图把同址 RDW（addr=2，原值为 `00`，写入 `AA`）的差别画出来：

```
                ┌──┐   ┌──┐   ┌──┐
clock        ───┘  └───┘  └───┘  └──
addr=2,wren=1,data=AA ──────────────  (整段保持)
                ↑ 第 1 个沿：同址又读又写

READ_NEW_DATA=0 (<=, 读旧值):   read_data 在沿后 = 00（内存原值）
READ_NEW_DATA=1 (=,  读新值):   read_data 在沿后 = AA（刚写入的值）
```

两种实现的「分叉」完全由 `READ_NEW_DATA` 在 `generate` 里二选一（[RAM_Single_Port.v:L107-L129](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L107-L129)）。

#### 4.2.3 源码精读

读旧值分支（非阻塞）：[RAM_Single_Port.v:L109-L116](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L109-L116)。

读新值分支（阻塞）：[RAM_Single_Port.v:L120-L127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L120-L127)。两段代码**除 `<=` 与 `=` 外完全相同**——这就是「赋值方式即硬件行为」的最纯粹示范。

> **为什么作者标注「This isn't proper」？** 见 [RAM_Single_Port.v:L93-L105](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L93-L105)。按 IEEE 1364-2001 的调度语义，在时钟块里用阻塞赋值**可能**引发仿真竞争（取决于 always 块的求值顺序）。作者说实践中没遇到过（可能因为这段逻辑被单独封装成一个模块，影响了事件调度），且找不到更干净的方式来表达「写先于读」又能让综合器自由推断 BRAM。也正因如此，代码用 `// verilator lint_off BLKSEQ` ... `lint_on` 把 Verilator 的「时钟块里出现阻塞赋值」告警关掉（[RAM_Single_Port.v:L119-L128](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L119-L128)）。作者的结语很坦诚：**「请检查你的综合结果！」**

厂商侧的控制旋钮（[RAM_Single_Port.v:L31-L46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Single_Port.v#L31-L46)）：Quartus 用 RAMSTYLE 里的 `no_rw_check`（或全局 `ADD_PASS_THROUGH_LOGIC_TO_INFERRED_RAMS OFF`）来抑制写转发；Vivado 则用 `rw_addr_collision = "yes"/"no"/"auto"`。这些与 `READ_NEW_DATA` 的行为相呼应，是同一件事在不同工具里的不同表达。

#### 4.2.4 代码实践（仿真型，完整动手）

**目标**：用仿真亲眼看到 `READ_NEW_DATA=0` 与 `=1` 在同址 RDW 时的差别，并验证「为什么必须用阻塞赋值才能读出新值」。

**操作步骤**：

1. 把下面的测试台存为 `tb_ram_compare.v`（这是**示例代码**，不属于项目原文件）：

```verilog
// 示例代码：对比 READ_NEW_DATA=0 与 =1 在同址同时读写时的差别
`timescale 1ns/1ps
`default_nettype none

module tb_ram_compare;
    localparam WORD_WIDTH = 8;
    localparam ADDR_WIDTH = 3;
    localparam DEPTH      = 8;

    reg                       clock = 1'b0;
    reg                       wren;
    reg  [ADDR_WIDTH-1:0]     addr;
    reg  [WORD_WIDTH-1:0]     write_data;
    wire [WORD_WIDTH-1:0]     read_data_old;   // READ_NEW_DATA=0
    wire [WORD_WIDTH-1:0]     read_data_new;   // READ_NEW_DATA=1

    RAM_Single_Port #(
        .WORD_WIDTH(WORD_WIDTH),
        .ADDR_WIDTH(ADDR_WIDTH),
        .DEPTH(DEPTH),
        .READ_NEW_DATA(0)          // 读旧值
    ) ram_old (
        .clock(clock), .wren(wren), .addr(addr),
        .write_data(write_data), .read_data(read_data_old)
    );

    RAM_Single_Port #(
        .WORD_WIDTH(WORD_WIDTH),
        .ADDR_WIDTH(ADDR_WIDTH),
        .DEPTH(DEPTH),
        .READ_NEW_DATA(1)          // 读新值（写转发）
    ) ram_new (
        .clock(clock), .wren(wren), .addr(addr),
        .write_data(write_data), .read_data(read_data_new)
    );

    always #5 clock = ~clock;

    initial begin
        // 第 1 拍：addr=2、写入 0xAA，同时也在读 addr=2（同址 RDW）
        addr = 3'd2; write_data = 8'hAA; wren = 1'b1;
        @(posedge clock); #1;   // #1 等非阻塞/阻塞赋值都落定
        $display("after coincident R/W to addr=2:");
        $display("  READ_NEW_DATA=0 read_data = %h (期望 00，旧值)", read_data_old);
        $display("  READ_NEW_DATA=1 read_data = %h (期望 aa，新值)", read_data_new);

        // 第 2 拍：停止写，再读一次 addr=2
        wren = 1'b0;
        @(posedge clock); #1;
        $display("next cycle, read addr=2 again:");
        $display("  READ_NEW_DATA=0 read_data = %h (期望 aa)", read_data_old);
        $display("  READ_NEW_DATA=1 read_data = %h (期望 aa)", read_data_new);
        $finish;
    end
endmodule
```

2. 用 Icarus Verilog 编译运行（项目里没有 RAM 的现成测试台，本讲义提供的为示例）：
   ```bash
   iverilog -g2001 -o sim tb_ram_compare.v RAM_Single_Port.v
   vvp sim
   ```

**需要观察的现象**：第 1 拍同址 RDW 后，`ram_old`（非阻塞）输出 `00`，`ram_new`（阻塞）输出 `aa`；第 2 拍两者都输出 `aa`（说明写确实落盘了）。

**预期结果**（请在本地运行后核对）：
```
after coincident R/W to addr=2:
  READ_NEW_DATA=0 read_data = 00 (期望 00，旧值)
  READ_NEW_DATA=1 read_data = aa (期望 aa，新值)
next cycle, read addr=2 again:
  READ_NEW_DATA=0 read_data = aa (期望 aa)
  READ_NEW_DATA=1 read_data = aa (期望 aa)
```

**回答实践任务的核心问题**：`READ_NEW_DATA=1` 分支之所以用阻塞赋值 `=`，是因为阻塞赋值「立即生效」，使得 `ram[addr] = write_data` 先把新值写进数组，紧接着的 `read_data = ram[addr]` 读到的就是刚写入的新值——这正是「写转发 / 读新值」的语义；若改回非阻塞 `<=`，两句都按沿前旧值采样，`read_data` 就只能拿到旧值，写转发也就推断不出来了。

#### 4.2.5 小练习与答案

- **练习**：如果某 FPGA 的 LUT RAM 天然就是「读旧值」，你会把 `READ_NEW_DATA` 设成几？
  **答案**：设成 `0`。读旧值正好匹配 LUT RAM（如 MLAB）的硬件行为，无需额外推断写转发逻辑。
- **练习**：作者为什么用 `generate if/else` 而不是用 `if` 写在 always 块里来切换两种行为？
  **答案**：`generate` 在**精化期（elaboration）**就二选一，综合器看到的是「纯非阻塞」或「纯阻塞」的单一 always 块，模式清晰、易于识别成 RAM；若用普通 `if` 在运行期切换，综合器难以稳定地推断成某一种 BRAM 行为。
- **练习**：代码里的 `verilator lint_off BLKSEQ` 是干嘛的？
  **答案**：`BLKSEQ` 是 Verilator 对「在时钟沿驱动的 always 块里使用阻塞赋值」的告警。作者明知此处用阻塞「不合规」（可能引发仿真竞争），但为了表达写转发推断不得不如此，故显式关掉该告警，避免噪声。

---

### 4.3 双端口 RAM：Simple（1W1R）与 True（2WR）的选型

#### 4.3.1 概念说明

单端口的「一个地址口」是带宽瓶颈。双端口 RAM 给出两个独立端口，允许同一拍访问两个不同地址。它分两种：

- **Simple Dual-Port（1W1R）**：一个**专职写口**（`write_addr`/`write_data`/`wren`）+ 一个**专职读口**（`read_addr`/`read_data`/`rden`），共用一个时钟。作者称其为「配合写转发逻辑时**最快**的 BRAM 配置」（[RAM_Simple_Dual_Port.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L4-L7)）。
- **True Dual-Port（2WR）**：两个**对等的读/写口** A 与 B，各自有独立地址、可读可写，共用一个时钟。它对应底层 BRAM 的「真双口」模式。

两者的写转发机制与单端口**完全相同**（同样的 `READ_NEW_DATA` + 阻塞/非阻塞套路，注释几乎一字不差），所以本节聚焦于它们在「端口职责」和「端口间关系」上的差异。

#### 4.3.2 核心流程

**Simple Dual-Port** 每拍的读写各自独立（[RAM_Simple_Dual_Port.v:L116-L125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L116-L125) 为读旧值版）：

```
if (wren) ram[write_addr] <= write_data;   // 写口
if (rden) read_data <= ram[read_addr];      // 读口（rden=0 时 read_data 保持不变）
```

注意读口有 `rden`：不读时 `read_data` **冻结**在上一拍的值。

**True Dual-Port** 把 A、B 两口放进**两个独立的 always 块**（关键设计！见 [RAM_True_Dual_Port.v:L117-L118](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_True_Dual_Port.v#L117-L118) 的注释）。每口每拍都读、且可按 `wren_X` 写：

```
// 口 A（与口 B 完全对等，各自一个 always 块）
if (wren_A) ram[addr_A] <= write_data_A;
read_data_A <= ram[addr_A];          // 每拍都读，没有 rden
```

为什么拆成两个 always 块？为了表达「A、B 两口**彼此无优先级**」——这正贴合底层 BRAM 的硬件语义。若写成同一个 always 块的先后两句，仿真上就隐含了顺序；分两块则明确告诉综合器「两口平等」。代价是：当 A、B 同拍写同一地址时，结果是**不确定的**（和真实 BRAM 一样）。

三种 RAM 的端口对照：

| 模块 | 端口形态 | 读口是否每拍都读 | 典型资源/速度 |
| --- | --- | --- | --- |
| `RAM_Single_Port` | 1RW（读写共用一个地址） | 是（无 rden） | 最省资源，带宽最低 |
| `RAM_Simple_Dual_Port` | 1W1R（写口 + 读口，地址分离） | 否（rden 控制冻结） | 配合写转发时最快 |
| `RAM_True_Dual_Port` | 2WR（两个对等读/写口） | 是（每口无 rden） | 两代理各自可读可写；同址同写结果不定 |

#### 4.3.3 源码精读

Simple Dual-Port 头部：[RAM_Simple_Dual_Port.v:L53-L76](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L53-L76)。注意 `write_addr` 与 `read_addr` 是两个独立地址，`rden` 门控读口。其写转发 generate 与单端口如出一辙：读旧值 [L116-L125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L116-L125)、读新值 [L129-L138](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L129-L138)。

True Dual-Port 头部：[RAM_True_Dual_Port.v:L52-L76](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_True_Dual_Port.v#L52-L76)，A/B 两口各有一整套 `wren/addr/write_data/read_data`。其 generate 把两口分别放在两个 always 块里（读旧值 [L122-L136](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_True_Dual_Port.v#L122-L136)、读新值 [L140-L154](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_True_Dual_Port.v#L140-L154)）。

> 同址写冲突：True Dual-Port 里若 A、B 同拍写同一地址，两个 always 块都驱动 `ram[addr]`，结果不确定——这**故意**复刻了真实 BRAM 的行为。设计时必须从协议层保证不会发生，或改用带冲突处理的多端口存储器（下一讲 u7-l2 的 `RAM_Multiported_LE`）。

#### 4.3.4 代码实践（选型型）

**目标**：为一个「单写多读」的寄存器堆选择 Simple 还是 True Dual-Port。

**场景**：一个 RISC 处理器的寄存器堆，每周期需要：写回 1 个结果（来自 ALU），同时读出 **1 个**源操作数送 ALU。读写地址互不相同。

**步骤与思考**：

1. 数端口：1 个写 + 1 个读 → 形态是 **1W1R**。
2. 候选：`RAM_Simple_Dual_Port` 正好是 1W1R，且作者说它配合写转发时最快。
3. 排除 `RAM_True_Dual_Port`：它的两口都能写也能读，对「专职一写一读」是浪费，而且引入了「同址双写不定」的风险；若误把读口当成「可能写」的口，还会让综合器难以映射成最快的 BRAM 模式。
4. 排除 `RAM_Single_Port`：只有一个地址口，无法同拍读写两个地址，带宽不够。

**预期结论**：选 **`RAM_Simple_Dual_Port`**（设 `READ_NEW_DATA=1` 启用写转发拿速度）。若该寄存器堆需要「同拍读 2 个操作数 + 写 1 个」，那就是 1W2R，超出本讲三模块能力，需要下一讲的多端口存储器。

**待本地验证**：在你目标器件的综合报告里，确认 `RAM_Simple_Dual_Port` 确实被映射成了 1 块 BRAM（而非触发器堆），并对比 `READ_NEW_DATA=0/1` 时的 `Fmax`。

#### 4.3.5 小练习与答案

- **练习**：Simple Dual-Port 的读口有 `rden`、True Dual-Port 的两口没有 `rden`，这会带来什么行为差异？
  **答案**：Simple 的读口在 `rden=0` 时 `read_data` **保持上一拍值**（冻结）；True 的两口**每拍都更新**输出。前者适合「偶尔读、想锁存结果」的场景，后者更像两个持续流水的访问口。
- **练习**：True Dual-Port 为什么把 A、B 拆进两个 always 块而不是写成一个块里的两句？
  **答案**：为了表达「两口无优先级」，匹配底层真双口 BRAM 的语义。一个块里的先后两句会隐含求值顺序，仿真与「两口平等」的硬件不符。

---

## 5. 综合实践

把本讲三块知识串起来：用推断的方式实现一个**深度 16、字宽 8、单写单读、读新值（写转发）的小寄存器堆**，并验证其同址 RDW 行为。

1. **选型**：1 写 + 1 读、要速度 → `RAM_Simple_Dual_Port`，`READ_NEW_DATA=1`，`RAMSTYLE` 按你的器件填（Intel 用 `"M10K"`，Xilinx 留空走 `block`）。
2. **实例化**：设 `.WORD_WIDTH(8)、.ADDR_WIDTH(4)、.DEPTH(16)、.READ_NEW_DATA(1)、.USE_INIT_FILE(0)、.INIT_VALUE(8'h00)`。
3. **写一个示例测试台**：先写 `addr=5` 写入 `8'h3C`；下一拍**同址** `read_addr=5`、`write_addr=5`、写入 `8'hFF`，观察 `read_data` 是否因写转发而立刻出现 `FF`（而不是上一拍的 `3C`，也不是 `00`）。
4. **综合核对**：在 CAD 工具里查看综合报告/原理图，确认它被推断成一块 BRAM（而非 16 个触发器），并记录 `Fmax`。
5. **反思**：把 `READ_NEW_DATA` 改回 `0` 重新仿真同址 RDW，确认 `read_data` 这次返回旧值 `3C`；再综合一次，对比资源与 `Fmax` 的变化。

> 这是「源码阅读 + 仿真 + 综合核对」三位一体的实践。若没有 FPGA 工具链，至少完成第 1～3 步的仿真部分（用 iverilog），其余标注「待本地验证」。

## 6. 本讲小结

- **推断优于实例化**：用「存储数组 + 时钟块」描述行为，让综合器映射到 BRAM/LUT RAM，可移植且可读。本书三个 RAM 模块都走这条路。
- **`read_data` 一律 `output reg`**：匹配 BRAM 的寄存输出读（延迟 1 拍），且不给同步清零以保持可重定时——要清零请在下游接 `Annuller`。
- **本讲核心机关**：`READ_NEW_DATA` 通过 `generate` 在非阻塞（读旧值）与阻塞（读新值/写转发）之间二选一；**赋值方式即硬件行为**。阻塞赋值在时钟块里「不合规」（可能仿真竞争），却是表达写转发推断最干净的方式，故用 `verilator lint_off BLKSEQ` 抑制告警。
- **写转发还关乎速度**：有了写转发逻辑，被同址写污染的读结果会在输出选择器处被丢弃换新，RAM 跑得更高频；Simple Dual-Port 配写转发往往是最快配置。
- **三种端口形态**：单端口（1RW，读写共地址）、Simple 双端口（1W1R，专职读写口、读口可冻结）、True 双端口（2WR，两个对等读/写口、两口无优先级、同址双写不定）。
- **初始化两路**：`USE_INIT_FILE=0` 用 `initial`+`for` 循环写 `INIT_VALUE`；`=1` 用 `$readmemh`，文件可用 `RAM_generate_empty_init_file.py` 生成。

## 7. 下一步学习建议

本讲的三种 RAM 最多只有「1 写 + 1 读」或「两个对等口」。当你的设计需要 **1 写多读（1WnR）** 或更多端口时，单块 BRAM 就不够了。下一讲 **u7-l2「多端口存储器与初始化」** 会讲解：

- `RAM_Multiported_LE`：用逻辑单元（LE/LUT）实现多端口，并处理读写冲突。
- `RAM_1WnR_Replicated`：用「复制存储副本」的方式支持多个读端口。
- 顺带深入 `$readmemh` 与初始化文件格式。

建议你先按本讲「综合实践」亲手推断一块 Simple Dual-Port BRAM，再去读 `RAM_1WnR_Replicated`，体会「为什么 1WnR 不能靠 True Dual-Port 硬撑，而要用复制法」。
