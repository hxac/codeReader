# 存储原语：双口 RAM 与寄存器堆

> 本讲属于第 3 单元「数据通路与存储组件」，承接 [u2-l2 时序原语：触发器家族](u2-l2-sequential-flops.md)。
> 前置要求：你已经理解 D 触发器（DFF）、`always @(posedge clk)`、非阻塞赋值 `<=`，以及 OH! 的「soft/hard 双实现」与参数化写法。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 OH! 的双口 RAM（`oh_dpram`）和单口 RAM（`oh_ram`），说清楚写口、读口、地址译码与可选输出寄存器各自的作用。
2. 理解 `SYN` / `TYPE` / `SHAPE` / `TARGET` 这一组参数如何让同一份顶层在 **soft（可综合 RTL）** 与 **hard（ASIC 存储器宏）** 两种实现之间切换，并能指出它们在三个文件里的真实取值与一处历史不一致。
3. 读懂多端口寄存器堆 `oh_regfile`，理解它如何复用 `oh_mux`、用「拼接总线 + one-hot 写使能译码」实现多个读口/写口。
4. 自己动手用 `oh_dpram` 搭一个 32×16 的小存储，写一组地址再读回验证。

---

## 2. 前置知识

### 2.1 从「一个触发器」到「一块存储」

[u2-l2](u2-l2-sequential-flops.md) 里的 `oh_dffq` 是「记住 1 比特」的最小单元。如果你要记住 1000 个 32 位的数，当然可以例化 1000 个 `oh_dffq`——但这既不省面积也不省功耗。

真实芯片里，大量数据的存储交给 **存储器（memory）** 来做：

- **触发器阵列（register file / flop array）**：每个比特一个独立的 DFF。优点是端口灵活（想几个读口几个写口都行）、随机访问快；缺点是密度低、面积大。适合「少量、多端口」的场景，比如寄存器堆。
- **SRAM 宏（RAM）**：用 6T（六个晶体管）存储单元搭成的阵列，译码器共享行/列选择线。密度高、面积省；但端口结构受限于宏的物理版图，通常是固定的 1 读 1 写或 2 口。适合「大量、少端口」的场景，比如 FIFO 缓冲、数据缓存。

OH! 用两类原语分别对应这两条路线：

| 原语 | 本质 | 典型用途 |
|------|------|----------|
| `oh_dpram` / `oh_ram` | RAM 阵列（soft 模式下综合成块 RAM，hard 模式下替换成 SRAM 宏） | FIFO、大缓冲 |
| `oh_regfile` | 多读多写的寄存器阵列 | CPU 寄存器组、少量配置寄存器 |

### 2.2 你需要记住的几个术语

- **单口 RAM（Single-Port RAM）**：只有一套地址/数据线，读和写**分时共享**同一组端口。
- **双口 RAM（Dual-Port RAM）**：有独立的写口（`wr_*`）和读口（`rd_*`），读写可以**同时**进行，甚至可以用不同时钟（`wr_clk` / `rd_clk`）。本讲的 `oh_dpram` 是「1 写口 + 1 读口」的真双口结构。
- **位写使能（per-bit write enable，`wem`）**：N 位的写数据，配一个 N 位的 `wem` 掩码，哪一位为 1 才写哪一位。这就是字节使能（byte-enable）的位级版本，做「读-改-写」时很有用。
- **BIST（Built-In Self-Test，内建自测）**：芯片出厂前用片上逻辑给存储阵列跑测试向量的接口。
- **hard macro（硬核/硬宏）**：晶圆厂用 SRAM 编译器生成的、固定版图的存储器黑盒，交付成一个 `.db` 或行为模型 `.v`。你只看得到端口，看不到内部。
- **`$clog2(x)`**：Verilog 2005 标准函数，返回「至少能表示 x 个地址」所需的地址位宽，即向上取整的对数。例如 `$clog2(32)=5`。

地址位宽的推导关系是：

\[
\text{AW} = \lceil \log_2(\text{DEPTH}) \rceil = \$clog2(\text{DEPTH})
\]

即 DEPTH 个存储字需要 AW 位地址来编址。

---

## 3. 本讲源码地图

| 文件 | 作用 | 是否可综合（soft） |
|------|------|--------------------|
| [stdlib/rtl/oh_dpram.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v) | 双口 RAM（1 写口 + 1 读口），带可选输出寄存器、BIST 与电源接口 | 是（soft 分支） |
| [stdlib/rtl/oh_ram.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ram.v) | 单口 RAM（读写共享一套端口），结构与 `oh_dpram` 几乎对称 | 是（soft 分支） |
| [stdlib/rtl/oh_regfile.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v) | 多读多写的寄存器堆，用 `oh_mux` 处理多写口冲突 | 是 |
| [stdlib/rtl/oh_fifo_sync.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v) | 同步 FIFO，**内部例化 `oh_dpram` 作为存储体**，是本讲「参数怎么传」的最佳范例 | 是 |

> ⚠️ 提醒：仓库里**没有** `asic_memory_dp` / `asic_memory_sp` 这两个文件（用 `grep -rl "asic_memory" .` 只能在 `oh_dpram.v` / `oh_ram.v` 里找到对它们的**引用**，找不到定义）。也就是说 hard 分支目前是「占位桩（stub）」，真正流片时要由晶圆厂的 SRAM 编译器生成并替换。这一点本讲第 4.2 节会详细说明。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 双口 RAM `oh_dpram`**（顺带对照单口 `oh_ram`）
- **4.2 hard macro 参数：`SYN` / `TYPE` / `SHAPE` / `TARGET` 的真相**
- **4.3 寄存器堆 `oh_regfile`**

### 4.1 双口 RAM：`oh_dpram` 的读写口与地址译码

#### 4.1.1 概念说明

`oh_dpram` 是一块「1 个写口 + 1 个读口」的双口 RAM。

- **写口**：在 `wr_clk` 上升沿，当 `wr_en` 有效且对应位的 `wr_wem[i]` 为 1 时，把 `wr_din[i]` 写进 `ram[wr_addr]` 的第 i 位。
- **读口**：组合逻辑直接把 `ram[rd_addr]` 读出来到 `rd_dout`（取决于 `REG` 参数，可以选择再打一拍寄存器）。
- 读写口各自带独立时钟（`wr_clk` / `rd_clk`），所以它天然支持两个时钟域——这也是为什么后面的异步 FIFO（[u3-l2](u3-l2-fifo-design.md)）会把它当存储体。

为什么读用组合逻辑、写用边沿？因为综合器（以及真实 SRAM 宏）的标准模型就是「**同步写、异步读（或可选同步读）**」：写必须在时钟沿落到阵列里，读可以看作一张查找表。把 `reg [N-1:0] ram [0:DEPTH-1];` 这种数组交给综合器，FPGA 上会推断成块 RAM（Block RAM），ASIC soft 流程里则推断成寄存器阵列或 SRAM。

#### 4.1.2 核心流程

一次「写后读」的时序（`REG=0`，即不选输出寄存器时）：

```text
周期 0:  wr_clk↑  wr_en=1, wr_wem=全1, wr_addr=5, wr_din=0xCAFE  → ram[5]←0xCAFE
周期 1:  令 rd_addr=5                                            → rd_dout=0xCAFE（组合输出，同周期生效）
```

若 `REG=1`，读数据多一级寄存器，`rd_dout` 要在 `rd_en` 有效的**下一个** `rd_clk` 沿才出现——读延迟从 0 拍变成 1 拍，但时序（setup/hold）更好，能跑更高频率。这正是参数 `REG` 的取舍。

地址译码本身没有复杂逻辑：地址位宽 AW 由 `DEPTH` 自动算出，`ram[wr_addr[AW-1:0]]` 直接用作数组下标。所谓「译码」就是 Verilog 数组的位索引。

#### 4.1.3 源码精读

**参数与端口列表**——注意地址位宽 `AW` 是用 `$clog2(DEPTH)` 推导出来的，不用手填：

参考 [stdlib/rtl/oh_dpram.v:8-39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L8-L39)。这里定义了 `N`（位宽）、`DEPTH`（深度）、`REG`（是否寄存输出）、`TARGET`、`SHAPE`、`AW`（派生），以及写口（`wr_clk/wr_en/wr_wem/wr_addr/wr_din`）、读口（`rd_clk/rd_en/rd_addr/rd_dout`）、BIST 口（`bist_*`）、电源/修复口（`shutdown/vss/vdd/vddio/memconfig/memrepair`）。

> 🔎 留意：`oh_dpram` 的 soft/hard 开关参数叫 **`TARGET`**（默认 `"DEFAULT"`），**不是** `SYN`。这一点和 `oh_ram` 不同，是第 4.2 节的重点。

**soft/hard 切换骨架**——`generate if` 在编译期二选一：

参考 [stdlib/rtl/oh_dpram.v:41-42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L41-L42)。`TARGET == "DEFAULT"` 走 soft（可综合 RTL）分支，否则走 hard（`asic_memory_dp`）分支。

**写口——逐位写使能**：

参考 [stdlib/rtl/oh_dpram.v:51-55](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L51-L55)。这里用 `for` 循环遍历每一位，只有 `wr_en & wr_wem[i]` 同时为真才写第 i 位。注意它用的是阻塞赋值 `=`（在 `for` 循环里逐位写入数组元素，这是写存储阵列的常见写法）。

**读口——组合读 + 可选输出寄存**：

参考 [stdlib/rtl/oh_dpram.v:57-67](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L57-L67)。`rdata` 由 `assign` 组合给出；`rd_reg` 在 `rd_clk` 上升沿、`rd_en` 有效时锁存 `rdata`；最后用 `REG==1 ? rd_reg : rdata` 决定输出是否多打一拍。

**hard 分支（占位桩）**：

参考 [stdlib/rtl/oh_dpram.v:69-75](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L69-L75)。例化 `asic_memory_dp`，但该模块在仓库里**没有定义**，需要流片时由 SRAM 编译器提供。

**单口 RAM `oh_ram` 对照**：结构和 `oh_dpram` 几乎一样，只是读写共用一套端口（`clk/en/wem/addr/din/dout`）。

参考 [stdlib/rtl/oh_ram.v:8-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ram.v#L8-L16) 看它的参数（注意它用的是 `SYN`/`TYPE`，不是 `TARGET`）；

参考 [stdlib/rtl/oh_ram.v:47-63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ram.v#L47-L63) 看它的写口与读口（和双口版本一一对应，只是地址/数据线只有一套）。

#### 4.1.4 代码实践

**实践目标**：亲手例化 `oh_dpram`，写 4 个地址再读回，验证存储正确，并体会 `REG` 参数对读延迟的影响。

由于仓库没有现成的 RAM 测试台（`stdlib/testbench/` 里只有 `dut_fifo_generic.v` 等 FIFO/外设包装），我们写一个最小的独立 testbench（**示例代码**）。

第 1 步：新建文件 `oh-tutorial/lab/dpram_demo_tb.v`（你也可以放在任意临时目录），内容如下：

```verilog
// 示例代码：oh_dpram 的最小读写验证（REG=0，组合读）
`timescale 1ns/1ps
module dpram_demo_tb;

   // N=16 位宽, DEPTH=32 深度 -> AW = $clog2(32) = 5
   reg         wr_clk = 0;
   reg         rd_clk = 0;
   reg         wr_en  = 0;
   reg  [15:0] wr_wem = 16'hFFFF;
   reg  [4:0]  wr_addr;
   reg  [15:0] wr_din;
   reg         rd_en  = 0;
   reg  [4:0]  rd_addr;
   wire [15:0] rd_dout;

   // 例化 oh_dpram，注意把 BIST/电源口全部拉成静默值
   oh_dpram #(.N(16), .DEPTH(32), .REG(0), .TARGET("DEFAULT"), .SHAPE("SQUARE"))
   dut (
      .wr_clk(wr_clk), .wr_en(wr_en), .wr_wem(wr_wem),
      .wr_addr(wr_addr), .wr_din(wr_din),
      .rd_clk(rd_clk),  .rd_en(rd_en), .rd_addr(rd_addr), .rd_dout(rd_dout),
      .bist_en(1'b0), .bist_we(1'b0), .bist_wem(16'h0),
      .bist_addr(5'h0), .bist_din(16'h0),
      .shutdown(1'b0), .vss(1'b0), .vdd(1'b1), .vddio(1'b1),
      .memconfig(8'h0), .memrepair(8'h0)
   );

   always #5 wr_clk = ~wr_clk;   // 100MHz 写时钟
   always #5 rd_clk = ~rd_clk;   //   同频读时钟

   integer i, errors = 0;
   reg [15:0] expect_val;

   initial begin
      // 1) 往地址 0..3 写入 4 个不同的值
      for (i = 0; i < 4; i = i + 1) begin
         @(negedge wr_clk);
         wr_en   = 1'b1;
         wr_addr = i[4:0];
         wr_din  = 16'h1000 + i[15:0];   // 0x1000,0x1001,0x1002,0x1003
      end
      @(negedge wr_clk);
      wr_en = 1'b0;

      // 2) 读回地址 0..3，REG=0 -> 组合读，rd_addr 一变 rd_dout 立刻更新
      for (i = 0; i < 4; i = i + 1) begin
         rd_en   = 1'b1;
         rd_addr = i[4:0];
         #1;                            // 等组合逻辑稳定
         expect_val = 16'h1000 + i[15:0];
         if (rd_dout !== expect_val) begin
            $display("ERROR addr=%0d got=%h exp=%h", i, rd_dout, expect_val);
            errors = errors + 1;
         end else begin
            $display("OK    addr=%0d got=%h", i, rd_dout);
         end
      end

      $display("TEST %s", (errors==0) ? "PASSED" : "FAILED");
      $finish;
   end
endmodule
```

第 2 步：直接用 iverilog 编译运行（`oh_dpram.v` 和这个 tb 放同一目录，或在 `-y` 里指向 `stdlib/rtl`）：

```bash
iverilog -g2005 -o dpram_demo.vvp dpram_demo_tb.v stdlib/rtl/oh_dpram.v
vvp dpram_demo.vvp
```

第 3 步（需要观察的现象与预期结果）：

- 预期终端打印 4 行 `OK addr=.. got=1001..` 之类的成功信息，最后 `TEST PASSED`。
- 把例化里的 `.REG(0)` 改成 `.REG(1)` 重新跑：读路径多了一级寄存器，原 testbench 的 `#1` 组合等待不再够用——读地址给出后要等到**下一个** `rd_clk` 沿 `rd_dout` 才更新，测试会**失败**。修复方法：在设置 `rd_addr` 后加 `@(posedge rd_clk);` 再比较。这正好让你体会 `REG` 的「换频率、加延迟」取舍。

> 说明：本讲义没有在本机执行以上命令，运行结果为「待本地验证」。地址译码与读写逻辑是确定的，预期结果可推算；但具体 iverilog 版本对 `generate`/`$clog2` 的支持差异请以本地实际为准。

#### 4.1.5 小练习与答案

**练习 1**：`oh_dpram` 的写口用了 `wr_wem`（逐位写使能）。如果只想更新地址 5 的**高字节**（bit[15:8]），其余字节保持不变，`wr_en` 和 `wr_wem` 应该怎么给？

**参考答案**：`wr_en=1`，`wr_wem=16'hFF00`（bit[15:8]=1，bit[7:0]=0），`wr_din` 的高字节放新值、低字节任意（因为被掩码挡住不会写）。

**练习 2**：把 `DEPTH` 从 32 改成 48，`AW` 会变成几？还能直接用 `$clog2` 吗？

**参考答案**：`$clog2(48)=6`，`AW=6`，能编址 64 个地址。但 48 不是 2 的幂，数组 `ram[0:47]` 只有 48 项，地址 48–63 会越界（仿真里读出 `x`，综合器一般会回卷或报错）。所以工程上 `DEPTH` 通常取 2 的幂。

---

### 4.2 hard macro 参数：`SYN` / `TYPE` / `SHAPE` / `TARGET` 的真相

#### 4.2.1 概念说明

本节是本讲的重点，也是 spec 指定要「重点解析」的部分。

同一块存储，OH! 希望在两种物理形态间无缝切换：

- **soft**：用 `reg` 数组 + `always` 块写成可综合 RTL。FPGA 上综合成 Block RAM，ASIC soft 流程里也能用。优点是可移植、可仿真。
- **hard**：替换成晶圆厂的 **SRAM 宏（hard macro）**——一个版图固定、密度极高、速度更快的黑盒（本仓库里的 `asic_memory_dp` / `asic_memory_sp`）。

为了让顶层不用改代码就能在两者间切换，OH! 用一组「字符串参数 + `generate if`」做编译期选择（这套手法在 [u1-l4](u1-l4-coding-style.md)、[u2-l2](u2-l2-sequential-flops.md) 里已经见过）。本讲要弄清楚这组参数的**真实名字和取值**——因为三个文件并不完全一致。

| 参数 | 含义 | 谁在用 |
|------|------|--------|
| `SYN` | `"TRUE"`=soft（RTL），`"FALSE"`=hard（宏） | `oh_ram`、`oh_fifo_sync`、`oh_mux` 等 |
| `TYPE` | 透传字符串，选择 hard 宏的具体型号/工艺变体，默认 `"DEFAULT"` | `oh_ram`、`oh_fifo_sync` |
| `SHAPE` | hard 宏的物理形状：`"SQUARE"` / `"TALL"` / `"WIDE"`，影响阵列长宽比与面积/时序 | `oh_dpram`、`oh_ram`、`oh_fifo_sync` |
| `TARGET` | `oh_dpram` 专用的 soft/hard 开关，`"DEFAULT"`=soft | **仅** `oh_dpram` |

`SHAPE` 为什么重要？SRAM 编译器允许你选阵列的版图长宽比：

- **SQUARE**：行列接近，面积/时序均衡，最常用。
- **TALL**：窄而高，适合放在芯片窄缝里，但字线（wordline）长、速度慢。
- **WIDE**：宽而矮，位线（bitline）短、速度快，但横向占地多。

这三种形状在 RTL 阶段看不出区别（soft 分支都一样），只在 hard 流程替换真实宏时才生效。

BIST / 电源 / 修复那一组端口（`bist_*`、`shutdown`、`vdd`、`vddio`、`memconfig`、`memrepair`）也只在 hard 模式有意义：`bist_*` 接内建自测逻辑、`shutdown/vdd/vddio` 做电源域关断以省漏电、`memrepair` 是冗余修复（用备用行/列替换坏单元）。soft 模式下它们被静默拉死，不影响功能。

#### 4.2.2 核心流程

参数流动的伪代码（以 `oh_fifo_sync` → `oh_dpram` 为例）：

```text
顶层（如某个 IP）
   │  传 SYN/TYPE/SHAPE 给
   ▼
oh_fifo_sync   #(.SYN(..), .TYPE(..), .SHAPE(..))
   │  把 SYN/TYPE/SHAPE 继续透传给内部存储体
   ▼
oh_dpram       #(.SYN(..), .TYPE(..), .SHAPE(..))   ← 见下方「真相」
   │
   ├─ if (TARGET=="DEFAULT") : reg 数组（soft，本仓库可仿真）
   └─ else                    : asic_memory_dp（hard，仓库内为桩）
```

理想情况下，只要改顶层一处 `SYN="FALSE"`，整条链路就从 RTL 翻译成 SRAM 宏。但本仓库的实际代码有一处历史不一致，需要你睁大眼睛看 4.2.3。

#### 4.2.3 源码精读

**`oh_ram`：规范的 `SYN`/`TYPE` 用法**——这是「标准答案」：

参考 [stdlib/rtl/oh_ram.v:8-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ram.v#L8-L16)，参数声明 `SYN="TRUE"`、`TYPE="DEFAULT"`、`SHAPE="SQUARE"`；

参考 [stdlib/rtl/oh_ram.v:39-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ram.v#L39-L41)，`if(SYN == "TRUE") begin: rtl ... else begin: hard`——soft 与 hard 干净二选一，hard 分支例化 `asic_memory_sp`。

**`oh_dpram`：开关参数叫 `TARGET`，不是 `SYN`**——这是「不一致点之一」：

参考 [stdlib/rtl/oh_dpram.v:8-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L8-L15)，它的参数是 `TARGET="DEFAULT"`、`SHAPE="SQUARE"`，**没有** `SYN`、**没有** `TYPE`；

参考 [stdlib/rtl/oh_dpram.v:41-42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L41-L42)，判据是 `if(TARGET == "DEFAULT")`。

**`oh_fifo_sync`：把 `SYN`/`TYPE` 传给了一个没有这俩参数的 `oh_dpram`**——这是「不一致点之二」，也是最值得你警惕的地方：

参考 [stdlib/rtl/oh_fifo_sync.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L8-L17)，FIFO 自己声明了 `SYN/TYPE/SHAPE`；

参考 [stdlib/rtl/oh_fifo_sync.v:111-139](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L111-L139)，它例化 `oh_dpram` 时写了 `.SYN(SYN), .TYPE(TYPE), .SHAPE(SHAPE)`。

> 🚨 **真相**：`oh_dpram` 根本没有 `SYN` 和 `TYPE` 这两个参数（它只有 `TARGET`）。所以 `oh_fifo_sync` 里 `.SYN(SYN)` 和 `.TYPE(TYPE)` 这两条**按名覆盖指向了目标模块中并不存在的参数**。在默认取值下（`SYN="TRUE"`）FIFO 走 soft 路径、功能正常，这个不一致平时不暴露；可一旦你想把 FIFO 切到 hard（`SYN="FALSE"`），这条链路并不会如预期地把 `oh_dpram` 也切到 hard——因为 `oh_dpram` 收不到这个开关。
>
> 这是 OH! 文档/脚本可能滞后（见 [u1-l1](u1-l1-project-overview.md)、[u1-l2](u1-l2-directory-structure.md) 的阅读原则）在源码层面的又一例证：**参数表必须以被例化模块的实际声明为准**。学习建议：在 `oh_dpram`、`oh_fifo_sync` 之间统一参数命名前，不要假设 `SYN` 能贯通到双口 RAM。

此外，hard 分支引用的 `asic_memory_dp` / `asic_memory_sp` 在仓库里**找不到定义**（`grep -rl "asic_memory" . --include=*.v` 只返回两个引用文件）。也就是说 hard 路径目前是**占位桩**，真正切到 hard 需要外部提供 SRAM 宏模型。soft 路径则完全可仿真、可综合。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，亲手核对上面这处参数不一致，而不是停留在「听讲」。

操作步骤：

1. 打开 [stdlib/rtl/oh_dpram.v:8-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L8-L15)，列出 `oh_dpram` 的**全部**参数名。
2. 打开 [stdlib/rtl/oh_fifo_sync.v:111-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L111-L116)，列出它在 `#(...)` 里向 `oh_dpram` 传了哪些参数名。
3. 做集合差：哪些是「FIFO 传了、`oh_dpram` 却没有」？哪些是「`oh_dpram` 有、FIFO 没传」？
4. （可选）用 iverilog 试编译一个把 `oh_fifo_sync` 例化为 `SYN="FALSE"` 的顶层，观察编译器是否对 `.SYN()/.TYPE()` 报警告或错误。

预期结果 / 观察现象：

- 第 3 步应得出：FIFO 多传了 `SYN`、`TYPE`；`oh_dpram` 多暴露了 `TARGET`（FIFO 没传它，于是 `TARGET` 取默认 `"DEFAULT"`，始终 soft）。
- 第 4 步的报错形式取决于 iverilog 版本——「待本地验证」。即便不报错，FIFO 的 hard 意图也无法传到存储体。

> 结论性提醒：本练习不修改任何源码（本讲义禁止改源码），只做核对与（可选的）只读编译验证。

#### 4.2.5 小练习与答案

**练习 1**：`SHAPE` 三个取值 `SQUARE`/`TALL`/`WIDE` 在 soft 分支里会影响仿真结果吗？为什么？

**参考答案**：不会。soft 分支是纯 RTL 的 `reg` 数组，没有任何与形状相关的逻辑；`SHAPE` 只在 hard 分支被透传给 SRAM 宏，影响的是物理版图长宽比。仿真里三者等价。

**练习 2**：`oh_fifo_sync` 想真正支持 hard 切换，最小改动是什么？（只说思路，不改代码）

**参考答案**：让 `oh_fifo_sync` 把软硬开关按 `oh_dpram` 实际的参数名来传——即把 `.SYN(SYN), .TYPE(TYPE)` 改成 `.TARGET(...)`（或反过来给 `oh_dpram` 补上 `SYN/TYPE` 参数并相应改判据），使开关能贯通到存储体。两种方向任选其一，关键是上下游参数名对齐。

---

### 4.3 寄存器堆：`oh_regfile` 的多端口设计

#### 4.3.1 概念说明

`oh_regfile` 是「少量、多端口」的存储，与 RAM 互补：RAM 端口少但容量大，寄存器堆容量小但端口多。典型场景是 CPU 的通用寄存器组——一个周期里可能要同时读 2~3 个寄存器、写 1 个寄存器。

它的参数完全围绕「端口」展开：

- `REGS`：寄存器个数（不是 RAM 的 DEPTH）。
- `RW`：每个寄存器的位宽（Register Width）。
- `RP`：读口（Read Port）数量。
- `WP`：写口（Write Port）数量。
- `RAW = $clog2(REGS)`：寄存器地址位宽。

难点在于「多写口」：如果有 3 个写口（`WP=3`）在同一个周期想写同一个寄存器，怎么办？`oh_regfile` 的策略是：对每个寄存器做 **one-hot 写使能译码**，再用 `oh_mux`（[u2-l1](u2-l1-combinational-primitives.md) 学过的 one-hot 多路选择器）选出「实际生效的那一个写口」的数据写入。若多个写口同时命中同一寄存器，由 `oh_mux` 的 one-hot 优先级（取决于输入排列）决定谁赢。

另一个特点：端口数据是**拼接总线**。`WP` 个写口的数据不是 `din0/din1/din2` 三根线，而是拼成一根 `wr_data[WP*RW-1:0]`，每个写口占其中 `RW` 位的一段。读口同理。这种打包方式让参数化更整洁（端口数变了，只要改位宽计算，不用增删端口）。

#### 4.3.2 核心流程

写路径（每个寄存器 i，每个写口 j）：

```text
write_en[i][j] = wr_valid[j] & (wr_addr 的第 j 段 == i)   // one-hot：第 j 个写口是否要写寄存器 i
若有任意 write_en[i][*] 为 1：
   datamux[i] = oh_mux(write_en[i], wr_data)              // 从命中写口里挑数据
   mem[i] <= datamux[i]                                    // 时钟沿写入
```

读路径（每个读口 i）：

```text
rd_data 的第 i 段 = {(RW){rd_valid[i]}} & mem[rd_addr 的第 i 段]
```

也就是：读口有效时输出对应寄存器内容，无效时输出全 0（用 `rd_valid` 复制成位宽再相与做掩码）。

#### 4.3.3 源码精读

**参数与端口**——注意端口全是拼接总线：

参考 [stdlib/rtl/oh_regfile.v:8-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L8-L25)。`REGS/RW/RP/WP/RAW`，以及写口 `wr_valid[WP-1:0]` / `wr_addr[WP*RAW-1:0]` / `wr_data[WP*RW-1:0]`，读口 `rd_valid[RP-1:0]` / `rd_addr[RP*RAW-1:0]` / `rd_data[RP*RW-1:0]`。

**存储阵列与 one-hot 写使能译码**：

参考 [stdlib/rtl/oh_regfile.v:40-47](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L40-L47)。双重 `for` 循环：外层遍历每个寄存器 `i`，内层遍历每个写口 `j`，计算 `write_en[i][j]`——第 j 个写口地址等于 i 时拉高。

**多写口数据选择（复用 `oh_mux`）**：

参考 [stdlib/rtl/oh_regfile.v:49-56](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L49-L56)。对每个寄存器例化一个 `oh_mux #(.N(RW), .M(WP))`，用 `write_en[i]` 作 one-hot 选择、`wr_data` 作被选数据，得到 `datamux[i]`。这正是 [u2-l1](u2-l1-combinational-primitives.md) 学过的 one-hot AND-OR 选择器在系统级的应用。

**寄存器写入**：

参考 [stdlib/rtl/oh_regfile.v:58-64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L58-L64)。`if (|write_en[i])` 即「该寄存器有任意写口命中」，则把 `datamux[i]` 用非阻塞赋值写进 `mem[i]`。

**读口**：

参考 [stdlib/rtl/oh_regfile.v:71-74](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L71-L74)。组合读，`rd_valid` 做掩码：有效才输出寄存器值，否则输出 0。

#### 4.3.4 代码实践

**实践目标**：用源码阅读 + 纸上推演，理解一个「2 写口、3 读口」的寄存器堆在一次事务里的数据流。

操作步骤：

1. 假设参数 `REGS=8, RW=16, WP=2, RP=3`，算出 `RAW=$clog2(8)=3`，于是 `wr_addr` 宽 `WP*RAW=6` 位、`wr_data` 宽 `WP*RW=32` 位、`rd_addr` 宽 `RP*RAW=9` 位、`rd_data` 宽 `RP*RW=48` 位。把每个端口的位宽列成表。
2. 设定一个场景：周期 N，`wr_valid=2'b11`，`wr_addr=6'b001_000`（写口 0 地址=0，写口 1 地址=1），`wr_data=32'hAAAA_BBBB`（写口 0 数据=0xBBB，写口 1 数据=0xAAAA）。
3. 在纸上跟踪 [stdlib/rtl/oh_regfile.v:40-64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_regfile.v#L40-L64)：`write_en[0][0]`、`write_en[1][1]` 应为 1，其余为 0；`mem[0]` 在时钟沿后应变为 `0xBBBB`，`mem[1]` 应变为 `0xAAAA`。
4. 接着设周期 N+1：`rd_valid=3'b111`，`rd_addr=9'b001_000_000`（读口 0 地址=0，读口 1 地址=1，读口 2 地址=4）。

预期结果：

- 周期 N+1，`rd_data` 的第 0 段（48 位里的 [15:0]）=`0xBBBB`，第 1 段（[31:16]）=`0xAAAA`，第 2 段（[47:32]）=`0x0000`（寄存器 4 未写过，初值为 `x` 或 0，取决于仿真初值——「待本地验证」具体是 `x` 还是 0）。
- 通过这条跟踪你应该能说清：`oh_mux` 在这里只处理「同一寄存器被多写口命中」的冲突选择，而不会丢失任何「不同寄存器被不同写口同时写」的事务。

#### 4.3.5 小练习与答案

**练习 1**：`oh_regfile` 的读口是组合读（`assign`），没有时钟。如果想要「读数据在下一拍才出来」（像 `oh_dpram` 的 `REG=1`），应该怎么改？

**参考答案**：把 `rd_data` 的每一段先接到一个 `reg`，再用 `always @(posedge clk) if(rd_valid[i]) rd_reg[i] <= mem[rd_addr...];`，最后 `assign rd_data[i*RW+:RW] = rd_reg[i];`。即给读路径加一级寄存器，代价是多一拍读延迟和面积。

**练习 2**：为什么 `oh_regfile` 用拼接总线（一根 `wr_data[WP*RW-1:0]`）而不是给每个写口单独命名 `din0/din1/...`？

**参考答案**：因为端口数 `WP`/`RP` 是参数化的。用拼接总线后，端口增减只需改位宽计算，端口列表本身不变，模块的对外接口保持稳定；单独命名则无法用参数表达可变端口数。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「读-改-写缓冲」小任务（纸上设计 + 可选仿真）：

**场景**：你要做一个小缓冲，深度 16、位宽 8，要求能**按位**更新（只改指定 bit，其余保留），并能在任意时刻读出当前内容。

要求：

1. **选型**：在 `oh_dpram` / `oh_ram` / `oh_regfile` 三者中选一个，并说明理由。
   - 参考思路：深度 16、单读单写、需要按位更新 → `oh_dpram`（或 `oh_ram`）配合 `wr_wem` 掩码即可；若端口数很少但希望读组合立即出，`oh_regfile` 也行但杀鸡用牛刀。
2. **参数**：写出所选模块的 `#(...)` 参数取值（注意 `AW` 是派生的，不要手填）。
3. **按位更新**：写一段简短文字或伪代码，说明如何用 `wr_wem` 实现「只把地址 3 的 bit2 翻转」而不影响其他位。（提示：先读回旧值，算出新值，再配合全 1 掩码写入；`oh_dpram` 本身不支持「读改写原子操作」，需要外部逻辑。）
4. **soft/hard**：说明本任务必须用 soft（`TARGET="DEFAULT"` 或 `SYN="TRUE"`），因为 hard 宏在仓库里是桩；并指出如果你例化的是 `oh_fifo_sync` 而不是裸 `oh_dpram`，参数名要对齐（见 4.2）。
5. **（可选）仿真**：仿照 4.1.4 的 testbench 风格，写一个最小激励，验证「写 0x03 到地址 3 → 读回 → 在外部算出 0x07（翻转 bit2）→ 再写 → 读回应为 0x07」。

> 这个任务覆盖了：双口 RAM 的读写口与地址译码（4.1）、`wem` 位掩码与 hard 参数的现实约束（4.2）、以及寄存器堆与 RAM 的选型判断（4.3）。

---

## 6. 本讲小结

- **`oh_dpram`** 是 1 写口 + 1 读口的真双口 RAM：同步写、组合读（`REG=0`）或同步读（`REG=1`），地址位宽 `AW=$clog2(DEPTH)` 派生，写口带逐位使能 `wr_wem`。`oh_ram` 是它的单口版。
- **soft/hard 切换**靠一组字符串参数 + `generate if`：`SYN`/`TYPE`（`oh_ram`、`oh_fifo_sync` 用）与 `TARGET`（`oh_dpram` 用）、以及 `SHAPE`（物理版图长宽比）。BIST/电源/修复端口只在 hard 模式有意义。
- **重要真相**：`oh_fifo_sync` 向 `oh_dpram` 传了 `.SYN()/.TYPE()`，但 `oh_dpram` 只有 `TARGET`，两者参数名不对齐；且 `asic_memory_dp`/`asic_memory_sp` 在仓库里无定义，hard 分支是占位桩。学习时以被例化模块的实际参数表为准。
- **`oh_regfile`** 是多读多写的寄存器堆：用 one-hot 写使能译码 + `oh_mux` 处理多写口冲突，端口数据走拼接总线以支持参数化端口数；读口组合读、用 `rd_valid` 做掩码。
- **选型直觉**：大量少端口 → `oh_dpram`/`oh_ram`；少量多端口 → `oh_regfile`。
- 仓库没有现成的 RAM/寄存器堆测试台，本讲配了最小 testbench 范例（4.1.4）与源码阅读型实践（4.2.4、4.3.4）。

---

## 7. 下一步学习建议

本讲把「存储体」讲清楚了。存储体本身不管理「满/空」，它只是被动的阵列。把存储体包上**读写指针 + 满空判断**就成了 FIFO——这正是下一讲 [u3-l2 FIFO 设计：同步、异步与跨时钟域](u3-l2-fifo-design.md) 的主题，里面会直接用本讲的 `oh_fifo_sync`（以及它内部的 `oh_dpram`）做同步 FIFO，并升级到基于格雷码指针的异步 FIFO。

建议你在进入下一讲前：

1. 跑通 4.1.4 的 `oh_dpram` testbench，亲手看到「写后读」的正确返回值。
2. 回顾 [stdlib/rtl/oh_fifo_sync.v:50-105](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L50-L105) 的 FIFO 控制部分（`wr_addr`/`rd_addr`/`wr_count`/`wr_full`/`rd_empty`），先自己想想满空条件怎么写，再去和源码对照。
3. 如果你对 one-hot 选择器还不够熟，回头做一下 [u2-l1](u2-l1-combinational-primitives.md) 的练习——`oh_regfile` 和后续很多模块都依赖它。
