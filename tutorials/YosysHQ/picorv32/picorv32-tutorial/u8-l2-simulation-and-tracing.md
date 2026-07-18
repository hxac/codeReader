# 仿真测试台与执行追踪

## 1. 本讲目标

本讲把视角从「CPU 内部」转到「CPU 外面围着一圈什么」。读完本讲，你应该能够：

- 画出 `testbench.v` 的三层结构（`testbench` → `picorv32_wrapper` → `axi4_memory` + 被测核 `picorv32_axi`），并说清每一层各负责什么。
- 解释 `+axi_test` 这个 plusarg 如何用一颗 xorshift 伪随机数发生器给 AXI 总线注入随机延迟，从而压测 CPU 总线接口的健壮性。
- 说清同一套 RTL 如何既被 Icarus Verilog 仿真，又被 Verilator 编译成 C++ 仿真，以及 `testbench.cc` 在其中扮演的角色。
- 看懂 36 位 `trace_data` 的打包格式（标志位 + 32 位载荷），并用 `showtrace.py` 把二进制 trace 反解成与 `objdump` 对照的可读执行流。

## 2. 前置知识

本讲默认你已经读过以下两讲：

- **u1-l3 跑起来：最小测试台 testbench_ez**：那里讲过一个最小测试台如何用 `always #5 clk=~clk` 造时钟、用 `reg [31:0] memory[0:255]` 兼作指令与数据存储、用 `mem_valid/mem_ready/mem_wstrb` 三件套与 CPU 握手。本讲的 `testbench.v` 是它的「豪华版」：内存更大、接的是 AXI 变体、还自带随机延迟与自检。
- **u4-l2 主状态机 cpu_state**：那里讲过 `cpu_state_trap` 是不可恢复的死锁状态，靠 `trap` 端口对外暴露。本讲的测试台正是用 `trap` 来判断「程序跑完了」。
- 此外，u5-l3 讲过的原生内存接口（valid-ready 握手、`mem_wstrb` 字节写使能、`mem_instr`）和 u7-l1 讲过的 AXI4-Lite 五通道（AW/W/B/AR/R）是理解 `axi4_memory` 的前提。

几个术语先约定：

| 术语 | 含义 |
|---|---|
| **测试台 (testbench)** | 不被综合、只为仿真存在的 Verilog 顶层，负责造时钟、复位、喂激励、检查结果。 |
| **DUT (Device Under Test)** | 被测器件，这里就是 `picorv32_axi` 核。 |
| **plusarg** | 仿真启动时从命令行传入的参数，如 `+vcd`、`+trace`、`+axi_test`，用 `$test$plusargs` / `$value$plusargs` 在 Verilog 里读取。 |
| **自检 (self-checking)** | 测试台自己判断对错：固件向约定地址写魔术数即视为通过，CPU 触发 `trap` 时据此打印 PASS/FAIL。 |
| **xorshift** | 一类轻量伪随机数算法，只用若干次异或与移位就能产生新随机数，适合硬件/仿真里做随机延迟源。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [testbench.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v) | 主测试台。含三个模块：`testbench`（时钟/复位/VCD/trace 文件）、`picorv32_wrapper`（封装被测核 + 中断激励 + 通过判定）、`axi4_memory`（带随机延迟的 AXI4-Lite 从端内存模型 + UART/自检地址译码）。 |
| [testbench_wb.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v) | Wishbone 版测试台。结构与 AXI 版对称，被测核换成 `picorv32_wb`，内存模型换成 `wb_ram`。 |
| [testbench.cc](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.cc) | Verilator 的 C++ 驱动。在 Verilator 流程中顶替 Verilog 的 `testbench` 模块，承担时钟生成、复位、VCD 与 trace 文件写出。 |
| [showtrace.py](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py) | trace 解码器。读取 `testbench.trace` 的 36 位十六进制行，结合 `objdump -d` 的反汇编，输出「人类可读的逐指令执行流」。 |
| [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile) | 把上述文件串起来：`test/test_vcd/test_axi/test_wb/test_verilator` 等目标各对应一套编译规则与 plusarg 组合。 |

此外会引用 [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) 中产生 `trace_data` 的那几段代码，用来解释 trace 的打包格式。

## 4. 核心概念与源码讲解

### 4.1 AXI4 自检测试台：testbench.v 的三层结构

#### 4.1.1 概念说明

`testbench_ez.v`（u1-l3）教会我们「最小闭环」：时钟、复位、一块内存、一个被测核。但它有四个不足：内存只有 256 字、接的是裸 `picorv32` 原生接口、没有中断激励、跑完不报告对错。

`testbench.v` 是面向真实回归测试的「完整版」，它要回答四个问题：

1. **接哪个核？** —— 接 AXI4-Lite 变体 `picorv32_axi`（注意不是裸核）。
2. **挂什么外设？** —— 一块 128 KB 的 AXI4-Lite 内存模型，兼作指令/数据存储，并在地址 `0x1000_0000` / `0x2000_0000` 处仿真 UART 输出与「测试通过」标志。
3. **怎么测中断？** —— 用一个 16 位自由计数器，周期性地拉起 `irq[4]` / `irq[5]`，自动给 CPU 喂中断。
4. **怎么判对错？** —— 固件跑完向 `0x2000_0000` 写魔术数 `123456789` 即置 `tests_passed`；CPU 进入 `trap` 时据此打印 `ALL TESTS PASSED.` 或 `ERROR!`。

为了可读性，`testbench.v` 把这些职责拆成三个嵌套的 Verilog 模块：外层 `testbench` 管仿真环境（时钟、复位、文件），中层 `picorv32_wrapper` 把被测核和激励/检查粘到一起，内层 `axi4_memory` 是那个聪明的从端内存模型。

#### 4.1.2 核心流程

一次 `make test` 的运行流程大致是：

```text
make test
  └─ iverilog 编译 testbench.v + picorv32.v → testbench.vvp
  └─ vvp testbench.vvp
       ├─ 0~100 拍: resetn=0, CPU 复位, reg_pc<=0
       ├─ 100 拍后: resetn=1, CPU 开始从地址 0 取指
       │    ├─ 取指/访存 → axi4_memory 应答 (随机延迟与否看 +axi_test)
       │    ├─ 固件向 0x1000_0000 写字符 → axi4_memory 打印到 stdout
       │    ├─ count_cycle 溢出 → irq[4]/irq[5] 拉起 → CPU 进中断处理
       │    └─ 固件向 0x2000_0000 写 123456789 → tests_passed=1
       └─ CPU 执行 ebreak/非法指令 → trap=1
            ├─ tests_passed==1 → 打印 "ALL TESTS PASSED." → $finish
            └─ tests_passed==0 → 打印 "ERROR!" → $stop (除非 +noerror)
```

注意：`trap` 是终止仿真的唯一正常出口；如果固件卡死，外层还有「跑满 1,000,000 个时钟就 `TIMEOUT` 并 `$finish`」的看门狗。

#### 4.1.3 源码精读

**(a) 外层 `testbench`：时钟、复位、VCD、trace 文件**

[testbench.v:10-64](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L10-L64) 用 `ifndef VERILATOR` 包起来——这一点很关键，下一节会展开。它的核心是四个 `initial` / `always` 块：

```verilog
always #5 clk = ~clk;                       // 10ns 周期时钟
initial begin repeat (100) @(posedge clk); resetn <= 1; end   // 复位 100 拍
initial begin
  if ($test$plusargs("vcd")) begin $dumpfile("testbench.vcd"); $dumpvars(0, testbench); end
  repeat (1000000) @(posedge clk); $display("TIMEOUT"); $finish;   // 看门狗
end
initial begin
  if ($test$plusargs("trace")) begin
    trace_file = $fopen("testbench.trace", "w");
    while (!trap) begin @(posedge clk); if (trace_valid) $fwrite(trace_file, "%x\n", trace_data); end
    ...
  end
end
```

要点：

- `resetn` 低有效，前 100 个上升沿保持 0（复位活跃），第 101 拍置 1 释放——与 u1-l3 里 `testbench_ez.v` 的做法一致。
- `+vcd` 才波形、`+trace` 才写 trace 文件——这两个开关默认关，避免常规回归测试产生大文件。
- trace 只在 `trap` 拉起前写，且每拍检查 `trace_valid`，命中才写一行 `%x`（36 位 `trace_data` 的十六进制）。

**(b) 中层 `picorv32_wrapper`：被测核 + 中断激励 + 通过判定**

中断是用一个 16 位计数器造出来的 [testbench.v:80-87](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L80-L87)：

```verilog
reg [15:0] count_cycle = 0;
always @(posedge clk) count_cycle <= resetn ? count_cycle + 1 : 0;
always @* begin
  irq = 0;
  irq[4] = &count_cycle[12:0];   // 13 位全 1, 每 8192 拍拉一次
  irq[5] = &count_cycle[15:0];   // 16 位全 1, 每 65536 拍拉一次
end
```

`&count_cycle[12:0]` 是「归约与」——只有当低 13 位全为 1 时才返回 1。于是 `irq[4]` 每 \(2^{13}=8192\) 拍拉起一个单周期脉冲，`irq[5]` 每 \(2^{16}=65536\) 拍拉起一次。这两个外部中断源自动反复触发 CPU 的中断处理路径，使固件里的 IRQ 测试代码（见 u6-l2）得以被覆盖。

被测核是 `picorv32_axi`（**注意：是 AXI 变体，不是裸 `picorv32`**），并显式开启了乘除法、中断与 trace [testbench.v:163-176](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L163-L176)：

```verilog
picorv32_axi #(
  `ifdef COMPRESSED_ISA .COMPRESSED_ISA(1), `endif
  .ENABLE_MUL(1), .ENABLE_DIV(1), .ENABLE_IRQ(1), .ENABLE_TRACE(1)
) uut ( ... );
```

通过判定在 `trap` 拉起那一刻检查 [testbench.v:256-274](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L256-L274)：

```verilog
if (resetn && trap) begin
  $display("TRAP after %1d clock cycles", cycle_counter);
  if (tests_passed)  $display("ALL TESTS PASSED.");  $finish;   // 通过
  else begin $display("ERROR!"); if ($test$plusargs("noerror")) $finish; else $stop; end
end
```

`$finish` 是正常退出，`$stop` 会把仿真器停在一个可交互的断点（便于排查），而 `+noerror` 会让失败也走 `$finish`（便于脚本里串联跑）。

**(c) 内层 `axi4_memory`：128 KB 内存 + UART + 自检 + 随机延迟**

这块内存用 `tests_passed` 与 128 KB 数组 [testbench.v:308](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L308)：

```verilog
reg [31:0] memory [0:128*1024/4-1] /* verilator public */;
```

`/* verilator public */` 注释让 Verilator 把这个数组暴露成 C++ 可访问符号（`Vpicorv32_wrapper::axi4_memory::memory`），这正是外层能 `$readmemh(firmware_file, mem.memory)` 把固件灌进来的原因 [testbench.v:249-254](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L249-L254)。

写事务的处理在 `handle_axi_bvalid` 任务里集中体现了「内存 / UART / 自检地址」三路译码 [testbench.v:396-428](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L396-L428)：

```verilog
if (latched_waddr < 128*1024) begin              // 普通内存: 按 wstrb 字节写入
  ...按 latched_wstrb[3:0] 选择性写 4 个字节...
end else
if (latched_waddr == 32'h1000_0000)              // UART: 低字节当 ASCII 打印
  $write("%c", latched_wdata[7:0]);
else if (latched_waddr == 32'h2000_0000) begin   // 自检: 写魔术数 = 通过
  if (latched_wdata == 123456789) tests_passed = 1;
end else begin $display("OUT-OF-BOUNDS ..."); $finish; end
```

这与 u2-l2 讲过的固件约定完全吻合：`0x1000_0000` 是 UART、`0x2000_0000` 是「测试通过」魔术数端口。

**(d) 随机延迟：`+axi_test` 的核心机制**

这是本模块最值得读的一段。随机数来自一颗 xorshift64 [testbench.v:324-344](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L324-L344)：

```verilog
reg [63:0] xorshift64_state = 64'd88172645463325252;
task xorshift64_next; begin
  xorshift64_state = xorshift64_state ^ (xorshift64_state << 13);
  xorshift64_state = xorshift64_state ^ (xorshift64_state >>  7);
  xorshift64_state = xorshift64_state ^ (xorshift64_state << 17);
end endtask
always @(posedge clk) if (axi_test) begin
  xorshift64_next;
  {fast_axi_transaction, async_axi_transaction, delay_axi_transaction} <= xorshift64_state;
end
```

`axi_test` 由 `+axi_test` plusarg（或 `AXI_TEST` 参数）置位。一旦置位，每拍推进一次随机状态，并把 64 位随机数拆成三段分别灌进三组控制位：

| 寄存器组 | 宽度 | 控制的行为 |
|---|---|---|
| `fast_axi_transaction` | 3 位 | 某笔事务是否「当拍成交」（不等下一拍） |
| `async_axi_transaction` | 5 位 | 是否在 `negedge` 异步提前成交（见下） |
| `delay_axi_transaction` | 5 位 | 是否**故意推迟**本笔事务（插入空闲拍） |

这三位一组分别控制 AXI 五个通道里五种事件（AR/AW/W 地址与数据、R 读返回、B 写响应）。于是每个通道的每次握手都可能被随机地「立刻成交 / 延后成交 / 干脆这拍不成交」，从而制造出千奇百怪的总线时序。

成交时机分两个 `always` 块实现，体现「同步」与「异步」两条路径：

- **异步路径**（`negedge`）[testbench.v:430-436](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L430-L436)：在时钟下降沿检查各 `valid`，若对应 `async_axi_transaction` 位为 1 则立刻应答，使事务在同一个时钟周期内就握手完成（最快的极限时序）。
- **同步路径**（`posedge`）[testbench.v:472-477](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L472-L477)：若 `delay_axi_transaction` 对应位为 1，则本拍**故意不处理**这笔事务，相当于插入一个等待周期。

```verilog
// 同步路径: delay 位为 1 时这拍跳过该事务 (制造延迟)
if (mem_axi_arvalid && !(latched_raddr_en || fast_raddr) && !delay_axi_transaction[0]) handle_axi_arvalid;
```

把这些随机化合在一起，效果就是：**同一个固件，每次 `make test_axi` 都会跑出不同的总线时序**。如果 CPU 的 AXI 适配器有任何「假设 ready 马上来」「假设写数据先于写地址」之类的隐含前提，迟早会被某次随机组合戳穿。这正是用随机延迟做总线压测的价值。

#### 4.1.4 代码实践

**实践目标**：亲眼看 `+axi_test` 如何改变总线行为，并验证即便有随机延迟，固件依然能通过。

**操作步骤**：

1. 先跑无随机延迟的基线：`make test`，记下输出末尾的 `TRAP after N clock cycles` 与 `ALL TESTS PASSED.`，记下周期数 N。
2. 再跑带随机延迟的版本：`make test_axi`，记下新的周期数 N'。
3. 想看每笔总线事务：`make test_axi` 改成手动加 verbose——阅读 [testbench.v:309-313](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L309-L313) 会发现 `verbose` 由 `+verbose` 置位，于是可以 `vvp testbench.vvp +axi_test +verbose`，观察 `RD:`/`WR:` 行里同一地址被不同延迟应答的过程。

**需要观察的现象**：

- 两次都应打印 `ALL TESTS PASSED.`——说明 CPU 的 AXI 接口对随机延迟是健壮的。
- 周期数 N' 通常大于 N——随机延迟让总线事务平均变慢，从而整段固件耗时增加。

**预期结果**：`make test` 与 `make test_axi` 均 PASS；`N' ≥ N`。

> 若本地未安装 iverilog 或未构建固件，则周期数与具体输出**待本地验证**；但「随机延迟使总周期数增加」这一趋势是确定的。

#### 4.1.5 小练习与答案

**练习 1**：把 `+axi_test` 关掉后，`xorshift64_next` 还会被调用吗？`fast/async/delay_axi_transaction` 会是什么值？

**答案**：不会。[testbench.v:339-344](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L339-L344) 把 `xorshift64_next` 包在 `if (axi_test)` 里，关掉后随机状态冻结。三组寄存器保持初始值 `fast=~0`、`async=~0`、`delay=0`（见 [testbench.v:335-337](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L335-L337)）：即「尽量快成交、从异步路径成交、绝不延迟」——最快的总线，故 N 最小。

**练习 2**：为什么把随机延迟检查放在 `negedge` 与 `posedge` 两个 `always` 块里，而不是只用一个？

**答案**：分两个沿可以覆盖两种极端：`negedge` 路径模拟「从端在同一个时钟周期内就给出 ready」（组合直通、最快）；`posedge` 路径模拟「从端寄存输出、可能延迟若干拍」（最常见、最慢）。CPU 的 AXI 适配器必须对这两种以及介于其间的所有时序都正确，所以测试台要能把它们都生成出来。

**练习 3**：如果固件有 bug，向 `0x2000_0000` 写了 `123456780`（差一个 9），`make test` 会怎样？

**答案**：`tests_passed` 不会被置位（[testbench.v:418-420](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L418-L420) 要求严格等于 `123456789`）。等 CPU 进入 `trap`，[testbench.v:264-272](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L264-L272) 走 `else` 分支打印 `ERROR!` 并 `$stop`（除非加了 `+noerror`）。

---

### 4.2 Verilator 流程：用 C++ 驱动同一份 RTL

#### 4.2.1 概念说明

Icarus Verilog（iverilog）是「解释型」仿真器：它把 Verilog 编译成 `.vvp` 字节码，由 `vvp` 解释执行，`initial` 块、`$display`、`$dumpvars` 都按 Verilog 语义跑。它上手快、对 SystemVerilog 行为建模友好，但慢。

Verilator 是「编译型」仿真器：它把可综合的 Verilog **直接翻译成 C++**，再编进一个原生可执行程序。它快一两个数量级，但代价是——它**不仿真 `initial` 块里那些测试台风格的系统任务**（`$dumpvars`、`while(!trap) @(posedge clk)` 等）。于是 Verilator 流程里，测试台的「环境」部分（时钟、复位、VCD、trace）必须用 **C++ 重写**。

PicoRV32 用一个很巧妙的办法让两种仿真器**共用同一份 `testbench.v`**：把纯仿真风格的 `testbench` 模块用 `` `ifndef VERILATOR `` 包起来。这样：

- **iverilog**：`VERILATOR` 未定义 → `testbench` 模块在 → 它就是顶层，一切照 Verilog 跑。
- **Verilator**：`VERILATOR` 已定义 → `testbench` 模块被剥掉 → 顶层换成 `picorv32_wrapper`（它没被包在 ifndef 里），环境职责由 `testbench.cc` 接管。

于是同一份 `testbench.v` + `picorv32.v`，既能被 iverilog 解释，又能被 Verilator 编译——只是「谁是顶层、谁来造时钟」不同。

#### 4.2.2 核心流程

`make test_verilator` 的流程 [Makefile:81-85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L81-L85)：

```text
verilator --cc --exe -Wno-lint --trace --top-module picorv32_wrapper \
          testbench.v picorv32.v testbench.cc --Mdir testbench_verilator_dir
make -C testbench_verilator_dir -f Vpicorv32_wrapper.mk
cp testbench_verilator_dir/Vpicorv32_wrapper testbench_verilator
./testbench_verilator
```

- `--cc`：把 RTL 翻译成 C++（生成 `Vpicorv32_wrapper.h/.cpp`）。
- `--exe` + `testbench.cc`：把 C++ 驱动一起链进可执行文件。
- `--trace`：启用 VCD 输出能力（生成 `verilated_vcd_c.h`）。
- `--top-module picorv32_wrapper`：**显式指定顶层是被剥掉 `testbench` 后剩下的 `picorv32_wrapper`**。

Verilator 自动把 `testbench.cc` 里对 `top->clk`、`top->resetn`、`top->trace_valid`、`top->trace_data` 的访问映射成 `picorv32_wrapper` 模块的端口——因为它们就是该模块的端口（见 [testbench.v:70-76](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L70-L76)）。

#### 4.2.3 源码精读

`testbench.cc` 是一个极简的 C++ 主循环 [testbench.cc:1-43](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.cc#L1-L43)：

```cpp
Vpicorv32_wrapper* top = new Vpicorv32_wrapper;

VerilatedVcdC* tfp = NULL;
if (flag_vcd && 0==strcmp(flag_vcd, "+vcd")) {        // +vcd 才开 VCD
  Verilated::traceEverOn(true); tfp = new VerilatedVcdC; top->trace(tfp, 99); tfp->open("testbench.vcd");
}
FILE *trace_fd = NULL;
if (flag_trace && 0==strcmp(flag_trace, "+trace"))    // +trace 才写 trace
  trace_fd = fopen("testbench.trace", "w");

top->clk = 0; int t = 0;
while (!Verilated::gotFinish()) {                     // 直到 $finish 才停
  if (t > 200) top->resetn = 1;                       // 前 200 个半拍不复位
  top->clk = !top->clk;                               // 翻转时钟
  top->eval();                                        // 求值一拍
  if (tfp) tfp->dump(t);                              // VCD 采样
  if (trace_fd && top->clk && top->trace_valid) fprintf(trace_fd, "%9.9lx\n", top->trace_data);
  t += 5;
}
```

把它和 Verilog 版的 `testbench` 模块逐行对照，会看到一一对应：

| 职责 | Verilog `testbench` 模块 | C++ `testbench.cc` |
|---|---|---|
| 造时钟 | `always #5 clk = ~clk;` | `top->clk = !top->clk; t += 5;` |
| 复位 100 拍 | `repeat(100) @(posedge clk); resetn<=1;` | `if (t > 200) top->resetn = 1;`（200 个半拍 = 100 个周期） |
| VCD | `$dumpfile/$dumpvars` 受 `+vcd` 控制 | `VerilatedVcdC` 受 `+vcd` 控制 |
| 写 trace | `$fwrite(...,"%x\n",trace_data)` | `fprintf(...,"%9.9lx\n", top->trace_data)` |
| 停机 | `$finish` | `while(!Verilated::gotFinish())` 检测 `$finish` |

**两个值得注意的细节**：

1. **trace 写法略有不同**：Verilog 用 `%x`（不定位宽），C++ 用 `%9.9lx`（恰好 9 位十六进制，对应 36 位）。`showtrace.py` 里用 `line.replace("x","0")` 把 Verilog 可能输出的 `x`（不定值）容错成 `0`，故两种写法都能被解码。
2. **复位极性**：C++ 版前 200 个**半拍**（`t` 每次加 5，一个完整时钟周期是两个半拍）保持 `resetn=0`，等价于 Verilog 版「100 个上升沿」。两者语义一致：先复位 100 周期再释放。

另外，`axi4_memory` 内部的 `$fflush();`（无参数版本）也用 `` `ifndef VERILATOR `` 保护 [testbench.v:413-415](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L413-L415)——因为 Verilator 不支持无参数 `$fflush`，必须屏蔽。

#### 4.2.4 代码实践

**实践目标**：对比 iverilog 与 Verilator 在同一固件上的运行速度。

**操作步骤**：

1. `make test` 计时：`time make test`，记录 real 时间。
2. `make test_verilator` 计时：先构建（`make testbench_verilator`）再 `time ./testbench_verilator`。
3. （可选）验证两者产物一致：分别给两者加 `+vcd`，用 GTKWave 打开 `testbench.vcd`，对比前若干拍的信号波形是否相同。

**需要观察的现象**：两者最终都打印 `ALL TESTS PASSED.`；Verilator 版的 real 时间显著短于 iverilog 版（通常快一到两个数量级）。

**预期结果**：行为一致、Verilator 更快。

> Verilator 是否预装、具体加速比**待本地验证**；Verilator 快于 iverilog 是普遍结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `testbench.cc` 里检查的是 `top->clk && top->trace_valid`（两个都为真）才写 trace，而不是只检查 `trace_valid`？

**答案**：Verilator 的 `eval()` 每个半拍调用一次，`top->clk` 在高低之间翻转。trace 应当只在**上升沿**采样（与 Verilog 版 `@(posedge clk)` 对应），故额外要求 `top->clk` 为真，避免在下降沿重复写一行。

**练习 2**：如果删掉 `testbench.v` 第 10 行的 `` `ifndef VERILATOR `` 与第 65 行的 `` `endif ``，Verilator 流程会怎样？

**答案**：`testbench` 模块会进入 Verilator 编译。但该模块含 `initial`/`$dumpvars`/`@(posedge clk)` 等 Verilator 对 testbench 风格支持有限或需特殊处理的构造，且现在同时存在 `testbench` 与 `picorv32_wrapper` 两个候选顶层，`--top-module picorv32_wrapper` 还会与之冲突。总之会让 Verilator 编译失败或行为异常——这正是用 ifndef 把它挡掉的初衷。

**练习 3**：`%9.9lx` 里的两个 `9` 分别是什么意思？为什么恰好是 9 位十六进制？

**答案**：`%9.9lx` 表示输出 `long` 十六进制，**最少 9 位、且精度 9 位**（不足补 0、不截断）。36 位 / 4 = 9 个十六进制位，故 9 位恰好装下整个 `trace_data`，与 Verilog 的 36 位 `%x` 对齐。

---

### 4.3 trace 解码：从 36 位 trace_data 到可读反汇编

#### 4.3.1 概念说明

CPU 跑起来后，你怎么知道它「到底执行了哪条指令、跳到哪、读了哪个地址」？波形（VCD）能看所有信号，但信息过载；`$display` 能打印，但要在 RTL 里到处插。PicoRV32 选了第三条路：**用一对专用端口 `trace_valid` / `trace_data` 在每条指令提交时吐出一个 36 位记录**，测试台把它逐行写进 `testbench.trace`，再由 `showtrace.py` 离线解码。

关键是这 36 位是**打包的**：低 32 位是「载荷」（PC、地址或寄存器值），高 4 位是「这行是什么类型」的标志。`showtrace.py` 不需要懂 CPU 内部，只需要：

1. 用 `objdump -d` 把固件反汇编成「地址 → 指令」字典；
2. 顺着 trace 行维护一个「当前 PC」游标，用分支记录重定位游标，把每条 trace 行贴到对应的那条反汇编指令上。

这样输出就是「地址 | 指令编码 | 反汇编 | 本次 trace 载荷」四列对齐的可读流，调试时一目了然。

#### 4.3.2 核心流程

trace 从产生到可读的完整链路：

```text
picorv32 (ENABLE_TRACE=1)
  每条指令提交时拉一拍 trace_valid, 同时给出 36 位 trace_data
      │
      ▼
testbench.v / testbench.cc
  while(!trap) 每拍 if(trace_valid) 写一行 "%x" 到 testbench.trace
      │
      ▼
testbench.trace  (纯文本, 每行一个 9 位十六进制数)
      │
      ▼
showtrace.py testbench.trace firmware/firmware.elf
  ├─ 跑 riscv32-unknown-elf-objdump -d firmware.elf 建 insns{addr:(opcode,desc)}
  └─ 逐行解析 36 位: 拆 payload/irq/addr/branch, 用 pc 游标贴反汇编
      │
      ▼
可读执行流 (IRQ/>目标地址/@访存地址/=寄存器值  +  四列反汇编)
```

36 位 `trace_data` 的打包格式（来自 [picorv32.v:171-173](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L171-L173)）：

```verilog
localparam [35:0] TRACE_BRANCH = {4'b 0001, 32'b 0};   // bit32 = 这是分支, 载荷=目标PC
localparam [35:0] TRACE_ADDR   = {4'b 0010, 32'b 0};   // bit33 = 这是访存地址, 载荷=地址
localparam [35:0] TRACE_IRQ    = {4'b 1000, 32'b 0};   // bit35 = 当前在中断处理中
```

| 位 | 名称 | 为 1 时含义 |
|---|---|---|
| bit 35 | TRACE_IRQ | 本条 trace 处于中断处理上下文（`irq_active`） |
| bit 34 | （未用） | 保留 |
| bit 33 | TRACE_ADDR | 载荷是一个访存地址（load/store） |
| bit 32 | TRACE_BRANCH | 载荷是分支/跳转的目标 PC |
| bit 31:0 | payload | 32 位载荷（PC / 地址 / 寄存器写回值） |

若 bit33 与 bit32 **都为 0**，则载荷是「本指令写回寄存器的值」。于是每条指令的 trace 行天然分三类：`>` 分支目标、`@` 访存地址、`=` 寄存器值。

#### 4.3.3 源码精读

**(a) CPU 端：何时、如何打包 trace_data**

trace 端口声明为 36 位寄存器 [picorv32.v:158-159](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L158-L159)。主提交点在 `cpu_state_fetch` 里，由 `latched_trace` 触发 [picorv32.v:1517-1524](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1517-L1524)：

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

读法：

- 分支指令（`latched_branch`）→ 载荷是**目标地址** `current_pc`（最低位清零，因为地址按字对齐）。
- 非分支 → 载荷是**写回值**：ALU/移位结果（`latched_stalu ? alu_out_q`）或访存/其他结果（`reg_out`）。
- `irq_active` 决定是否或上 `TRACE_IRQ`，使整条 trace 都打上「在中断里」的标记。

load/store 还会**额外**发一条 `TRACE_ADDR` 行，载荷是计算出的访存地址 `reg_op1 + decoded_imm`：

- store 在 `cpu_state_stmem` [picorv32.v:1865-1868](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1865-L1868)
- load 在 `cpu_state_ldmem` [picorv32.v:1893-1896](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1893-L1896)

```verilog
trace_data <= (irq_active ? TRACE_IRQ : 0) | TRACE_ADDR | ((reg_op1 + decoded_imm) & 32'hffffffff);
```

所以一条 load 指令会在 trace 里出现**两行**：先 `@` 访存地址，再 `=` 装载到的值——这是读 trace 时要注意的特点。

**(b) 解码端：showtrace.py**

第一步，用 `objdump -d` 建地址→指令字典 [showtrace.py:8-15](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L8-L15)：

```python
with subprocess.Popen(["riscv32-unknown-elf-objdump", "-d", elf_filename], stdout=subprocess.PIPE) as proc:
  ...
  match = re.match(r'^\s*([0-9a-f]+):\s+([0-9a-f]+)\s*(.*)', line)
  if match: insns[int(match.group(1), 16)] = (int(match.group(2), 16), match.group(3).replace("\t", " "))
```

每行反汇编形如 `   1000: 00002883  lb a6,16(a6)`，正则抓出地址、编码、助记符，存进 `insns`。

第二步，逐行解析 36 位并维护 `pc` 游标 [showtrace.py:21-27](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L21-L27)：

```python
raw_data = int(line.replace("x", "0"), 16)      # 把不定值 x 容错成 0
payload  = raw_data & 0xffffffff                 =  bit31:0
is_branch = (raw_data & 0x100000000) != 0        →  bit32  (TRACE_BRANCH)
is_addr   = (raw_data & 0x200000000) != 0        →  bit33  (TRACE_ADDR)
irq_active= (raw_data & 0x800000000) != 0        →  bit35  (TRACE_IRQ)
info = "%s %s%08x" % ("IRQ" if irq_active or last_irq else "   ",
                      ">" if is_branch else "@" if is_addr else "=", payload)
```

这套掩码与 `picorv32.v` 的 `TRACE_*` localparam 完全对应（`0x1_0000_0000`=bit32、`0x2_0000_0000`=bit33、`0x8_0000_0000`=bit35），这是「打包端与解码端必须对齐」的契约。

第三步，pc 游标的推进规则是核心 [showtrace.py:32-63](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L32-L63)：

```python
if pc >= 0 and pc in insns:
  ...打印 insn...
  if not is_addr:                                # 访存地址行不推进 pc
    pc += 4 if (insn_opcode & 3) == 3 else 2     # 普通指令 +4, 压缩指令 +2
...
if is_branch:                                    # 分支: 跳到目标
  pc = payload
```

四个关键点：

1. **`pc` 初值是 -1**（「尚未与指令流同步」）。只有遇到第一条分支记录后才真正开始解码，之前都打印 `SKIPPING DATA UNTIL NEXT BRANCH`。
2. **压缩 vs 普通**：`(insn_opcode & 3) == 3` 是 RISC-V 判断「32 位指令」的规则——低 2 位为 `11` 表示 32 位指令，否则是 16 位压缩指令，故步进分别是 4 与 2（呼应 u7-l2 讲的 C 扩展）。
3. **`is_addr` 不推进 pc**：因为 load/store 会发两行（地址 + 值），只有「值」那一行（非 addr）才代表「这条指令执行完了，该去下一条」。
4. **`is_branch` 把 pc 设成载荷**：分支的目标地址，由此重建控制流。

第四步，两个特例：

- **中断入口**：检测到 `irq_active` 从无到有，把 `pc` 强制设为 `0x10` [showtrace.py:29-30](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L29-L30)。这个 `0x10` 不是硬编码魔数，而是 [picorv32.v:87](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L87) 的 `PROGADDR_IRQ = 32'h 0000_0010`——CPU 进入中断后跳到的那条指令的地址。
- **`retirq`**：[showtrace.py:37-39](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L37-L39) 把编码 `0x0400000b` 手动识别为 `retirq`（u6-l2 讲的自定义中断返回指令），因为 GNU objdump 不认识 PicoRV32 的自定义指令，会把它反汇编成 `.word 0x0400000b`，解码器必须自己补上。

最后还有两个健全性检查：若一行声明是分支但该地址的指令并非跳转类，打印 `UNEXPECTED BRANCH DATA`；若声明是访存地址但指令并非 load/store，打印 `UNEXPECTED ADDR DATA` [showtrace.py:41-47](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L41-L47)。它们能在 trace 与反汇编对不上时第一时间报警。

#### 4.3.4 代码实践

**实践目标**：亲手生成并解码一份 trace，读懂输出的每一列；再用它定位一条具体指令的执行。

**操作步骤**：

1. 生成 trace：`make test_vcd`（它会传 `+vcd +trace +noerror`，见 [Makefile:27-28](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L27-L28)），得到 `testbench.trace`。
2. 解码：`python3 showtrace.py testbench.trace firmware/firmware.elf`，把输出重定向到文件便于翻阅。
3. 在输出里找一行以 `IRQ >` 开头的记录——这是进入中断处理（跳到 `0x10`）的那一拍；再找紧随其后的若干行，观察中断向量里保存寄存器、调用 C 处理函数的过程（与 u6-l2 的 `irq_vec` 对应）。
4. 找一条 `lb/lw/sw` 指令，确认它出现**两次**：一次 `@地址`、一次 `=值`，验证 4.3.3 讲的「load/store 双行」特性。

**需要观察的现象**：

- 每行形如 `   =00000041 | 00001000 | 00002883 | lb a6, 16(a6)`：最左是 trace 标记与载荷，其后依次是 PC、指令编码、反汇编助记符。
- 进入中断处出现 `IRQ >00000010 | 00000010 | ...`，与 `PROGADDR_IRQ=0x10` 吻合。
- 整段流的首部应有 `FOUND BRANCH AND STARTING DECODING`（游标完成同步）。

**预期结果**：能稳定看到上述四列、IRQ 入口在 `0x10`、load/store 双行；若出现 `UNEXPECTED ...` 或 `NO INFORMATION ON INSN` 则说明 trace 与固件版本不匹配（例如忘了重新编译固件）。

> 若工具链或 iverilog 不可用，具体地址与指令值**待本地验证**；输出格式与「IRQ 入口=0x10」「load/store 双行」是确定的。

#### 4.3.5 小练习与答案

**练习 1**：一条 `jal`（无条件跳转并链接）指令在 trace 里会出现几行？载荷分别是什么？

**答案**：一行。`jal` 是分支（`latched_branch`），在 [picorv32.v:1520-1521](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1520-L1521) 走 `TRACE_BRANCH` 分支，载荷是目标 PC（`current_pc`）。它写回 `ra` 的动作不单独发 trace——`showtrace.py` 用 `>` 标记这条行并把 pc 游标跳到目标地址。注意：`jal` 同时也写 `ra`，但 trace 只记录「控制流目标」，不记录返回地址写回值。

**练习 2**：为什么 `showtrace.py` 要在 `irq_active` 由假变真时把 `pc` 设成 `0x10`，而不是依赖紧接着的分支记录？

**答案**：因为中断是**异步**发生的——它可能打断任意一条非分支指令，那条指令的 trace 载荷并不是「中断向量的地址」。如果不强制设 `pc=0x10`，游标会继续从中断前的 PC 往下推，与中断处理程序的实际地址对不上，导致后面每一行都贴错指令。强制跳到 `PROGADDR_IRQ`（0x10）才能让游标与真实的指令流重新同步。

**练习 3**：若把 `picorv32.v` 的 `PROGADDR_IRQ` 改成 `0x20` 重新综合，`showtrace.py` 的输出会出什么问题？怎么修？

**答案**：中断入口的实际指令在 `0x20`，但 `showtrace.py` 仍把游标设到 `0x10`（[showtrace.py:30](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/showtrace.py#L30) 硬编码），于是中断处理段会全部贴错指令，甚至打印 `NO INFORMATION ON INSN AT 00000010!`。修法是把脚本里的 `0x10` 改成 `0x20`（或让它从参数读取）。这是「打包端常量与解码端常量必须同步」这一契约的体现。

## 5. 综合实践

把本讲三块内容串成一个完整的「跑—测—看」流程：

1. **跑**：执行 `make test_vcd`，它通过 [Makefile:27-28](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L27-L28) 同时生成 `testbench.vcd`（波形）与 `testbench.trace`（执行流），并因 `+noerror` 即便失败也正常退出。
2. **测健壮性**：阅读 [testbench.v:277-478](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L277-L478) 的 `axi4_memory`，写一段 100 字以内的说明，解释 `+axi_test` 如何用 xorshift64 驱动 `fast/async/delay_axi_transaction` 三组随机位、分别从 `negedge` 与 `posedge` 两条路径给五个 AXI 通道注入「立即成交 / 异步提前 / 故意延迟」三种时序，从而验证 CPU 的 AXI 接口不依赖任何隐含时序假设。再跑 `make test_axi` 与 `make test` 对比总周期数，作为证据。
3. **看执行流**：用 `python3 showtrace.py testbench.trace firmware/firmware.elf > trace.txt` 解码，在 `trace.txt` 中：
   - 找到 `FOUND BRANCH AND STARTING DECODING` 确认游标已同步；
   - 找到一处 `IRQ >00000010`，说明中断把控制流带到 `PROGADDR_IRQ`；
   - 找到一条 load 指令，确认它占两行（`@` 地址 + `=` 值）；
   - 用 `riscv32-unknown-elf-objdump -d firmware/firmware.elf | grep <某地址>` 交叉验证 `showtrace.py` 贴的反汇编与 objdump 一致。
4. **（进阶）换引擎**：再跑 `make test_verilator`（若已装 Verilator），确认它与 iverilog 版产生**相同**的 `ALL TESTS PASSED.` 结论，体会「同一份 RTL、两种仿真器」的设计。

交付物：一段 `+axi_test` 机制的说明 + 一段解码后的 trace 片段（含上述四个标记）+ iverilog/Verilator 的对比结论。

## 6. 本讲小结

- `testbench.v` 是三层结构：`testbench`（环境）→ `picorv32_wrapper`（粘合被测核 `picorv32_axi`、中断激励与通过判定）→ `axi4_memory`（128 KB 带 UART/自检地址译码的 AXI 从端）。
- 通过判定是自检式的：固件向 `0x2000_0000` 写 `123456789` 置 `tests_passed`，`trap` 拉起时据此打印 `ALL TESTS PASSED.` 或 `ERROR!`。
- `+axi_test` 用 xorshift64 伪随机数驱动 `fast/async/delay_axi_transaction` 三组位，从 `negedge` 与 `posedge` 两条路径给 AXI 五通道注入随机延迟，压测总线接口健壮性。
- 同一份 `testbench.v` 兼容 iverilog 与 Verilator：用 `` `ifndef VERILATOR `` 剥掉纯仿真的 `testbench` 模块，在 Verilator 下顶层换成 `picorv32_wrapper`，环境职责由 `testbench.cc` 用 C++ 重写。
- 36 位 `trace_data` = 4 位标志（`TRACE_BRANCH`/`TRACE_ADDR`/`TRACE_IRQ`）+ 32 位载荷；`showtrace.py` 用 `objdump -d` 建地址字典，靠 `pc` 游标把每行 trace 贴到对应反汇编，分支重定位、访存不推进、压缩步进 2。
- `showtrace.py` 里硬编码的 `pc=0x10` 与 `retirq`（`0x0400000b`）是与 CPU 端 `PROGADDR_IRQ` 及自定义指令的契约——改其一必须改其二。

## 7. 下一步学习建议

- 本讲只讲了「怎么仿真」，下一讲 **u8-l3 形式化验证与综合评估** 会讲「怎么证明」：`make check` 用 yosys-smtbmc 做 SMTBMC 形式化验证，`scripts/vivado` 做面积/时序综合评估，二者与仿真互补。
- 想深入 `axi4_memory` 的时序细节，可对照 **u7-l1 AXI4-Lite 与 Wishbone 适配** 里讲的五通道握手规则，理解随机延迟到底在挑战适配器的哪些假设。
- 想看 Wishbone 版测试台与 AXI 版的对称差异，直接读 [testbench_wb.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v)：它的 `wb_ram` 是经典 Wishbone 从端，没有随机延迟，可作「最简从端」对照。
- 若对 trace 记录的指令提交语义感兴趣，回到 **u4-l2 主状态机 cpu_state** 看 `latched_trace`/`latched_branch`/`latched_stalu` 在哪些状态被置位——那决定了 trace 行的种类与时机。
